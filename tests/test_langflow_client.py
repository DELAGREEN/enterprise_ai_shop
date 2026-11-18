import asyncio
import io
import json
import unittest
from unittest.mock import patch

import httpx

import config
from langflow_client import (
    LangflowClient,
    build_file_tweaks,
    extract_error_text,
    extract_output_text,
    find_disconnected_inputs,
)

READ_FILE_FLOW = {
    "data": {
        "nodes": [
            {
                "id": "File-im8wz",
                "data": {
                    "node": {
                        "display_name": "Read File",
                        "template": {
                            "path": {
                                "_input_type": "FileInput",
                                "name": "path",
                                "list": True,
                                "value": [],
                            },
                            "file_path_str": {
                                "_input_type": "StrInput",
                                "name": "file_path_str",
                                "list": False,
                            },
                        },
                    }
                },
            },
            {
                "id": "ChatInput-Ugw8Q",
                "data": {
                    "node": {
                        "display_name": "Chat Input",
                        "template": {
                            "files": {
                                "_input_type": "FileInput",
                                "name": "files",
                                "list": True,
                            },
                        },
                    }
                },
            },
        ]
    }
}


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload
        if text is not None:
            self.text = text
        else:
            self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("GET", "http://langflow"),
                response=self,
            )


class FakeAsyncClient:
    calls: list = []
    responses: list = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, params=None, headers=None, **kwargs):
        type(self).calls.append(("GET", url, params, headers))
        return type(self).responses.pop(0)

    async def post(self, url, json=None, files=None, headers=None, **kwargs):
        type(self).calls.append(("POST", url, json, files, headers))
        return type(self).responses.pop(0)


class LangflowClientContractTests(unittest.TestCase):
    def setUp(self):
        self._demo = config.ENABLE_DEMO_MODE
        config.ENABLE_DEMO_MODE = False
        FakeAsyncClient.calls = []
        FakeAsyncClient.responses = []

    def tearDown(self):
        config.ENABLE_DEMO_MODE = self._demo

    def _client(self):
        return LangflowClient(
            base_url="http://langflow:7860/api/v1",
            api_key="test-key",
        )

    def _run(self, coro):
        with patch("langflow_client.httpx.AsyncClient", FakeAsyncClient):
            return asyncio.run(coro)


    def test_run_flow_background_matches_v2_schema(self):
        FakeAsyncClient.responses = [
            FakeResponse(200, READ_FILE_FLOW),          # GET /flows/{id} для tweaks
            FakeResponse(200, {                          # POST /workflows
                "job_id": "job-1",
                "flow_id": "flow-1",
                "status": "queued",
            }),
        ]

        result = self._run(self._client().run_flow_background(
            "flow-1",
            {"input_value": "hello", "file_paths": ["flow-1/123_a.txt"]},
            session_id="sess-1",
        ))

        get_method, get_url = FakeAsyncClient.calls[0][0], FakeAsyncClient.calls[0][1]
        self.assertEqual(get_method, "GET")
        self.assertEqual(get_url, "http://langflow:7860/api/v1/flows/flow-1")

        method, url, payload, files, headers = FakeAsyncClient.calls[1]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "http://langflow:7860/api/v2/workflows")
        self.assertEqual(payload["flow_id"], "flow-1")
        self.assertEqual(payload["mode"], "background")
        self.assertEqual(payload["input_value"], "hello")
        self.assertEqual(payload["files"], ["flow-1/123_a.txt"])
        self.assertEqual(payload["session_id"], "sess-1")
        # Read File берёт файлы из своего поля path → tweaks обязательны
        self.assertEqual(
            payload["tweaks"], {"File-im8wz": {"path": ["flow-1/123_a.txt"]}}
        )
        # output_type/input_type — параметры /api/v1/run, в v2 запрещены
        self.assertNotIn("output_type", payload)
        self.assertNotIn("input_type", payload)
        self.assertEqual(result["job_id"], "job-1")

    def test_build_file_tweaks_skips_chat_input_files(self):
        tweaks = build_file_tweaks(READ_FILE_FLOW, ["flow-1/a.docx"])

        self.assertEqual(tweaks, {"File-im8wz": {"path": ["flow-1/a.docx"]}})
        self.assertNotIn("ChatInput-Ugw8Q", tweaks)

    def test_build_file_tweaks_handles_non_list_field(self):
        flow = {
            "data": {"nodes": [{
                "id": "File-x",
                "data": {"node": {"template": {
                    "path": {
                        "_input_type": "FileInput",
                        "name": "path",
                        "list": False,
                    },
                }}},
            }]}
        }

        tweaks = build_file_tweaks(flow, ["flow-1/a.docx", "flow-1/b.docx"])

        self.assertEqual(tweaks, {"File-x": {"path": "flow-1/a.docx"}})

    def test_build_file_tweaks_without_file_components(self):
        flow = {"data": {"nodes": [{"id": "N1", "data": {"node": {"template": {}}}}]}}
        self.assertEqual(build_file_tweaks(flow, ["flow-1/a.docx"]), {})

    def test_find_disconnected_inputs(self):
        flow = {
            "data": {
                "nodes": [
                    {"id": "ChatInput-Ugw8Q", "data": {"type": "ChatInput"}},
                    {"id": "ChatInput-ok", "data": {"type": "ChatInput"}},
                    {"id": "File-im8wz", "data": {"type": "File"}},
                ],
                "edges": [{"source": "ChatInput-ok", "target": "File-im8wz"}],
            }
        }

        self.assertEqual(find_disconnected_inputs(flow), ["ChatInput-Ugw8Q"])

    def test_find_disconnected_inputs_none(self):
        flow = {
            "data": {
                "nodes": [{"id": "ChatInput-1", "data": {"type": "ChatInput"}}],
                "edges": [{"source": "ChatInput-1", "target": "X"}],
            }
        }
        self.assertEqual(find_disconnected_inputs(flow), [])

    def test_run_flow_background_omits_empty_files(self):
        FakeAsyncClient.responses = [
            FakeResponse(200, {
                "job_id": "job-1", "flow_id": "flow-1", "status": "queued",
            })
        ]

        self._run(self._client().run_flow_background(
            "flow-1", {"input_value": "hello", "file_paths": []}
        ))

        payload = FakeAsyncClient.calls[0][2]
        self.assertNotIn("files", payload)

    def test_run_flow_background_returns_error_on_422(self):
        FakeAsyncClient.responses = [
            FakeResponse(422, {"detail": [{"type": "extra_forbidden"}]})
        ]

        result = self._run(self._client().run_flow_background(
            "flow-1", {"input_value": "hello"}
        ))

        self.assertTrue(result["error"])
        self.assertEqual(result["status_code"], 422)
        self.assertIn("detail", result)

    def test_get_job_status_uses_query_param(self):
        FakeAsyncClient.responses = [
            FakeResponse(200, {"job_id": "job-1", "status": "in_progress"})
        ]

        result = self._run(self._client().get_job_status("job-1"))

        method, url, params, headers = FakeAsyncClient.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(url, "http://langflow:7860/api/v2/workflows")
        self.assertEqual(params, {"job_id": "job-1"})
        self.assertEqual(result["status"], "in_progress")

    def test_get_job_status_maps_failed_job(self):
        FakeAsyncClient.responses = [
            FakeResponse(500, {
                "detail": {
                    "error": "Job failed",
                    "code": "JOB_FAILED",
                    "error_detail": {"error": "Workflow execution timed out."},
                }
            })
        ]

        result = self._run(self._client().get_job_status("job-1"))

        self.assertEqual(result["status"], "timeout")
        self.assertEqual(
            extract_error_text(result), "Workflow execution timed out."
        )

    def test_upload_file_uses_flow_scoped_endpoint(self):
        FakeAsyncClient.responses = [
            FakeResponse(201, {
                "flowId": "flow-1",
                "file_path": "flow-1/2026-01-01_a.txt",
            })
        ]

        result = self._run(self._client().upload_file(
            io.BytesIO(b"data"), "a.txt", "flow-1"
        ))

        method, url, payload, files, headers = FakeAsyncClient.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(
            url, "http://langflow:7860/api/v1/files/upload/flow-1"
        )
        self.assertEqual(result["path"], "flow-1/2026-01-01_a.txt")
        self.assertEqual(result["file_path"], "flow-1/2026-01-01_a.txt")

    def test_extract_output_text_supports_v2(self):
        self.assertEqual(
            extract_output_text(
                {"output": {"reason": "single", "text": "hello"}}
            ),
            "hello",
        )
        self.assertEqual(
            extract_output_text({
                "output": {"reason": "multiple", "text": None},
                "outputs": {
                    "ChatOutput-1": {
                        "type": "message",
                        "status": "completed",
                        "content": {"text": "from outputs"},
                    }
                },
            }),
            "from outputs",
        )


    def test_extract_error_text_ignores_successful_response(self):
        self.assertEqual(
            extract_error_text({
                "status": "completed",
                "errors": [],
                "output": {"text": "hi"},
            }),
            "",
        )


if __name__ == "__main__":
    unittest.main()
