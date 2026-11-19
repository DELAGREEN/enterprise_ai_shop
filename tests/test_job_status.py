import asyncio
import json
import unittest
from datetime import datetime

from models import Chat, ChatMessage, LLMRequestLog
from routers.jobs import (
    EMPTY_RESULT_HINT,
    apply_job_status,
    build_input_value,
    save_job_history,
)


class FakeJob:
    def __init__(self, status="running"):
        self.id = "job-1"
        self.status = status
        self.result = None
        self.error = None
        self.finished_at = None
        self.chat_id = None
        self.user_id = "testuser"
        self.flow_id = "flow-1"
        self.input_text = None
        self.input_files = None
        self.history_saved = False


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


class FakeDb:
    def __init__(self, chat=None):
        self.chat = chat
        self.added = []

    async def get(self, model, key):
        return self.chat if model is Chat else None

    def add(self, obj):
        self.added.append(obj)


class SaveJobHistoryTests(unittest.TestCase):
    def _job(self, status="completed", result="Готово", chat_id="chat-1"):
        job = FakeJob(status=status)
        job.chat_id = chat_id
        job.result = result
        job.input_text = "Обработай файл"
        return job

    def test_writes_assistant_message_and_log(self):
        chat = Chat(id="chat-1", flow_id="flow-1", user_id="testuser", title="Тест")
        db = FakeDb(chat)
        job = self._job()

        changed = asyncio.run(save_job_history(db, job))

        self.assertTrue(changed)
        self.assertTrue(job.history_saved)
        self.assertEqual(len(db.added), 2)

        message, log = db.added
        self.assertIsInstance(message, ChatMessage)
        self.assertEqual(message.role, "assistant")
        self.assertEqual(message.content, "Готово")

        self.assertIsInstance(log, LLMRequestLog)
        self.assertEqual(log.question, "Обработай файл")
        self.assertEqual(log.answer, "Готово")
        self.assertEqual(log.chat_id, "chat-1")

    def test_does_not_write_twice(self):
        chat = Chat(id="chat-1", flow_id="flow-1", user_id="testuser", title="Тест")
        db = FakeDb(chat)
        job = self._job()
        job.history_saved = True

        self.assertFalse(asyncio.run(save_job_history(db, job)))
        self.assertEqual(db.added, [])

    def test_skips_jobs_without_chat(self):
        db = FakeDb(None)
        job = self._job(chat_id=None)

        self.assertFalse(asyncio.run(save_job_history(db, job)))
        self.assertEqual(db.added, [])

    def test_skips_running_jobs(self):
        db = FakeDb(Chat(id="chat-1", flow_id="flow-1", user_id="u", title="t"))
        job = self._job(status="running")

        self.assertFalse(asyncio.run(save_job_history(db, job)))
        self.assertEqual(db.added, [])

    def test_failed_job_stores_error_message(self):
        db = FakeDb(Chat(id="chat-1", flow_id="flow-1", user_id="u", title="t"))
        job = self._job(status="timeout", result=None)
        job.error = "Таймаут при обращении к внешнему сервису"

        self.assertTrue(asyncio.run(save_job_history(db, job)))

        message = db.added[0]
        self.assertEqual(message.role, "assistant")
        self.assertIn("Таймаут", message.content)

    def test_question_falls_back_to_file_names(self):
        db = FakeDb(Chat(id="chat-1", flow_id="flow-1", user_id="u", title="t"))
        job = self._job()
        job.input_text = None
        job.input_files = json.dumps([{"name": "gost.docx", "path": "x/y.docx"}])

        asyncio.run(save_job_history(db, job))

        log = db.added[1]
        self.assertEqual(log.question, "📎 gost.docx")


class BuildInputValueTests(unittest.TestCase):
    FILES = [{"name": "gost.docx", "path": "u/gost.docx"}]

    def test_text_only(self):
        self.assertEqual(build_input_value("Привет", []), "Привет")

    def test_files_only(self):
        value = build_input_value(None, self.FILES)
        self.assertEqual(value, "Проанализируй файлы: gost.docx")

    def test_text_and_files_prefers_text(self):
        self.assertEqual(build_input_value("Что тут?", self.FILES), "Что тут?")

    def test_blank_text_falls_back_to_files(self):
        self.assertEqual(
            build_input_value("   \n ", self.FILES),
            "Проанализируй файлы: gost.docx",
        )

    def test_nothing_to_send(self):
        self.assertEqual(build_input_value(None, []), "")
        self.assertEqual(build_input_value("   ", []), "")


if __name__ == "__main__":
    unittest.main()
