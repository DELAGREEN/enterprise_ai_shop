"""Удаление чата: мягкое у себя + физическая чистка памяти в Langflow."""

import asyncio
import unittest
from datetime import datetime

from sqlalchemy import select

from chat_purge import MAX_ATTEMPTS, purge_deleted_chat
from models import Chat, ChatMessage
from routers.jobs import save_job_history


class FakeChat:
    """Минимальный Chat: purge_deleted_chat трогает только эти поля."""

    def __init__(self, chat_id="chat-1", deleted_at=None):
        self.id = chat_id
        self.deleted_at = deleted_at
        self.langflow_purged_at = None
        self.purge_attempts = 0
        self.purge_error = None


class FakeDb:
    def __init__(self, chat=None):
        self.chat = chat
        self.added = []
        self.committed = 0

    async def get(self, model, key):
        return self.chat

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed += 1


class PurgeDeletedChatTests(unittest.TestCase):
    def setUp(self):
        self._orig_session = __import__("chat_purge").purge_chat_session

    def tearDown(self):
        __import__("chat_purge").purge_chat_session = self._orig_session

    def _patch(self, ok, error=""):
        calls = []

        async def fake(chat_id):
            calls.append(chat_id)
            return ok, error

        __import__("chat_purge").purge_chat_session = fake
        return calls

    def test_success_marks_purged(self):
        calls = self._patch(True)
        chat = FakeChat()

        result = asyncio.run(purge_deleted_chat(FakeDb(), chat))

        self.assertTrue(result)
        self.assertEqual(calls, ["chat-1"])       # session_id == chats.id
        self.assertIsNotNone(chat.langflow_purged_at)
        self.assertEqual(chat.purge_attempts, 0)
        self.assertIsNone(chat.purge_error)

    def test_failure_increments_attempts_and_keeps_timestamp_empty(self):
        self._patch(False, "в Langflow осталось сообщений: 4")
        chat = FakeChat()

        result = asyncio.run(purge_deleted_chat(FakeDb(), chat))

        self.assertFalse(result)
        self.assertIsNone(chat.langflow_purged_at)
        self.assertEqual(chat.purge_attempts, 1)
        self.assertIn("осталось сообщений", chat.purge_error)

    def test_repeated_failures_accumulate(self):
        self._patch(False, "timeout")
        chat = FakeChat()

        for _ in range(MAX_ATTEMPTS):
            asyncio.run(purge_deleted_chat(FakeDb(), chat))

        self.assertEqual(chat.purge_attempts, MAX_ATTEMPTS)
        self.assertIsNone(chat.langflow_purged_at)

    def test_error_is_truncated(self):
        self._patch(False, "x" * 5000)
        chat = FakeChat()

        asyncio.run(purge_deleted_chat(FakeDb(), chat))

        self.assertEqual(len(chat.purge_error), 1000)


class SaveJobHistoryForDeletedChatTests(unittest.TestCase):
    def _job(self):
        from tests.test_job_status import FakeJob

        job = FakeJob(status="completed")
        job.chat_id = "chat-1"
        job.result = "Готово"
        return job

    def test_deleted_chat_gets_no_messages(self):
        chat = Chat(
            id="chat-1",
            flow_id="flow-1",
            user_id="testuser",
            deleted_at=datetime.utcnow(),
        )
        chat.langflow_purged_at = datetime.utcnow()
        db = FakeDb(chat)
        job = self._job()

        changed = asyncio.run(save_job_history(db, job))

        self.assertTrue(changed)
        self.assertTrue(job.history_saved)
        self.assertEqual(db.added, [])                    # ни ответа, ни лога
        self.assertIsNone(chat.langflow_purged_at)        # память вычистим снова

    def test_live_chat_still_gets_messages(self):
        chat = Chat(id="chat-1", flow_id="flow-1", user_id="testuser")
        db = FakeDb(chat)
        job = self._job()

        asyncio.run(save_job_history(db, job))

        self.assertEqual(len(db.added), 2)                # ответ + LLMRequestLog
        self.assertIsInstance(db.added[0], ChatMessage)


if __name__ == "__main__":
    unittest.main()
