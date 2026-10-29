import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from reference_backend import prepare_references,run_reference,REFERENCE_MODEL
from fal_backend import ProviderError,PendingJob,job_path

class ReferenceBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1],ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.refs=[{"feature":"Eyes","instruction":"Shape only; narrower", "image_bytes":b"mock-png-one","crop_percent":{"horizontal":[0,100],"vertical":[0,100]}}]
        self.prompt,self.urls,self.plan=prepare_references("Fictional sketch",self.refs)

    def transport(self):
        return Mock(side_effect=[{"request_id":"ref-1","status_url":"https://queue.fal.run/status/ref-1","response_url":"https://queue.fal.run/result/ref-1"},{"status":"COMPLETED"},{"images":[{"url":"https://v3.fal.media/result.png"}],"seed":42}])

    def test_no_authorization_or_key_performs_no_network(self):
        network=Mock()
        for key,allowed in [("secret",False),("",True)]:
            with self.assertRaises(ProviderError):run_reference(self.prompt,42,key,self.root,"case",references=self.refs,authorized=allowed,transport=network)
        network.assert_not_called()

    def test_payload_uses_crops_and_correct_model_without_storing_bytes_or_key(self):
        network=self.transport()
        result=run_reference(self.prompt,42,"private-key",self.root,"case",references=self.refs,authorized=True,transport=network,downloader=lambda _:b"result")
        self.assertEqual(result,(b"result",42,"ref-1"))
        args=network.call_args_list[0].args
        self.assertEqual(args[0],"https://queue.fal.run/"+REFERENCE_MODEL)
        self.assertTrue(args[2]["image_urls"][0].startswith("data:image/png;base64,"))
        self.assertNotIn("num_inference_steps",args[2])
        self.assertIn("narrower",args[2]["prompt"])
        saved=job_path(self.root,"case",self.prompt,42,REFERENCE_MODEL).read_text()
        self.assertNotIn("private-key",saved)
        self.assertNotIn("data:image",saved)

    def test_reference_changes_change_request_identity(self):
        changed=[dict(self.refs[0],image_bytes=b"different")]
        other,_,_=prepare_references("Fictional sketch",changed)
        self.assertNotEqual(self.prompt,other)
        self.assertNotEqual(job_path(self.root,"case",self.prompt,42,REFERENCE_MODEL),job_path(self.root,"case",other,42,REFERENCE_MODEL))

    def test_missing_instruction_rejected_before_submission(self):
        network=Mock()
        with self.assertRaises(ProviderError):run_reference(self.prompt,42,"key",self.root,"case",references=[dict(self.refs[0],instruction="")],authorized=True,transport=network)
        network.assert_not_called()

    def test_pending_recovery_never_posts_again(self):
        network=Mock(side_effect=[{"request_id":"ref-1","status_url":"https://queue.fal.run/status/ref-1","response_url":"https://queue.fal.run/result/ref-1"},{"status":"IN_PROGRESS"}])
        with self.assertRaises(PendingJob):run_reference(self.prompt,42,"key",self.root,"case",references=self.refs,authorized=True,transport=network,max_wait=0)
        resumed=Mock(side_effect=[{"status":"COMPLETED"},{"images":[{"url":"https://v3.fal.media/result.png"}],"seed":42}])
        run_reference(self.prompt,42,"key",self.root,"case",authorized=True,transport=resumed,downloader=lambda _:b"result")
        self.assertTrue(all(len(c.args)==2 for c in resumed.call_args_list))

    def test_unknown_submission_is_not_automatically_retried(self):
        network=Mock(side_effect=TimeoutError())
        with self.assertRaises(PendingJob):run_reference(self.prompt,42,"key",self.root,"case",references=self.refs,authorized=True,transport=network)
        network.reset_mock()
        with self.assertRaises(PendingJob):run_reference(self.prompt,42,"key",self.root,"case",references=self.refs,authorized=True,transport=network)
        network.assert_not_called()
