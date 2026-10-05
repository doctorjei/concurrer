"""Browsers run from local disk, not the NFS-backed $HOME install."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from concurrer.login import stage_local_browsers


class BrowserStagingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.installed = root / "home-cache"
        (self.installed / "chromium-1243").mkdir(parents=True)
        (self.installed / "chromium-1243" / "chrome").write_bytes(b"bin")
        (self.installed / ".links").mkdir()
        self.local = root / "local"

    def tearDown(self):
        self.tmp.cleanup()

    def test_copies_when_missing_and_points_playwright_at_it(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
            got = stage_local_browsers(self.installed, self.local)
            self.assertEqual(got, self.local)
            self.assertEqual(os.environ["PLAYWRIGHT_BROWSERS_PATH"], str(self.local))
        self.assertEqual((self.local / "chromium-1243" / "chrome").read_bytes(), b"bin")
        self.assertFalse(self.local.with_name("local.partial").exists())

    def test_current_copy_is_reused_without_recopying(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
            stage_local_browsers(self.installed, self.local)
            with patch("concurrer.login.shutil.copytree") as copy:
                stage_local_browsers(self.installed, self.local)
            copy.assert_not_called()

    def test_explicit_env_is_respected(self):
        with patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": "/elsewhere"}):
            self.assertIsNone(stage_local_browsers(self.installed, self.local))
            self.assertEqual(os.environ["PLAYWRIGHT_BROWSERS_PATH"], "/elsewhere")
        self.assertFalse(self.local.exists())

    def test_copy_failure_falls_back_without_setting_env(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
            with patch("concurrer.login.shutil.copytree", side_effect=OSError("disk full")):
                self.assertIsNone(stage_local_browsers(self.installed, self.local))
            self.assertNotIn("PLAYWRIGHT_BROWSERS_PATH", os.environ)
        self.assertFalse(self.local.exists())


if __name__ == "__main__":
    unittest.main()
