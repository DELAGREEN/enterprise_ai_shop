import unittest

from routers.jobs import EMPTY_RESULT_HINT, apply_job_status


class FakeJob:
    def __init__(self, status="running"):
        self.status = status
        self.result = None
        self.error = None
        self.finished_at = None


class ApplyJobStatusTests(unittest.TestCase):
    def test_completed_with_text_stores_result(self):
        job = FakeJob()
        changed = apply_job_status(job, {
            "status": "completed",
            "output": {"reason": "single", "text": "Готово"},
        })

        self.assertTrue(changed)
        self.assertEqual(job.status, "completed")
        self.assertEqual(job.result, "Готово")
        self.assertIsNone(job.error)
        self.assertIsNotNone(job.finished_at)

    def test_completed_without_text_is_failure(self):
        job = FakeJob()
        job.result = "старый мусор"
        apply_job_status(job, {
            "status": "completed",
            "output": {"reason": "non_string", "text": None},
            "outputs": {"ChatOutput-1": {"content": None}},
            "errors": [],
        })

        self.assertEqual(job.status, "failed")
        self.assertEqual(job.error, EMPTY_RESULT_HINT)
        self.assertIsNone(job.result)
        self.assertIsNotNone(job.finished_at)

    def test_timeout_stores_error(self):
        job = FakeJob()
        apply_job_status(job, {
            "status": "timeout",
            "errors": [{"error": "Workflow execution timed out."}],
        })

        self.assertEqual(job.status, "timeout")
        self.assertEqual(job.error, "Workflow execution timed out.")

    def test_bare_timeout_message_gets_hint(self):
        job = FakeJob()
        apply_job_status(job, {
            "status": "failed",
            "errors": [{"error": "timed out"}],
        })

        self.assertEqual(job.status, "failed")
        self.assertIn("Таймаут", job.error)
        self.assertIn("timed out", job.error)

    def test_in_progress_keeps_running(self):
        job = FakeJob()
        apply_job_status(job, {"status": "in_progress", "errors": []})

        self.assertEqual(job.status, "running")
        self.assertIsNone(job.finished_at)

    def test_no_changes_returns_false(self):
        job = FakeJob(status="completed")
        job.result = "Готово"
        from datetime import datetime
        job.finished_at = datetime.utcnow()

        changed = apply_job_status(job, {
            "status": "completed",
            "output": {"reason": "single", "text": "Готово"},
        })

        self.assertFalse(changed)


if __name__ == "__main__":
    unittest.main()
