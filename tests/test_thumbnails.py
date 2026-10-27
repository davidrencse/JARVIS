"""Preview output, input limits, and storage boundaries."""
import base64
import inspect
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image
from jarvis.storage import Storage, REMOTE_SCRIPT
from jarvis.thumbnails import thumbnail_payload


class ThumbnailTests(unittest.TestCase):
    def test_preview_and_storage_boundary(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            allowed = root / "allowed"
            allowed.mkdir()
            source = allowed / "photo.png"
            Image.new("RGB", (800, 600), "red").save(source)
            fs = Storage({"devices": {"pc": {"transport": "local"}},
                          "storage": {"pc": {"roots": [str(allowed)]}}})
            result = fs.resolve("pc", str(source), "thumbnail")
            with Image.open(io.BytesIO(base64.b64decode(result["image"]))) as image:
                self.assertEqual(image.format, "PNG")
                self.assertEqual(image.size, (240, 180))
            outside = root / "outside.png"
            Image.new("RGB", (10, 10)).save(outside)
            with self.assertRaises(ValueError):
                fs.resolve("pc", str(outside), "thumbnail")
            with self.assertRaises(ValueError):
                fs.resolve("pc", str(allowed), "thumbnail")

    def test_remote_script_preview(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "image.png"
            Image.new("RGB", (60, 40), "blue").save(source)
            request = json.dumps({"action": "thumbnail", "path": str(source), "roots": [temp]})
            script = inspect.getsource(thumbnail_payload) + "\nREQUEST = " + repr(request) + "\n" + REMOTE_SCRIPT
            result = subprocess.run([sys.executable, "-"], input=script, text=True,
                                    capture_output=True, check=True, timeout=15)
            self.assertIn("image", json.loads(result.stdout))

    def test_invalid_and_oversized_files(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "fake.png"
            source.write_text("not an image")
            with self.assertRaises(OSError):
                thumbnail_payload(source)
            with source.open("wb") as out:
                out.truncate(20 * 1024 * 1024 + 1)
            with self.assertRaises(ValueError):
                thumbnail_payload(source)
