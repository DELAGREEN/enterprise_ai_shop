"""Восстановление индикатора прогресса после перезагрузки страницы.

Прогресс рисуется в браузере, поэтому при F5 он исчезал: задача продолжает
идти, а показать её нечем. ``GET /api/jobs/active`` отдаёт незавершённую
задачу, и страница снова подписывается на опрос.
"""

import unittest
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from database import get_db
from routers.jobs import router as jobs_router
from session import get_current_user


class FakeJob:
    """Только те поля, которые читает active_job/_progress_of."""

    def __init__(self, status="running"):
        self.id = "job-1"
        self.flow_id = "flow-1"
        self.chat_id = "chat-1"
        self.status = status
        self.progress_stage = "parsing"
        self.progress_done = 129
        self.progress_total = 673
        self.progress_message = None
        self.progress_eta_seconds = 360
        self.progress_updated_at = datetime(2026, 11, 26, 7, 13, 4)
        self.created_at = datetime(2026, 11, 26, 7, 0, 0)


class FakeResult:
    def __init__(self, job):
        self._job = job

    def scalars(self):
        return self

    def first(self):
        return self._job


class FakeDb:
    def __init__(self, job=None):
        self.job = job

    async def execute(self, stmt):
        return FakeResult(self.job)


def build_client(job=None) -> TestClient:
    app = FastAPI()
    app.include_router(jobs_router)

    async def override_db():
        yield FakeDb(job)

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "username": "testuser",
        "is_admin": False,
    }
    return TestClient(app)


class ActiveJobTests(unittest.TestCase):
    def test_returns_none_when_nothing_runs(self):
        response = build_client(None).get("/api/jobs/active?chat_id=chat-1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"job": None})

    def test_returns_running_job_with_progress(self):
        response = build_client(FakeJob()).get("/api/jobs/active?chat_id=chat-1")

        self.assertEqual(response.status_code, 200)
        job = response.json()["job"]
        self.assertEqual(job["job_id"], "job-1")
        self.assertEqual(job["status"], "running")
        self.assertEqual(job["progress"]["percent"], 19)
        self.assertEqual(job["progress"]["eta_at"], "2026-11-26T07:19:04+00:00")

    def test_active_is_not_treated_as_job_id(self):
        # Маршрут объявлен до /jobs/{job_id}: иначе FastAPI примет «active»
        # за идентификатор задачи и вернёт 404 «Job not found».
        response = build_client(FakeJob()).get("/api/jobs/active?chat_id=chat-1")

        self.assertNotEqual(response.status_code, 404)
        self.assertIn("job", response.json())

    def test_timestamps_carry_utc_offset(self):
        job = build_client(FakeJob()).get(
            "/api/jobs/active?chat_id=chat-1"
        ).json()["job"]

        self.assertTrue(job["created_at"].endswith("+00:00"))
        self.assertTrue(job["progress"]["updated_at"].endswith("+00:00"))


if __name__ == "__main__":
    unittest.main()
