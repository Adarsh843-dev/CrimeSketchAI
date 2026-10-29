
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tempfile
import unittest
from fal_backend import run_fal, list_pending, validate_queue_url, download_image, ProviderError, PendingJob, RejectedSubmission

class FalBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1], ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.submissions = 0
        self.queued = False

    def transport(self, url, key, payload=None):
        if payload is not None:
            self.submissions += 1
            self.assertTrue(payload["enable_safety_checker"])
            return {"request_id": "mock-request", "status_url": "https://queue.fal.run/mock/status",
                    "response_url": "https://queue.fal.run/mock/result"}
        if url.endswith("/status"):
            return {"status": "IN_QUEUE" if self.queued else "COMPLETED"}
        return {"images": [{"url": "https://v3.fal.media/mock.png"}], "seed": 42, "has_nsfw_concepts": [False]}

    def run_request(self, **kwargs):
        return run_fal("fictional scene", 42, "private-test-key", self.root, "case",
                       transport=self.transport, downloader=lambda url: b"mock image", **kwargs)

    def test_accepted_request_is_not_submitted_twice(self):
        self.assertEqual(self.run_request()[0], b"mock image")
        self.assertEqual(self.run_request()[0], b"mock image")
        self.assertEqual(self.submissions, 1)
        self.assertEqual(list_pending(self.root, "case"), [])
        for path in self.root.rglob("*.json"):
            self.assertNotIn("private-test-key", path.read_text())

    def test_queued_request_can_resume_without_another_submission(self):
        self.queued = True
        with self.assertRaises(PendingJob):
            self.run_request(max_wait=0)
        self.assertEqual(len(list_pending(self.root, "case")), 1)
        self.queued = False
        self.run_request()
        self.assertEqual(self.submissions, 1)

    def test_uncertain_submission_is_not_blindly_retried(self):
        calls = []
        def lost_response(url, key, payload=None):
            calls.append(url)
            raise OSError("lost response")
        for attempt in range(2):
            with self.assertRaises(PendingJob):
                run_fal("uncertain scene", 1, "private-test-key", self.root, "case",
                        transport=lost_response)
        self.assertEqual(len(calls), 1)

    def test_rejected_submission_can_retry_after_account_fix(self):
        def reject(url, key, payload=None):
            raise RejectedSubmission("fal account has insufficient funds.")
        with self.assertRaises(RejectedSubmission):
            run_fal("fictional scene", 42, "private-test-key", self.root, "case", transport=reject)
        self.run_request()
        self.assertEqual(self.submissions, 1)

    def test_missing_key_blocks_before_network(self):
        with self.assertRaises(ProviderError):
            run_fal("scene", 1, "", self.root, "case", transport=self.transport)
        self.assertEqual(self.submissions, 0)

    def test_unexpected_hosts_are_rejected_without_network(self):
        for url in ("http://queue.fal.run/test", "https://evil.example/test", "https://queue.fal.run.evil.example/test"):
            with self.assertRaises(ProviderError):
                validate_queue_url(url)
        with self.assertRaises(ProviderError):
            download_image("https://evil.example/private.png")

if __name__ == "__main__":
    unittest.main()
