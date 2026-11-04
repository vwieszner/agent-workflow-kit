"""design_assets.py: SKIPPED without config, design renders per manifest frame, docs-page groups in
`newest` and `all` modes (masters kept), a phase without credentials skipped, an HTTP error exits 2."""
from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _state_testenv as te  # noqa: E402
import design_assets  # noqa: E402

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAwS2OUAAAAABJRU5ErkJggg==")


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Opener:
    def __init__(self, routes):
        self.routes, self.seen = routes, []

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.seen.append((url, dict(req.header_items())))
        for prefix, body in self.routes:
            if url.startswith(prefix):
                if isinstance(body, Exception):
                    raise body
                return FakeResp(body if isinstance(body, bytes) else json.dumps(body).encode())
        raise AssertionError(f"unexpected URL {url}")


MANIFEST = {
    "design_source": {"screens_file_key": "SCR", "frames": [{"slug": "home", "node": "1:2"}]},
    "attachments": {"groups": [
        {"dir": "portraits", "mode": "newest", "pages": [{"slug": "ada", "page": "11"}],
         "max_edge": 768, "format": "png"},
        {"dir": "backdrops", "mode": "all", "page": "22", "prefix": "bg", "max_edge": None, "format": "png"},
    ]},
}


class DesignAssetsTests(unittest.TestCase):
    def _setup(self, manifest=MANIFEST):
        root = te.enter(self, '[design]\nintake_dir = "intake"\nmanifest = "m.json"\n'
                              '[docs_mirror]\nsite = "https://docs.example"\n')
        (root / "m.json").write_text(json.dumps(manifest), encoding="utf-8")
        te.clear_caches()
        return root

    def _run(self, *argv, opener=None, env=None):
        out = io.StringIO()
        with mock.patch.dict(os.environ, env or {}, clear=False), contextlib.redirect_stdout(out):
            rc = design_assets.main(list(argv), opener=opener)
        return rc, out.getvalue()

    def test_unconfigured_is_skipped(self):
        te.enter(self, "")
        rc, out = self._run()
        self.assertEqual(rc, 3, out)
        self.assertIn("SKIPPED", out)

    def test_renders_and_docs_groups_are_written_with_masters(self):
        root = self._setup()
        opener = Opener([
            ("https://api.figma.com/v1/images/SCR", {"images": {"1:2": "https://cdn.example/home.png"}}),
            ("https://cdn.example/", PNG),
            ("https://docs.example/wiki/api/v2/pages/11/attachments", {"results": [{"id": "9", "downloadLink": "/download/a"}]}),
            ("https://docs.example/wiki/api/v2/pages/22/attachments",
             {"results": [{"id": "b", "downloadLink": "/download/b"}, {"id": "a", "downloadLink": "/download/c"}]}),
            ("https://docs.example/wiki/download/", PNG),
        ])
        rc, out = self._run(opener=opener, env={"FIGMA_ACCESS_TOKEN": "t", "TRACKER_USER": "u", "TRACKER_TOKEN": "k"})
        self.assertEqual(rc, 0, out)
        intake = root / "intake"
        for rel in ("assets/home.png", "assets/portraits/ada.png", "assets-master/portraits/ada.png",
                    "assets/backdrops/bg-01.png", "assets/backdrops/bg-02.png"):
            self.assertTrue((intake / rel).is_file(), rel)
        self.assertIn("4 file(s) written", out)
        figma_headers = opener.seen[0][1]
        self.assertEqual(figma_headers.get("X-figma-token"), "t")
        docs = [h for u, h in opener.seen if "docs.example" in u]
        self.assertTrue(all(h.get("Authorization", "").startswith("Basic ") for h in docs))

    def test_missing_credentials_skip_each_phase(self):
        self._setup()
        rc, out = self._run(opener=Opener([]), env={"FIGMA_ACCESS_TOKEN": "", "TRACKER_USER": "", "TRACKER_TOKEN": ""})
        self.assertEqual(rc, 1, out)
        self.assertIn("SKIP design renders: FIGMA_ACCESS_TOKEN is not set", out)
        self.assertIn("SKIP docs-page images", out)

    def test_http_error_exits_2_and_names_a_rejected_token(self):
        self._setup()
        err = urllib.error.HTTPError("https://api.figma.com/v1/images/SCR", 403, "Forbidden", {}, io.BytesIO(b"no"))
        rc, out = self._run("--design-only", opener=Opener([("https://api.figma.com/", err)]),
                            env={"FIGMA_ACCESS_TOKEN": "t"})
        self.assertEqual(rc, 2, out)
        self.assertIn("HTTP 403", out)
        self.assertIn("token is rejected", out)


if __name__ == "__main__":
    unittest.main()
