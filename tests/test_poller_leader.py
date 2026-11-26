"""Ведущий поллер: из всех воркеров Langflow опрашивает только один.

Приложение запускает N воркеров uvicorn, каждый выполняет lifespan и создаёт
свой поллер. Раньше это давало N-кратный опрос Langflow и БД на каждую
активную задачу. Теперь право опрашивать выдаёт advisory-лок Postgres:
он привязан к соединению и снимается сам, если процесс упал.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import app


class _Stop(Exception):
    """Прерывает бесконечный цикл поллера в тесте."""


class FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class FakeConn:
    def __init__(self, acquires=True, alive=True, tag="conn"):
        self.acquires = acquires
        self.alive = alive
        self.closed = False
        self.unlocked = False
        self.tag = tag

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        if "pg_try_advisory_lock" in sql:
            return FakeResult(self.acquires)
        if "pg_advisory_unlock" in sql:
            self.unlocked = True
            return FakeResult(True)
        if "SELECT 1" in sql:
            if not self.alive:
                raise RuntimeError("connection lost")
            return FakeResult(1)
        return FakeResult(None)

    async def close(self):
        self.closed = True


class FakeEngine:
    def __init__(self, conns):
        self._conns = list(conns)
        self.connections = 0

    async def connect(self):
        self.connections += 1
        if not self._conns:
            raise RuntimeError("no more connections")
        return self._conns.pop(0)


class LeaderLockTests(unittest.TestCase):
    def test_first_process_becomes_leader(self):
        conn = FakeConn(acquires=True)
        lock = app._LeaderLock(1, "тест", engine=FakeEngine([conn]))

        self.assertTrue(asyncio.run(lock.try_acquire()))
        self.assertTrue(lock.held)

    def test_second_process_backs_off_and_closes_connection(self):
        # Лок занят другим воркером: соединение не нужно, его надо закрыть,
        # иначе оно висит в пуле просто так.
        conn = FakeConn(acquires=False)
        lock = app._LeaderLock(1, "тест", engine=FakeEngine([conn]))

        self.assertFalse(asyncio.run(lock.try_acquire()))
        self.assertFalse(lock.held)
        self.assertTrue(conn.closed)

    def test_ensure_keeps_lock_while_connection_alive(self):
        conn = FakeConn(acquires=True)
        engine = FakeEngine([conn])
        lock = app._LeaderLock(1, "тест", engine=engine)

        asyncio.run(lock.try_acquire())
        self.assertTrue(asyncio.run(lock.ensure()))
        # Второй попытки подключения не было — лок не переспрашивали.
        self.assertEqual(engine.connections, 1)

    def test_ensure_reacquires_after_connection_loss(self):
        # Соединение оборвалось. Postgres снял лок сам — нужно заново
        # участвовать в выборах, иначе опрос остановится навсегда.
        dead = FakeConn(acquires=True, alive=False)
        fresh = FakeConn(acquires=True)
        lock = app._LeaderLock(1, "тест", engine=FakeEngine([dead, fresh]))

        asyncio.run(lock.try_acquire())
        self.assertTrue(asyncio.run(lock.ensure()))
        self.assertTrue(dead.closed)
        self.assertTrue(lock.held)

    def test_release_unlocks_and_closes(self):
        conn = FakeConn(acquires=True)
        lock = app._LeaderLock(1, "тест", engine=FakeEngine([conn]))

        asyncio.run(lock.try_acquire())
        asyncio.run(lock.release())

        self.assertTrue(conn.unlocked)
        self.assertTrue(conn.closed)
        self.assertFalse(lock.held)

    def test_no_database_means_no_leadership(self):
        class BrokenEngine:
            async def connect(self):
                raise RuntimeError("БД недоступна")

        lock = app._LeaderLock(1, "тест", engine=BrokenEngine())

        self.assertFalse(asyncio.run(lock.try_acquire()))
        self.assertFalse(lock.held)


class PollerBackoffTests(unittest.TestCase):
    """Не-ведущий воркер не должен ходить ни в Langflow, ни в таблицы задач."""

    def test_non_leader_skips_polling(self):
        non_leader = AsyncMock()
        non_leader.ensure = AsyncMock(return_value=False)
        non_leader.release = AsyncMock()
        once = AsyncMock()

        async def stop(_seconds):
            raise _Stop()

        async def scenario():
            with patch.object(app, "_LeaderLock", return_value=non_leader), \
                 patch.object(app, "_poll_jobs_once", new=once), \
                 patch.object(app, "_pause", stop):
                try:
                    await app._poll_jobs()
                except _Stop:
                    pass

        asyncio.run(scenario())

        once.assert_not_called()
        non_leader.release.assert_awaited()

    def test_leader_polls_and_releases_lock_on_shutdown(self):
        leader = AsyncMock()
        leader.ensure = AsyncMock(return_value=True)
        leader.release = AsyncMock()
        calls = []

        async def body():
            calls.append(1)

        async def stop(_seconds):
            raise _Stop()

        async def scenario():
            with patch.object(app, "_LeaderLock", return_value=leader), \
                 patch.object(app, "_poll_jobs_once", new=body), \
                 patch.object(app, "_pause", stop):
                try:
                    await app._poll_jobs()
                except _Stop:
                    pass

        asyncio.run(scenario())

        self.assertEqual(calls, [1])
        # Штатный останов обязан отдать лок, иначе следующий ведущий
        # ждал бы, пока закроется соединение.
        leader.release.assert_awaited()

    def test_purge_poller_is_guarded_too(self):
        non_leader = AsyncMock()
        non_leader.ensure = AsyncMock(return_value=False)
        non_leader.release = AsyncMock()
        purge = AsyncMock()

        async def stop(_seconds):
            raise _Stop()

        async def scenario():
            with patch.object(app, "_LeaderLock", return_value=non_leader), \
                 patch.object(app.chat_purge, "purge_pending", new=purge), \
                 patch.object(app, "_pause", stop):
                try:
                    await app._poll_chat_purges()
                except _Stop:
                    pass

        asyncio.run(scenario())

        purge.assert_not_called()


if __name__ == "__main__":
    unittest.main()