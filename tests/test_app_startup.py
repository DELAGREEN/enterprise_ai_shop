import asyncio
import unittest

import app


class FakeConnection:
    def __init__(self):
        self.calls = []

    async def execute(self, stmt):
        self.calls.append(str(stmt))
        if len(self.calls) == 1:
            raise RuntimeError("first migration failed")


class FakeBeginContext:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False


class AppStartupMigrationTests(unittest.TestCase):
    def test_apply_migrations_skips_failed_statement_and_continues(self):
        original_engine = app.engine
        original_migrations = app.MIGRATIONS

        class FakeEngine:
            def __init__(self):
                self.conn = FakeConnection()

            def begin(self):
                return FakeBeginContext(self.conn)

        try:
            app.engine = FakeEngine()
            app.MIGRATIONS = ["first", "second"]
            asyncio.run(app._apply_migrations())
            self.assertEqual(len(app.engine.conn.calls), 2)
        finally:
            app.engine = original_engine
            app.MIGRATIONS = original_migrations


if __name__ == "__main__":
    unittest.main()
