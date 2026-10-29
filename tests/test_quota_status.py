
from pathlib import Path
from datetime import datetime, timezone
import tempfile
import unittest
from test_workspace import storage_functions, APP

class QuotaStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=APP.parent, ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.env = storage_functions(self.temp.name)
        self.env["init_db"]()

    def test_parses_provider_duration_formats(self):
        examples = [
            ("ZeroGPU quota exceeded. Try again in 0:05:00.", 300),
            ("Quota exceeded. Try again in 1 day, 2:03:04.", 93784),
            ("Rate limit. Retry after 120 seconds.", 120),
            ("Quota reached. Retry in 2 hours 3 minutes.", 7380),
        ]
        for message, expected in examples:
            self.assertEqual(self.env["parse_quota_report"](message)["retry_seconds"], expected)

    def test_zero_or_missing_estimate_remains_unknown(self):
        for text in ("ZeroGPU quota. Try again in 0:00:00.", "Quota reached."):
            self.env["record_quota_failure"]("HF", RuntimeError(text), observed=1000)
            self.assertEqual(self.env["get_quota_status"]("HF", 1100)["status"], "reset_unknown")

    def test_countdown_persists_and_expiry_does_not_claim_success(self):
        self.env["record_quota_failure"]("HF", RuntimeError("Quota. Try again in 0:05:00."), observed=1000)
        restarted = storage_functions(self.temp.name)
        restarted["init_db"]()
        status = restarted["get_quota_status"]("HF", 1100)
        self.assertEqual(status["wait_seconds"], 200)
        self.assertEqual(status["status"], "estimated_wait")
        self.assertEqual(restarted["get_quota_status"]("HF", 1300)["status"], "estimate_elapsed")

    def test_allowance_numbers_and_provider_isolation(self):
        self.env["record_quota_failure"]("HF",
            RuntimeError("You have exceeded your ZeroGPU quota (90s requested vs. 0s left). Try again in 0:10:00."),
            observed=1000)
        status = self.env["get_quota_status"]("HF", 1000)
        self.assertEqual(status["requested_seconds"], 90)
        self.assertEqual(status["remaining_seconds"], 0)
        self.assertEqual(self.env["get_quota_status"]("fal", 1000)["status"], "not_reported")

    def test_success_clears_only_provider_status(self):
        self.env["record_quota_failure"]("HF", RuntimeError("Quota. Try again in 0:05:00."), observed=1000)
        self.env["record_generation_success"]("HF", observed=1200)
        status = self.env["get_quota_status"]("HF", 1200)
        self.assertEqual(status["status"], "last_succeeded")
        self.assertIsNone(status["retry_at"])

    def test_non_quota_errors_are_not_saved_and_secrets_are_not_stored(self):
        self.assertFalse(self.env["record_quota_failure"]("HF", RuntimeError("max_sequence length exceeded")))
        self.env["record_quota_failure"]("HF", RuntimeError("quota hf_private_secret. Try again in 0:05:00."), observed=1000)
        self.assertNotIn(b"hf_private_secret", self.env["database_backup"]())
        self.env["clear_quota_notice"]("HF")
        self.assertEqual(self.env["get_quota_status"]("HF")["status"], "not_reported")

    def test_ist_time_handles_next_day(self):
        timestamp = datetime(2026,10,28,20,0,tzinfo=timezone.utc).timestamp()
        self.assertIn("29 Oct 2026, 01:30:00 AM IST", self.env["format_quota_time"](timestamp))
        self.assertEqual(self.env["format_wait"](3661), "01:01:01")

if __name__ == "__main__":
    unittest.main()
