from io import BytesIO
import unittest
from PIL import Image
from reference_board import load_reference, crop_reference

class ReferenceTests(unittest.TestCase):
    def test_decodes_and_crops_without_altering_source(self):
        source=Image.new("RGB",(200,100),"white")
        buffer=BytesIO(); source.save(buffer,format="PNG")
        image=load_reference(buffer.getvalue())
        crop=crop_reference(image,(25,75),(10,90))
        self.assertEqual(crop.size,(100,80))
        self.assertEqual(image.size,(200,100))

    def test_rejects_invalid_image_and_large_upload(self):
        for raw in (b"not an image",b"x"*(5*1024*1024+1)):
            with self.assertRaises(ValueError):load_reference(raw)

    def test_zero_area_crop_rejected(self):
        with self.assertRaises(ValueError):crop_reference(Image.new("RGB",(20,20)),(10,10),(0,100))

    def test_large_dimensions_rejected(self):
        image=Image.new("1",(4001,4000))
        buffer=BytesIO();image.save(buffer,format="PNG")
        with self.assertRaises(ValueError):load_reference(buffer.getvalue())

    def test_preview_has_no_generation_dependency(self):
        import ast,inspect,reference_board
        tree=ast.parse(inspect.getsource(reference_board))
        imports=[node.module for node in ast.walk(tree) if isinstance(node,ast.ImportFrom)]
        self.assertNotIn("fal_backend",imports)
        self.assertNotIn("gradio_client",imports)
