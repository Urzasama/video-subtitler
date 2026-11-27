#!/usr/bin/env python3
"""video-subtitler - turn any video/audio file into subtitles, in one command.

    python subtitler.py clip.mp4                     # transcribe, keep the spoken language
    python subtitler.py clip.mp4 --task translate    # offline: Whisper translates to English
    python subtitler.py clip.mp4 --to zh             # translate into Chinese via an LLM endpoint
    python subtitler.py clip.mp4 --burn              # also write clip.subbed.mp4 with burned-in subs
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import subs_core as core


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="subtitler",
        description="Transcribe and translate video/audio into subtitle files.",
    )
    p.add_argument("media", nargs="+", help="input video/audio file(s)")
    p.add_argument("-o", "--outdir", default=None,
                   help="output folder (default: next to each input file)")
    p.add_argument("-m", "--model", default="tiny",
                   help="faster-whisper model: tiny/base/small/medium/large-v3 (default: tiny)")
    p.add_argument("-l", "--language", default=None,
                   help="spoken language hint, e.g. zh, en (default: auto detect)")
    p.add_argument("--task", choices=["transcribe", "translate"], default="transcribe",
                   help="translate = Whisper's built-in offline translation to English")
    p.add_argument("--to", dest="translate_to", default=None,
                   help="translate into this language via an OpenAI-compatible endpoint "
                        "(needs OPENAI_API_KEY / DEEPSEEK_API_KEY)")
    p.add_argument("--llm-model", default=None, help="override the chat model name")
    p.add_argument("--llm-base", default=None, help="override the chat base URL")
    p.add_argument("--max-chars", type=int, default=0,
                   help="split lines longer than this many characters (0 = off)")
    p.add_argument("--burn", action="store_true", help="burn subtitles into an .mp4")
    p.add_argument("--query", action="store_true",
                   help="do not transcribe: just print the segments of an existing .srt")
    p.add_argument("-q", "--quiet", action="store_true")
    return p


def human(seconds: float) -> str:
    return "%d:%04.1f" % (int(seconds // 60), seconds % 60)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    quiet = args.quiet
    failed = 0

    for media in args.media:
        if not os.path.exists(media):
            print("!! not found: %s" % media, file=sys.stderr)
            failed += 1
            continue

        outdir = args.outdir or os.path.dirname(os.path.abspath(media))
        t0 = time.time()
        if not quiet:
            print("==> %s" % media)

        last = [0.0]

        def progress(frac, text=""):
            if quiet:
                return
            now = time.time()
            if now - last[0] < 0.5 and frac < 1.0:
                return
            last[0] = now
            bar = int(frac * 24)
            sys.stdout.write("\r    [%s%s] %3d%%  %s" %
                             ("#" * bar, "." * (24 - bar), int(frac * 100), text[:48].ljust(48)))
            sys.stdout.flush()

        try:
            res = core.pipeline(
                media, outdir, model=args.model, language=args.language,
                task=args.task, translate_to=args.translate_to,
                max_chars=args.max_chars, burn=args.burn,
                progress=progress,
                log=None if quiet else (lambda msg: print("    " + msg)),
            )
        except Exception as exc:  # noqa: BLE001 - CLI boundary
            print("\n!! failed: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
            failed += 1
            continue

        if not quiet:
            sys.stdout.write("\r" + " " * 90 + "\r")
        print("    %d segments in %.1fs -> %s" %
              (res["segments"], time.time() - t0, os.path.dirname(res["files"]["srt"])))
        for kind, path in res["files"].items():
            print("      %-12s %s (%d B)" % (kind, os.path.basename(path),
                                             os.path.getsize(path) if os.path.exists(path) else 0))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
