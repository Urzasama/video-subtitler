#!/usr/bin/env python3
"""Local web UI for video-subtitler.

    python server.py            # http://127.0.0.1:8770

No frameworks: http.server from the standard library serves the page and a
small JSON API. Uploads go in with a raw POST body, jobs run on worker
threads and the page polls their state.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

import subs_core as core

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(ROOT, "static")
WORK = os.path.join(ROOT, "work")
UPLOADS = os.path.join(WORK, "uploads")
OUTPUTS = os.path.join(WORK, "outputs")
HOST, PORT = "127.0.0.1", 8770
MAX_UPLOAD = 4 * 1024 * 1024 * 1024  # 4 GB

_JOBS: dict = {}
_LOCK = threading.Lock()


def _new_job(name: str) -> dict:
    jid = uuid.uuid4().hex[:12]
    job = {
        "id": jid,
        "name": name,
        "state": "queued",
        "progress": 0.0,
        "message": "queued",
        "created": time.time(),
        "segments": [],
        "files": {},
        "preview": "",
        "error": None,
    }
    with _LOCK:
        _JOBS[jid] = job
    return job


def _run_job(job: dict, media: str, options: dict) -> None:
    outdir = os.path.join(OUTPUTS, job["id"])
    try:
        job["state"] = "running"
        job["message"] = "extracting audio"

        def progress(frac, text=""):
            job["progress"] = round(min(0.99, max(0.0, frac)), 3)
            job["message"] = text or "transcribing"
            if text:
                job["preview"] = (job["preview"] + "\n" + text)[-4000:]

        def log(msg):
            job["message"] = msg

        res = core.pipeline(
            media, outdir,
            model=options.get("model", "tiny"),
            language=options.get("language") or None,
            task=options.get("task", "transcribe"),
            translate_to=options.get("to") or None,
            max_chars=int(options.get("max_chars") or 0),
            burn=bool(options.get("burn")),
            progress=progress, log=log,
        )
        segs = []
        srt_path = res["files"].get("srt")
        if srt_path and os.path.exists(srt_path):
            segs = [{"start": round(s.start, 3), "end": round(s.end, 3), "text": s.text}
                    for s in core.read_srt(srt_path)]
        job["segments"] = segs
        job["segment_count"] = len(segs)
        job["files"] = {os.path.basename(v): {"kind": k, "size": os.path.getsize(v)}
                        for k, v in res["files"].items() if os.path.exists(v)}
        job["target"] = res.get("target") or ""
        job["language"] = res.get("language") or ""
        job["progress"] = 1.0
        job["state"] = "done"
        job["message"] = "%d segments" % len(segs)
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI
        job["state"] = "error"
        job["error"] = "%s: %s" % (type(exc).__name__, exc)
        job["message"] = "failed"
    finally:
        shutil.rmtree(os.path.dirname(media), ignore_errors=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "video-subtitler/0.1"

    # ---------- helpers ----------
    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, fmt, *args):  # quieter console
        if os.environ.get("SUBS_DEBUG"):
            BaseHTTPRequestHandler.log_message(self, fmt, *args)

    def _serve_file(self, path):
        if not os.path.isfile(path):
            return self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as fh:
            data = fh.read()
        self._send(HTTPStatus.OK, data, ctype, {"Content-Disposition":
                                                "inline; filename=%s" % os.path.basename(path)})

    # ---------- routes ----------
    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        parts = [unquote(p) for p in url.path.split("/") if p]

        if not parts:
            return self._serve_file(os.path.join(STATIC, "index.html"))
        if parts[0] == "static" and len(parts) > 1:
            return self._serve_file(os.path.join(STATIC, *parts[1:]))
        if parts[0] == "api":
            if parts[1:] == ["health"]:
                return self._send(HTTPStatus.OK, {"ok": True, "version": "0.1.0"})
            if parts[1:2] == ["jobs"]:
                jid = parts[2] if len(parts) > 2 else ""
                with _LOCK:
                    job = dict(_JOBS.get(jid) or {})
                if not job:
                    return self._send(HTTPStatus.NOT_FOUND, {"error": "no such job"})
                if len(parts) == 3:
                    return self._send(HTTPStatus.OK, job)
                if len(parts) >= 5 and parts[3] == "file":
                    name = os.path.basename(parts[4])
                    return self._serve_file(os.path.join(OUTPUTS, jid, name))
        return self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        url = urlparse(self.path)
        if url.path != "/api/jobs":
            return self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return self._send(HTTPStatus.BAD_REQUEST, {"error": "empty upload"})
        if length > MAX_UPLOAD:
            return self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "file too large"})

        raw_name = self.headers.get("X-Filename") or "upload.bin"
        name = re.sub(r"[^\w.\-() \u4e00-\u9fff]", "_", unquote(raw_name)) or "upload.bin"
        options = {
            "model": self.headers.get("X-Model") or "tiny",
            "language": self.headers.get("X-Language") or "",
            "task": self.headers.get("X-Task") or "transcribe",
            "to": self.headers.get("X-To") or "",
            "max_chars": self.headers.get("X-Max-Chars") or "0",
            "burn": (self.headers.get("X-Burn") or "").lower() in ("1", "true", "yes"),
        }

        os.makedirs(UPLOADS, exist_ok=True)
        job = _new_job(name)
        jobdir = os.path.join(UPLOADS, job["id"])
        os.makedirs(jobdir, exist_ok=True)
        # keep the original filename: the pipeline derives the subtitle names from it
        dest = os.path.join(jobdir, name)
        remaining = length
        with open(dest, "wb") as fh:
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                fh.write(chunk)
                remaining -= len(chunk)

        threading.Thread(target=_run_job, args=(job, dest, options), daemon=True).start()
        return self._send(HTTPStatus.CREATED, {"id": job["id"]})


def main() -> None:
    for path in (UPLOADS, OUTPUTS):
        os.makedirs(path, exist_ok=True)
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print("video-subtitler 0.1.0  ->  http://%s:%d" % (HOST, PORT))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
