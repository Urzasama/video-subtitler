"""subs_core - transcription, translation and subtitle writing for video-subtitler.

Everything here is plain Python + ffmpeg. Transcription uses faster-whisper
(CPU int8), translation to English can be done fully offline with Whisper's
built-in `translate` task, any other target language goes through an
OpenAI-compatible chat endpoint.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence

# --------------------------------------------------------------------------
# data model
# --------------------------------------------------------------------------


@dataclass
class Segment:
    start: float
    end: float
    text: str

    def shifted(self, delta: float) -> "Segment":
        return Segment(self.start + delta, self.end + delta, self.text)

    def as_dict(self) -> dict:
        return {"start": round(self.start, 3), "end": round(self.end, 3), "text": self.text}


# --------------------------------------------------------------------------
# timecode helpers
# --------------------------------------------------------------------------


def fmt_srt(seconds: float) -> str:
    """00:00:01,250"""
    if seconds < 0:
        seconds = 0.0
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def fmt_vtt(seconds: float) -> str:
    """00:00:01.250"""
    return fmt_srt(seconds).replace(",", ".")


_TC = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})")


def parse_timecode(value: str) -> float:
    m = _TC.search(value.strip())
    if not m:
        raise ValueError("bad timecode: %r" % value)
    h, mi, s, ms = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0


# --------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------

_SENT_END = "。！？!?；;…."


def split_long(text: str, max_chars: int = 0) -> List[str]:
    """Split an over-long subtitle line on punctuation, then on spaces."""
    text = text.strip()
    if max_chars <= 0 or len(text) <= max_chars:
        return [text] if text else []
    out, cur = [], ""
    for ch in text:
        cur += ch
        if ch in _SENT_END and len(cur) >= max_chars * 0.6:
            out.append(cur.strip())
            cur = ""
        elif len(cur) >= max_chars and ch == " ":
            out.append(cur.strip())
            cur = ""
    if cur.strip():
        out.append(cur.strip())
    # a single huge chunk with no spaces (long CJK run) - hard cut
    final = []
    for piece in out:
        while len(piece) > max_chars * 1.5:
            final.append(piece[:max_chars].strip())
            piece = piece[max_chars:]
        if piece.strip():
            final.append(piece.strip())
    return final


def segments_to_lines(segments: Sequence[Segment], max_chars: int = 0) -> List[Segment]:
    """Split segments whose text is longer than max_chars, sharing time evenly."""
    if max_chars <= 0:
        return list(segments)
    out: List[Segment] = []
    for seg in segments:
        parts = split_long(seg.text, max_chars)
        if len(parts) <= 1:
            out.append(seg)
            continue
        total = max(1, sum(len(p) for p in parts))
        t = seg.start
        span = max(0.0, seg.end - seg.start)
        for part in parts:
            dur = span * len(part) / total
            out.append(Segment(t, t + dur, part))
            t += dur
    return out


def merge_segments(segments: Sequence[Segment], gap: float = 0.35,
                   max_chars: int = 0) -> List[Segment]:
    """Merge consecutive tiny segments that belong to the same breath group."""
    if not segments:
        return []
    out = [Segment(segments[0].start, segments[0].end, segments[0].text)]
    for seg in segments[1:]:
        prev = out[-1]
        can = (seg.start - prev.end) <= gap
        if can and max_chars and len(prev.text) + 1 + len(seg.text) <= max_chars:
            prev.text = (prev.text + " " + seg.text).strip()
            prev.end = max(prev.end, seg.end)
        else:
            out.append(Segment(seg.start, seg.end, seg.text))
    return out


# --------------------------------------------------------------------------
# writers
# --------------------------------------------------------------------------


def write_srt(segments: Sequence[Segment], path: str) -> str:
    lines = []
    for i, seg in enumerate(segments, 1):
        lines.append("%d" % i)
        lines.append("%s --> %s" % (fmt_srt(seg.start), fmt_srt(seg.end)))
        lines.append(seg.text)
        lines.append("")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines))
    return path


def write_vtt(segments: Sequence[Segment], path: str) -> str:
    lines = ["WEBVTT", ""]
    for seg in segments:
        lines.append("%s --> %s" % (fmt_vtt(seg.start), fmt_vtt(seg.end)))
        lines.append(seg.text)
        lines.append("")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines))
    return path


def write_txt(segments: Sequence[Segment], path: str) -> str:
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(s.text for s in segments) + "\n")
    return path


def write_bilingual(source: Sequence[Segment], target: Sequence[Segment], path: str) -> str:
    n = min(len(source), len(target))
    lines = []
    for i in range(n):
        lines.append("%d" % (i + 1))
        lines.append("%s --> %s" % (fmt_srt(source[i].start), fmt_srt(source[i].end)))
        lines.append(source[i].text)
        lines.append(target[i].text)
        lines.append("")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines))
    return path


def write_json(segments: Sequence[Segment], path: str, meta: Optional[dict] = None) -> str:
    payload = {"segments": [s.as_dict() for s in segments]}
    if meta:
        payload["meta"] = meta
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return path


def read_srt(path: str) -> List[Segment]:
    out: List[Segment] = []
    block = []
    with open(path, encoding="utf-8-sig") as fh:
        lines = fh.read().splitlines()
    for raw in lines + [""]:
        if raw.strip():
            block.append(raw)
            continue
        if len(block) >= 3 and "-->" in block[1]:
            a, b = block[1].split("-->")
            out.append(Segment(parse_timecode(a), parse_timecode(b), "\n".join(block[2:])))
        block = []
    return out


# --------------------------------------------------------------------------
# ffmpeg
# --------------------------------------------------------------------------


def ffmpeg_bin() -> str:
    env = os.environ.get("FFMPEG_BIN")
    if env and os.path.exists(env):
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise RuntimeError("ffmpeg not found - set FFMPEG_BIN or put ffmpeg on PATH")


def extract_audio(video: str, out_wav: str, sample_rate: int = 16000) -> str:
    cmd = [ffmpeg_bin(), "-y", "-loglevel", "error", "-i", video,
           "-vn", "-ac", "1", "-ar", str(sample_rate), "-c:a", "pcm_s16le", out_wav]
    subprocess.run(cmd, check=True)
    return out_wav


def _style_arg(font: str, size: int, margin_v: int = 40) -> str:
    return ("FontName=%s,FontSize=%d,PrimaryColour=&H00FFFFFF,"
            "OutlineColour=&H60000000,BorderStyle=1,Outline=2,Shadow=0,"
            "MarginV=%d" % (font, size, margin_v))


def burn_subs(video: str, srt_path: str, out_path: str, font: str = "Microsoft YaHei",
              size: int = 22) -> str:
    """Hard-burn an .srt into the video. Works from a temp cwd so the filter
    argument stays a simple relative path (ffmpeg path escaping on Windows)."""
    tmp = tempfile.mkdtemp(prefix="subs_")
    try:
        local_srt = os.path.join(tmp, "sub.srt")
        shutil.copyfile(srt_path, local_srt)
        style = _style_arg(font, size)
        vf = "subtitles=sub.srt:force_style='%s'" % style
        cmd = [ffmpeg_bin(), "-y", "-loglevel", "error", "-i", os.path.abspath(video),
               "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
               "-c:a", "copy", os.path.abspath(out_path)]
        subprocess.run(cmd, check=True, cwd=tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out_path


# --------------------------------------------------------------------------
# transcription
# --------------------------------------------------------------------------

_MODEL_CACHE: dict = {}


def load_model(model: str = "tiny", device: str = "cpu", compute_type: str = "int8"):
    from faster_whisper import WhisperModel  # imported lazily: optional dependency at import time

    key = (model, device, compute_type)
    if key not in _MODEL_CACHE:
        _MODEL_CACHE[key] = WhisperModel(model, device=device, compute_type=compute_type)
    return _MODEL_CACHE[key]


def transcribe_ex(path: str, model: str = "tiny", language: Optional[str] = None,
                  task: str = "transcribe", vad: bool = True,
                  beam_size: int = 5, progress=None):
    """Run faster-whisper and return (segments, detected_language)."""
    m = load_model(model)
    segs, info = m.transcribe(
        path,
        language=language,
        task=task,
        vad_filter=vad,
        beam_size=beam_size,
        condition_on_previous_text=False,
    )
    out: List[Segment] = []
    total = getattr(info, "duration", 0) or 0
    for s in segs:
        text = (s.text or "").strip()
        if text:
            out.append(Segment(float(s.start), float(s.end), text))
        if progress and total:
            progress(min(1.0, float(s.end) / total), text)
    return out, (getattr(info, "language", None) or language or "auto")


def transcribe(path: str, model: str = "tiny", language: Optional[str] = None,
               task: str = "transcribe", vad: bool = True,
               beam_size: int = 5, progress=None) -> List[Segment]:
    """Run faster-whisper on a media file and return timed segments."""
    segs, _lang = transcribe_ex(path, model=model, language=language, task=task,
                                vad=vad, beam_size=beam_size, progress=progress)
    return segs


def _split_words_proportional(text: str, weights):
    """Split text on word boundaries into len(weights) chunks sized by weight."""
    words = text.split()
    n = len(weights)
    if len(words) <= n:
        return [" ".join(words[i:i + 1]) for i in range(min(n, len(words)))] + [""] * max(0, n - len(words))
    total = float(sum(weights)) or 1.0
    out, i = [], 0
    for k, w in enumerate(weights):
        take = max(1, int(round(len(words) * (w / total)))) if k < n - 1 else len(words) - i
        take = min(take, len(words) - i - (n - k - 1))
        take = max(take, 1)
        out.append(" ".join(words[i:i + take]))
        i += take
    return out


def align_translation(source: Sequence[Segment], translated: Sequence[Segment],
                      ) -> List[Segment]:
    """Re-time a translation onto the source timeline.

    The offline translate pass segments audio on its own boundaries, so text is
    distributed across the source lines by time overlap: one translated line
    covering several source lines is split between them on word boundaries,
    several translated lines inside one source line are joined.
    """
    if not translated:
        return []
    if len(source) == len(translated):
        return [Segment(s.start, s.end, t.text) for s, t in zip(source, translated)]

    buckets = [[] for _ in source]
    for t in translated:
        hits = []
        for i, s in enumerate(source):
            overlap = min(s.end, t.end) - max(s.start, t.start)
            if overlap > 0.01:
                hits.append((i, overlap))
        if not hits:
            nearest = min(range(len(source)), key=lambda i: abs(source[i].start - t.start))
            buckets[nearest].append((t.text, max(0.01, t.end - t.start)))
            continue
        if len(hits) == 1:
            buckets[hits[0][0]].append((t.text, hits[0][1]))
        else:
            parts = _split_words_proportional(t.text, [h[1] for h in hits])
            for (i, _w), part in zip(hits, parts):
                if part:
                    buckets[i].append((part, 1.0))

    out: List[Segment] = []
    for seg, parts in zip(source, buckets):
        text = " ".join(p for p, _w in parts).strip()
        text = re.sub(r"\s+", " ", text)
        out.append(Segment(seg.start, seg.end, text))
    return out


# --------------------------------------------------------------------------
# optional LLM translation (any target language)
# --------------------------------------------------------------------------

_DEFAULT_PROMPT = (
    "You are a professional subtitle translator. Translate each numbered subtitle "
    "line into {lang}. Keep it short and natural for on-screen reading, keep names "
    "and technical terms, and never merge or drop lines. "
    "Return ONLY a JSON array of strings, one per input line, same order, same count."
)


def translate_llm(segments: Sequence[Segment], target: str, model: Optional[str] = None,
                  base_url: Optional[str] = None, api_key: Optional[str] = None,
                  batch: int = 20) -> List[Segment]:
    """Translate with any OpenAI-compatible /chat/completions endpoint."""
    import urllib.request

    key = api_key or os.environ.get("OPENAI_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("no API key: set OPENAI_API_KEY (or DEEPSEEK_API_KEY)")
    base = (base_url or os.environ.get("OPENAI_BASE_URL")
            or os.environ.get("DEEPSEEK_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    model = model or os.environ.get("OPENAI_MODEL") or os.environ.get("DEEPSEEK_MODEL") or "gpt-4o-mini"
    prompt = _DEFAULT_PROMPT.replace("{lang}", target)

    out: List[Segment] = []
    for i in range(0, len(segments), batch):
        chunk = segments[i:i + batch]
        body = {
            "model": model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps([s.text for s in chunk], ensure_ascii=False)},
            ],
        }
        req = urllib.request.Request(
            base + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.load(resp)
        content = data["choices"][0]["message"]["content"].strip()
        content = re.sub(r"^```(?:json)?|```$", "", content, flags=re.M).strip()
        try:
            lines = json.loads(content)
        except json.JSONDecodeError:
            lines = [ln.strip(" ,\"'") for ln in content.splitlines() if ln.strip()]
        if not isinstance(lines, list) or len(lines) != len(chunk):
            raise RuntimeError("translator returned %d lines for %d inputs"
                               % (len(lines) if isinstance(lines, list) else -1, len(chunk)))
        for seg, text in zip(chunk, lines):
            out.append(Segment(seg.start, seg.end, str(text).strip()))
    return out


# --------------------------------------------------------------------------
# top-level pipeline
# --------------------------------------------------------------------------


def pipeline(media: str, outdir: str, model: str = "tiny", language: Optional[str] = None,
             task: str = "transcribe", target_lang: Optional[str] = None,
             translate_to: Optional[str] = None, max_chars: int = 0,
             burn: bool = False, progress=None, log=None) -> dict:
    """Transcribe (optionally translate) a media file and write subtitle files."""
    os.makedirs(outdir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(media))[0]
    work = tempfile.mkdtemp(prefix="subtitler_")
    result = {"media": media, "stem": stem, "files": {}, "language": language or "auto"}
    try:
        wav = os.path.join(work, "audio.wav")
        if log:
            log("extracting audio ...")
        extract_audio(media, wav)

        if log:
            log("transcribing with %s (task=%s) ..." % (model, task))
        base, detected = transcribe_ex(wav, model=model, language=language,
                                       task=task if task == "transcribe" else "transcribe",
                                       progress=progress)
        if task == "translate" and not detected.lower().startswith("en"):
            if log:
                log("second pass: translating %s -> en ..." % detected)
            translated_pass = transcribe(wav, model=model, language=detected,
                                         task="translate")
        else:
            translated_pass = None
        base = merge_segments(base, max_chars=max_chars) if max_chars else base
        base = segments_to_lines(base, max_chars)

        translated: Optional[List[Segment]] = None
        en_primary = False
        if task == "translate" and translated_pass is not None:
            en = merge_segments(translated_pass, gap=0.6)
            en = segments_to_lines(en, max_chars or 84)
            translated = [Segment(s.start, s.end, s.text) for s in en if s.text.strip()]
            en_primary = True
            write_srt(base, os.path.join(outdir, "%s.%s.srt" % (stem, detected)))
            result["files"]["source"] = os.path.join(outdir, "%s.%s.srt" % (stem, detected))
            result["target"] = "en"
        elif translate_to:
            if log:
                log("translating to %s ..." % translate_to)
            translated = translate_llm(base, translate_to)
            suffix = translate_to.split("-")[0]
            write_srt(translated, os.path.join(outdir, "%s.%s.srt" % (stem, suffix)))
            write_vtt(translated, os.path.join(outdir, "%s.%s.vtt" % (stem, suffix)))
            result["files"]["translated"] = os.path.join(outdir, "%s.%s.srt" % (stem, suffix))
            result["target"] = translate_to

        primary = translated if translated else base
        srt = os.path.join(outdir, stem + ".srt")
        write_srt(primary, srt)
        result["files"]["srt"] = srt
        result["files"]["vtt"] = write_vtt(primary, os.path.join(outdir, stem + ".vtt"))
        result["files"]["txt"] = write_txt(primary, os.path.join(outdir, stem + ".txt"))
        result["files"]["json"] = write_json(
            primary, os.path.join(outdir, stem + ".json"),
            meta={"source": os.path.basename(media), "model": model,
                  "language": detected, "task": task, "target": result.get("target")})
        result["segments"] = len(primary)

        # A line-by-line bilingual file is only honest when the translation was made
        # per source line (the LLM path). A second Whisper pass cuts the audio on its
        # own boundaries, so pairing the two takes would invent an alignment.
        if translated is not None and not en_primary:
            bi = write_bilingual(base, translated, os.path.join(outdir, stem + ".bilingual.srt"))
            result["files"]["bilingual"] = bi

        if burn:
            if log:
                log("burning subtitles into video ...")
            burnt = os.path.join(outdir, stem + ".subbed.mp4")
            burn_subs(media, srt, burnt)
            result["files"]["video"] = burnt
        return result
    finally:
        shutil.rmtree(work, ignore_errors=True)
