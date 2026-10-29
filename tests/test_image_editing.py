import base64
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from PIL import Image
from image_editing import (label_picture,source_picture,local_edit,run_image_edit,resume_image_edit,EDIT_MODEL,region_box)
from fal_backend import ProviderError,PendingJob,job_path

class ImageEditingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1],ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.base={"id":"saved-source","case_id":"case","description":"Fictional sketch","mode":"Face sketch","seed":42,"image":label_picture(Image.new("RGB",(100,80),(120,130,140)))}
        self.network=Mock(side_effect=[{"request_id":"edit-1","status_url":"https://queue.fal.run/status/edit-1","response_url":"https://queue.fal.run/result/edit-1"},{"status":"COMPLETED"},{"images":[{"url":"https://v3.fal.media/output.png"}],"seed":42}])
        buffer=BytesIO();Image.new("RGB",(100,80),"black").save(buffer,format="PNG");self.output=buffer.getvalue()

    def test_paid_payload_contains_the_actual_saved_image_without_footer(self):
        result=run_image_edit(self.base,"Thinner eyebrows",42,"private-key",self.temp.name,"case",authorized=True,transport=self.network,downloader=lambda _:self.output)
        args=self.network.call_args_list[0].args
        self.assertEqual(args[0],"https://queue.fal.run/"+EDIT_MODEL)
        input_image=Image.open(BytesIO(base64.b64decode(args[2]["image_url"].split(",",1)[1])))
        self.assertEqual(input_image.size,(100,80))
        self.assertEqual(input_image.getpixel((0,0)),(120,130,140))
        self.assertEqual(result["memory_snapshot"]["image_edit"]["source_revision"],self.base["id"])
        self.assertEqual(Image.open(BytesIO(result["image"])).size,(100,128))
        saved=next((Path(self.temp.name)/"generation_jobs").glob("*.json")).read_text()
        self.assertNotIn("private-key",saved)
        self.assertNotIn("data:image",saved)

    def test_selected_rectangle_preserves_outside_pixels_exactly(self):
        result=run_image_edit(self.base,"Change mouth",42,"key",self.temp.name,"case",authorized=True,region={"horizontal":[20,70],"vertical":[30,60]},transport=self.network,downloader=lambda _:self.output)
        picture=source_picture(result["image"])
        self.assertEqual(picture.getpixel((0,0)),(120,130,140))
        self.assertEqual(picture.getpixel((30,30)),(0,0,0))
        self.assertEqual(picture.getpixel((71,30)),(120,130,140))
        self.assertEqual(picture.getpixel((50,49)),(120,130,140))

    def test_local_adjustment_is_new_image_and_source_remains_unchanged(self):
        before=self.base["image"]
        result=local_edit(self.base,1.2,1.0,1.0)
        self.assertNotEqual(result["image"],before)
        self.assertEqual(self.base["image"],before)
        self.assertEqual(result["provider"],"Local image adjustments")
        self.assertEqual(Image.open(BytesIO(result["image"])).size,(100,128))
        with self.assertRaises(ValueError):local_edit(self.base,1,1,1)

    def test_no_permission_no_network_and_invalid_crop_rejected(self):
        network=Mock()
        with self.assertRaises(ProviderError):run_image_edit(self.base,"Edit",42,"key",self.temp.name,"case",transport=network)
        with self.assertRaises(ValueError):run_image_edit(self.base,"Edit",42,"key",self.temp.name,"case",authorized=True,region={"horizontal":[0,0],"vertical":[0,100]},transport=network)
        with self.assertRaises(ValueError):run_image_edit(self.base,"",42,"key",self.temp.name,"case",authorized=True,transport=network)
        network.assert_not_called()

    def test_resume_checks_same_source_and_never_resubmits(self):
        network=Mock(side_effect=[{"request_id":"edit-1","status_url":"https://queue.fal.run/status/edit-1","response_url":"https://queue.fal.run/result/edit-1"},{"status":"IN_PROGRESS"}])
        with self.assertRaises(PendingJob):run_image_edit(self.base,"Thinner eyebrows",42,"key",self.temp.name,"case",authorized=True,transport=network,max_wait=0)
        import json
        job=json.loads(next((Path(self.temp.name)/"generation_jobs").glob("*.json")).read_text())
        resumed=Mock(side_effect=[{"status":"COMPLETED"},{"images":[{"url":"https://v3.fal.media/output.png"}],"seed":42}])
        with self.assertRaises(ProviderError):resume_image_edit(job,dict(self.base,id="another"),"key",self.temp.name,"case",authorized=True,transport=resumed)
        resumed.assert_not_called()
        result=resume_image_edit(job,self.base,"key",self.temp.name,"case",authorized=True,transport=resumed,downloader=lambda _:self.output)
        self.assertEqual(result["request_id"],"edit-1")
        self.assertTrue(all(len(c.args)==2 for c in resumed.call_args_list))
