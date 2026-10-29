
from pathlib import Path
from io import BytesIO
from PIL import Image, ImageDraw, ImageOps
from concurrent.futures import TimeoutError as JobTimeout
from unittest.mock import Mock
import unittest
import tempfile
from test_workspace import APP, storage_functions

class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=APP.parent, ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.source = self.directory / "source.webp"
        Image.new("RGB", (768, 768), "blue").save(self.source)
        self.env = storage_functions(self.directory)
        self.client = Mock()
        self.env.update(Client=self.client, Image=Image, ImageDraw=ImageDraw,
                        ImageOps=ImageOps, JobTimeout=JobTimeout, get_token=lambda: None)

    def test_downloaded_image_is_labelled_png(self):
        self.client.return_value.submit.return_value.result.return_value = ({"path": str(self.source)}, 42)
        data, seed = self.env["generate_image"]("Fictional scene", 42)
        with Image.open(BytesIO(data)) as picture:
            self.assertEqual(picture.format, "PNG")
            self.assertEqual(picture.size, (768, 816))
            self.assertEqual(picture.getpixel((0, 0))[0], picture.getpixel((0, 0))[1])
        self.assertEqual(seed, 42)
        self.client.return_value.submit.assert_called_once()

    def test_timeout_cancels_job(self):
        self.client.return_value.submit.return_value.result.side_effect = JobTimeout()
        with self.assertRaises(TimeoutError):
            self.env["generate_image"]("Fictional scene", 42)
        self.client.return_value.submit.return_value.cancel.assert_called_once()

    def test_missing_download_is_not_saved_as_image(self):
        self.client.return_value.submit.return_value.result.return_value = ({"path": None}, 42)
        with self.assertRaises(RuntimeError):
            self.env["generate_image"]("Fictional scene", 42)

if __name__ == "__main__":
    unittest.main()
