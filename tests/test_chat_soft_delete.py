"""Мягкое удаление чата: переписка остаётся в БД, память в Langflow чистится."""

import asyncio
import unittest
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import Delete, Select, Update
from starlette.requests import Request

import chat_purge
from routers.chat import _get_chat_or_404, _get_user_chats, chat_delete
from session import SESSION_COOKIE, create_session


def make_request(username="testuser") -> Request:
    token = create_session({"username": username, "is_admin": False})
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [(b"cookie", f"{SESSION_COOKIE}={token}".encode())],
    }
    return Request(scope)


class FakeResult:
    def __init__(self, items=None):
        self._items = items or []
        self.rowcount = 0

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None

    def scalars(self):
        return self

    def all(self):
        return list(self._items)


class FakeDb:
    """Эмулирует фильтр ``deleted_at IS NULL`` и запоминает все запросы."""

    def __init__(self, chats=None):
        self.chats = chats or []
        self.statements = []
        self.commits = 0

    async def execute(self, stmt):
        self.statements.append(stmt)
        if isinstance(stmt, Select):
            live = [c for c in self.chats if c.deleted_at is None]
            return FakeResult(live)
        return FakeResult()

    async def commit(self):
        self.commits += 1

    def kinds(self):
        return [type(s).__name__ for s in self.statements]


class SoftDeleteQueryTests(unittest.TestCase):
    def _sql(self, stmt) -> str:
        from sqlalchemy.dialects import postgresql

        return str(stmt.compile(dialect=postgresql.dialect()))

    def test_get_chat_or_404_ignores_deleted(self):
        chat = type("C", (), {})()
        chat.deleted_at = datetime.utcnow()
        db = FakeDb([chat])

        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(_get_chat_or_404(db, "chat-1", "flow-1", "testuser"))

        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIn("deleted_at IS NULL", self._sql(db.statements[0]))

    def test_get_chat_or_404_returns_live_chat(self):
        chat = type("C", (), {})()
        chat.deleted_at = None
        db = FakeDb([chat])

        result = asyncio.run(_get_chat_or_404(db, "chat-1", "flow-1", "testuser"))

        self.assertIs(result, chat)

    def test_listing_filters_deleted(self):
        db = FakeDb([])

        asyncio.run(_get_user_chats(db, "flow-1", "testuser"))

        self.assertIn("deleted_at IS NULL", self._sql(db.statements[0]))


class ChatDeleteTests(unittest.TestCase):
    def setUp(self):
        self._orig = chat_purge.purge_deleted_chat

    def tearDown(self):
        chat_purge.purge_deleted_chat = self._orig

    def _patch_purge(self, result=True):
        calls = []

        async def fake(db, chat):
            calls.append(chat.id)
            if result:
                chat.langflow_purged_at = datetime.utcnow()
            return result

        chat_purge.purge_deleted_chat = fake
        return calls

    def _chat(self):
        return type(
            "C",
            (),
            {
                "id": "chat-1",
                "flow_id": "flow-1",
                "user_id": "testuser",
                "title": "Тест",
                "deleted_at": None,
                "deleted_by": None,
                "langflow_purged_at": None,
                "purge_attempts": 0,
                "purge_error": None,
            },
        )()

    def test_marks_deleted_and_keeps_messages(self):
        calls = self._patch_purge(True)
        chat = self._chat()
        db = FakeDb([chat])

        result = asyncio.run(
            chat_delete("flow-1", "chat-1", make_request(), db)
        )

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["langflow_purged"])
        self.assertEqual(calls, ["chat-1"])
        self.assertIsNotNone(chat.deleted_at)
        self.assertEqual(chat.deleted_by, "testuser")
        # Переписка не удаляется: ни одного DELETE
        self.assertNotIn("Delete", db.kinds())
        self.assertIn("Update", db.kinds())     # активные задачи помечены

    def test_purge_failure_keeps_chat_deleted(self):
        self._patch_purge(False)
        chat = self._chat()
        db = FakeDb([chat])

        result = asyncio.run(
            chat_delete("flow-1", "chat-1", make_request(), db)
        )

        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["langflow_purged"])
        self.assertIsNotNone(chat.deleted_at)   # чат всё равно удалён

    def test_purge_exception_does_not_break_response(self):
        async def boom(db, chat):
            raise RuntimeError("Langflow недоступен")

        chat_purge.purge_deleted_chat = boom
        chat = self._chat()
        db = FakeDb([chat])

        result = asyncio.run(
            chat_delete("flow-1", "chat-1", make_request(), db)
        )

        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["langflow_purged"])
        self.assertIsNotNone(chat.deleted_at)


if __name__ == "__main__":
    unittest.main()
