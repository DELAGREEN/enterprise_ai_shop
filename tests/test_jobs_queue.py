"""Управление очередью ингеста: отмена, удаление и очистка зависших задач.

Зависшая задача — активная, но давно молчащая. Она держит воркер Langflow,
поэтому чистить её нужно в два шага: сначала остановить запуск в Langflow,
потом убрать запись. Если удалить только строку в БД, воркер продолжит
считать документ «в пустоту», и новые загрузки встанут в очередь.
"""

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from database import get_db
from routers.jobs import _is_stale, router as jobs_router
from session import get_current_user


def ago(minutes: int) -> datetime:
    return datetime.utcnow() - timedelta(minutes=minutes)


class FakeJob:
    def __init__(self, job_id="job-1", status="running", **kw):
        self.id = job_id
        self.flow_id = "flow-1"
        self.user_id = "testuser"
        self.chat_id = kw.get("chat_id")           # None → история не пишется
        self.status = status
        self.langflow_job_id = kw.get("langflow_job_id", "lf-1")
        self.result = None
        self.error = None
        self.history_saved = False
        self.progress_stage = kw.get("progress_stage")
        self.progress_done = kw.get("progress_done")
        self.progress_total = kw.get("progress_total")
        self.progress_message = None
        self.progress_eta_seconds = kw.get("progress_eta_seconds")
        self.progress_updated_at = kw.get("progress_updated_at")
        self.created_at = kw.get("created_at", ago(1))
        self.finished_at = None


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class FakeDb:
    def __init__(self, jobs=None):
        self.jobs = {j.id: j for j in (jobs or [])}
        self.deleted = []
        self.commits = 0
        self.added = []

    async def get(self, model, key):
        return self.jobs.get(key)

    async def execute(self, stmt):
        return FakeResult(list(self.jobs.values()))

    async def delete(self, obj):
        self.deleted.append(obj.id)
        self.jobs.pop(obj.id, None)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


def build_client(db, is_admin=True) -> TestClient:
    app = FastAPI()
    app.include_router(jobs_router)

    async def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "username": "testuser",
        "is_admin": is_admin,
    }
    return TestClient(app)


class StaleDetectionTests(unittest.TestCase):
    def test_running_job_with_fresh_progress_is_alive(self):
        job = FakeJob(progress_updated_at=ago(1), created_at=ago(60))

        self.assertFalse(_is_stale(job, minutes=30))

    def test_running_job_with_old_progress_is_stale(self):
        job = FakeJob(progress_updated_at=ago(180), created_at=ago(200))

        self.assertTrue(_is_stale(job, minutes=30))

    def test_queued_job_never_started_is_stale_by_created_at(self):
        # Задача от 18 ноября так и висит в очереди: прогресса нет вовсе,
        # поэтому точкой отсчёта становится время постановки.
        job = FakeJob(status="queued", created_at=ago(60 * 24 * 8))

        self.assertTrue(_is_stale(job, minutes=30))

    def test_queued_job_just_added_is_not_stale(self):
        job = FakeJob(status="queued", created_at=ago(2))

        self.assertFalse(_is_stale(job, minutes=30))

    def test_finished_jobs_are_never_stale(self):
        for status in ("completed", "failed", "timeout"):
            job = FakeJob(status=status, created_at=ago(60 * 24 * 30))
            self.assertFalse(_is_stale(job, minutes=30), status)


class CancelJobTests(unittest.TestCase):
    def test_cancel_stops_langflow_and_marks_failed(self):
        job = FakeJob()
        db = FakeDb([job])

        with patch("routers.jobs.LangflowClient.stop_job",
                   return_value={"stopped": True, "reason": "stopped"}) as stop:
            response = build_client(db).post("/api/jobs/job-1/cancel")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["cancelled"])
        self.assertEqual(body["status"], "failed")
        stop.assert_awaited_once_with("lf-1")
        self.assertEqual(job.status, "failed")
        self.assertIn("Отменено администратором", job.error)
        self.assertIsNotNone(job.finished_at)

    def test_cancel_is_idempotent(self):
        job = FakeJob(status="completed")
        db = FakeDb([job])

        with patch("routers.jobs.LangflowClient.stop_job") as stop:
            response = build_client(db).post("/api/jobs/job-1/cancel")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["cancelled"])
        self.assertEqual(response.json()["reason"], "already_finished")
        stop.assert_not_called()

    def test_cancel_missing_job_is_404(self):
        response = build_client(FakeDb()).post("/api/jobs/nope/cancel")

        self.assertEqual(response.status_code, 404)

    def test_cancel_requires_admin(self):
        db = FakeDb([FakeJob()])

        with patch("routers.jobs.LangflowClient.stop_job") as stop:
            response = build_client(db, is_admin=False).post("/api/jobs/job-1/cancel")

        self.assertEqual(response.status_code, 403)
        stop.assert_not_called()


class DeleteJobTests(unittest.TestCase):
    def test_delete_stops_langflow_before_removing_row(self):
        job = FakeJob()
        db = FakeDb([job])

        with patch("routers.jobs.LangflowClient.stop_job",
                   return_value={"stopped": True, "reason": "stopped"}) as stop:
            response = build_client(db).delete("/api/jobs/job-1")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["deleted"])
        stop.assert_awaited_once_with("lf-1")
        self.assertEqual(db.deleted, ["job-1"])
        self.assertEqual(db.jobs, {})

    def test_delete_reports_failed_stop_but_still_deletes(self):
        # Langflow не ответил — запись всё равно убираем, но честно сообщаем,
        # что воркер мог остаться.
        job = FakeJob()
        db = FakeDb([job])

        with patch("routers.jobs.LangflowClient.stop_job",
                   return_value={"stopped": False, "reason": "connection refused"}):
            response = build_client(db).delete("/api/jobs/job-1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["langflow"]["reason"], "connection refused")
        self.assertEqual(db.deleted, ["job-1"])

    def test_delete_finished_job_skips_langflow(self):
        job = FakeJob(status="completed")
        db = FakeDb([job])

        with patch("routers.jobs.LangflowClient.stop_job") as stop:
            response = build_client(db).delete("/api/jobs/job-1")

        self.assertEqual(response.status_code, 200)
        stop.assert_not_called()
        self.assertEqual(db.deleted, ["job-1"])


class PurgeStaleTests(unittest.TestCase):
    def _jobs(self):
        return [
            FakeJob("stale-1", progress_updated_at=ago(180), created_at=ago(200)),
            FakeJob("stale-2", status="queued", created_at=ago(60 * 24 * 8)),
            FakeJob("alive-1", progress_updated_at=ago(1), created_at=ago(30)),
            FakeJob("done-1", status="completed", created_at=ago(500)),
        ]

    def test_dry_run_reports_without_changing_anything(self):
        db = FakeDb(self._jobs())

        with patch("routers.jobs.LangflowClient.stop_job") as stop:
            response = build_client(db).post(
                "/api/jobs/purge-stale",
                json={"older_than_minutes": 30, "dry_run": True},
            )

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(body["dry_run"])
        self.assertEqual(body["count"], 2)
        self.assertEqual(
            sorted(j["job_id"] for j in body["jobs"]), ["stale-1", "stale-2"]
        )
        # Ничего не тронуто и Langflow не дёргали.
        self.assertEqual(db.deleted, [])
        stop.assert_not_called()

    def test_purge_deletes_only_stale_and_stops_langflow(self):
        db = FakeDb(self._jobs())

        with patch("routers.jobs.LangflowClient.stop_job",
                   return_value={"stopped": True, "reason": "stopped"}) as stop:
            response = build_client(db).post(
                "/api/jobs/purge-stale",
                json={"older_than_minutes": 30, "action": "delete", "dry_run": False},
            )

        body = response.json()
        self.assertEqual(body["count"], 2)
        self.assertEqual(body["deleted"], 2)
        self.assertEqual(body["stopped_in_langflow"], 2)
        self.assertEqual(sorted(db.deleted), ["stale-1", "stale-2"])
        # Живая задача и завершённая остались на месте.
        self.assertIn("alive-1", db.jobs)
        self.assertIn("done-1", db.jobs)
        self.assertEqual(stop.await_count, 2)

    def test_purge_can_cancel_instead_of_delete(self):
        db = FakeDb(self._jobs())

        with patch("routers.jobs.LangflowClient.stop_job",
                   return_value={"stopped": True, "reason": "stopped"}):
            response = build_client(db).post(
                "/api/jobs/purge-stale",
                json={"older_than_minutes": 30, "action": "cancel", "dry_run": False},
            )

        body = response.json()
        self.assertEqual(body["deleted"], 0)
        self.assertEqual(db.deleted, [])
        self.assertEqual(db.jobs["stale-1"].status, "failed")
        self.assertEqual(db.jobs["stale-2"].status, "failed")
        self.assertEqual(db.jobs["alive-1"].status, "running")

    def test_unknown_action_is_rejected(self):
        response = build_client(FakeDb()).post(
            "/api/jobs/purge-stale", json={"action": "nuke"},
        )

        self.assertEqual(response.status_code, 400)

    def test_zero_threshold_is_rejected(self):
        response = build_client(FakeDb()).post(
            "/api/jobs/purge-stale", json={"older_than_minutes": 0},
        )

        self.assertEqual(response.status_code, 400)

    def test_purge_requires_admin(self):
        response = build_client(FakeDb(self._jobs()), is_admin=False).post(
            "/api/jobs/purge-stale", json={"dry_run": True},
        )

        self.assertEqual(response.status_code, 403)

    def test_purge_route_is_not_shadowed_by_job_id(self):
        # /api/jobs/purge-stale не должен попасть в /api/jobs/{job_id}.
        response = build_client(FakeDb([])).post(
            "/api/jobs/purge-stale", json={"dry_run": True},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("count", response.json())


class ListJobsStaleFlagTests(unittest.TestCase):
    def test_list_marks_stale_jobs(self):
        db = FakeDb([
            FakeJob("stale-1", progress_updated_at=ago(180), created_at=ago(200)),
            FakeJob("alive-1", progress_updated_at=ago(1), created_at=ago(30)),
        ])

        response = build_client(db).get("/api/jobs?limit=50")

        rows = {r["job_id"]: r for r in response.json()}
        self.assertTrue(rows["stale-1"]["stale"])
        self.assertFalse(rows["alive-1"]["stale"])


if __name__ == "__main__":
    unittest.main()
