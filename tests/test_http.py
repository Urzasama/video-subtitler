#!/usr/bin/env python3
"""HTTP-layer tests for server.py.

The transcription core is stubbed out, so these tests cover the part that is
easy to get wrong -- routing, upload handling, job state and downloads -- in
about a second, without a Whisper model.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

import server as srv


def _fake_pipeline(media, outdir, model="tiny", language=None, task="transcribe",
                   translate_to=None, max_chars=0, burn=False, progress=None, log=None):
    """Stand-in for subs_core.pipeline: writes a two-cue subtitle set."""
    os.makedirs(outdir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(media))[0]
    if progress:
        progress(0.5, "half way")
    path = os.path.join(outdir, stem + ".srt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("1\n00:00:00,000 --> 00:00:01,000\nhello\n\n"
                 "2\n00:00:01,000 --> 00:00:02,000\nworld\n")
    if log:
        log("done")
    return {"media": media, "stem": stem, "language": "en", "target": "",
            "files": {"srt": path}, "segments": 2}


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="subs_http_")
        cls._saved = (srv.UPLOADS, srv.OUTPUTS, srv.core.pipeline)
        srv.UPLOADS = os.path.join(cls.tmp, "uploads")
        srv.OUTPUTS = os.path.join(cls.tmp, "outputs")
        srv.core.pipeline = _fake_pipeline
        os.makedirs(srv.UPLOADS, exist_ok=True)
        os.makedirs(srv.OUTPUTS, exist_ok=True)

        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        srv.UPLOADS, srv.OUTPUTS, srv.core.pipeline = cls._saved
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---------- helpers ----------
    def _get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as r:
                return r.status, r.read(), r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), exc.headers.get("Content-Type", "")

    def _post(self, payload: bytes, name="clip.mp4", **headers):
        # browsers percent-encode the filename; headers themselves are latin-1 only
        hdrs = {"Content-Type": "application/octet-stream",
                "X-Filename": urllib.parse.quote(name)}
        hdrs.update({k.replace("_", "-"): v for k, v in headers.items()})
        req = urllib.request.Request(self.base + "/api/jobs", data=payload,
                                     method="POST", headers=hdrs)
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))

    def _wait(self, jid, timeout=20.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, body, _ = self._get("/api/jobs/" + jid)
            job = json.loads(body.decode("utf-8"))
            if job["state"] in ("done", "error"):
                return job
            time.sleep(0.05)
        self.fail("job %s did not finish in %.0fs" % (jid, timeout))

    # ---------- tests ----------
    def test_health(self):
        code, body, ctype = self._get("/api/health")
        self.assertEqual(code, 200)
        self.assertIn("application/json", ctype)
        self.assertTrue(json.loads(body.decode("utf-8"))["ok"])

    def test_index_page_is_served(self):
        code, body, ctype = self._get("/")
        self.assertEqual(code, 200)
        self.assertIn("text/html", ctype)
        self.assertIn(b"<html", body.lower())
        self.assertIn(b"/static/app.js", body)

    def test_static_assets(self):
        for name in ("app.js", "style.css"):
            code, body, _ = self._get("/static/" + name)
            self.assertEqual(code, 200, name)
            self.assertTrue(body, name)

    def test_upload_runs_job_and_serves_download(self):
        code, created = self._post(b"not really a video", name="clip.mp4",
                                   X_Model="tiny", X_Task="transcribe")
        self.assertEqual(code, 201)
        self.assertIn("id", created)

        job = self._wait(created["id"])
        self.assertEqual(job["state"], "done", job.get("error"))
        self.assertEqual(job["segment_count"], 2)
        self.assertEqual(len(job["segments"]), 2)
        self.assertEqual(job["segments"][0]["text"], "hello")
        self.assertEqual(job["name"], "clip.mp4")
        self.assertIn("clip.srt", job["files"])
        self.assertEqual(job["files"]["clip.srt"]["size"] > 0, True)

        code, body, ctype = self._get("/api/jobs/%s/file/clip.srt" % created["id"])
        self.assertEqual(code, 200)
        self.assertIn("00:00:01,000", body.decode("utf-8"))

        # the upload is cleaned up once the job finishes
        self.assertFalse(os.path.exists(os.path.join(srv.UPLOADS, created["id"])))

    def test_upload_name_keeps_original_stem(self):
        code, created = self._post(b"x", name="我的 视频.mp4")
        self.assertEqual(code, 201)
        job = self._wait(created["id"])
        self.assertEqual(job["state"], "done", job.get("error"))
        # subtitle files are named after the upload, not after the temp path
        self.assertTrue(any(k.startswith("我的 视频") for k in job["files"]), job["files"])

    def test_upload_is_required(self):
        req = urllib.request.Request(self.base + "/api/jobs", data=b"",
                                     method="POST",
                                     headers={"X-Filename": "x.mp4"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 400)

    def test_unknown_job_and_paths_are_404(self):
        code, _, _ = self._get("/api/jobs/deadbeef")
        self.assertEqual(code, 404)
        code, _, _ = self._get("/api/jobs/deadbeef/file/x.srt")
        self.assertEqual(code, 404)
        code, _, _ = self._get("/nope")
        self.assertEqual(code, 404)

    def test_download_is_confined_to_the_job_dir(self):
        code, created = self._post(b"x", name="../../escape.mp4")
        self.assertEqual(code, 201)
        job = self._wait(created["id"])
        self.assertEqual(job["state"], "done", job.get("error"))
        code, _, _ = self._get("/api/jobs/%s/file/..%%2F..%%2Fserver.py" % created["id"])
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()
