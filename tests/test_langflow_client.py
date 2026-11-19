import asyncio
import io
import json
import unittest
from unittest.mock import patch

import httpx

import config
from langflow_client import (
    LangflowClient,
    ThinkingSplitter,
    _stream_output_text,
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

    async def delete(self, url, headers=None, **kwargs):
        type(self).calls.append(("DELETE", url, None, None, headers))
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

    def test_upload_file_keeps_original_name(self):
        FakeAsyncClient.responses = [
            FakeResponse(200, []),                      # GET /api/v2/files — дубликатов нет
            FakeResponse(201, {                          # POST /api/v2/files
                "id": "file-1",
                "name": "a",
                "path": "user-1/a.txt",
                "size": 4,
            }),
        ]

        result = self._run(self._client().upload_file(
            io.BytesIO(b"data"), "a.txt", "flow-1"
        ))

        list_method, list_url = FakeAsyncClient.calls[0][0], FakeAsyncClient.calls[0][1]
        self.assertEqual(list_method, "GET")
        self.assertEqual(list_url, "http://langflow:7860/api/v2/files")

        method, url, payload, files, headers = FakeAsyncClient.calls[1]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "http://langflow:7860/api/v2/files")
        # имя уходит в Langflow как есть — без timestamp-префикса
        self.assertEqual(files["file"][0], "a.txt")
        self.assertEqual(result["path"], "user-1/a.txt")
        self.assertEqual(result["file_path"], "user-1/a.txt")

    def test_upload_file_replaces_same_name(self):
        FakeAsyncClient.responses = [
            FakeResponse(200, [                          # уже есть файл с тем же именем
                {"id": "old-1", "path": "user-1/a.txt", "name": "a"},
                {"id": "other", "path": "user-1/b.txt", "name": "b"},
            ]),
            FakeResponse(200, {}),                       # DELETE old-1
            FakeResponse(201, {                          # POST новый
                "id": "new-1", "name": "a", "path": "user-1/a.txt", "size": 4,
            }),
        ]

        self._run(self._client().upload_file(io.BytesIO(b"data"), "a.txt", "flow-1"))

        delete_call = FakeAsyncClient.calls[1]
        self.assertEqual(delete_call[0], "DELETE")
        self.assertEqual(delete_call[1], "http://langflow:7860/api/v2/files/old-1")
        # чужой файл не трогаем
        urls = [c[1] for c in FakeAsyncClient.calls]
        self.assertNotIn("http://langflow:7860/api/v2/files/other", urls)

    def test_upload_file_strips_directory_from_name(self):
        FakeAsyncClient.responses = [
            FakeResponse(200, []),
            FakeResponse(201, {"id": "f", "name": "a", "path": "user-1/a.txt", "size": 4}),
        ]

        self._run(self._client().upload_file(
            io.BytesIO(b"data"), "../../etc/a.txt", "flow-1"
        ))

        files = FakeAsyncClient.calls[1][3]
        self.assertEqual(files["file"][0], "a.txt")

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


class ThinkingSplitterTests(unittest.TestCase):
    def _run(self, chunks):
        s = ThinkingSplitter()
        out = []
        for c in chunks:
            out.extend(s.feed(c))
        out.extend(s.finish())
        return out, s

    def test_marker_in_single_chunk(self):
        out, s = self._run(["рассуждение</think>\n\nОтвет"])
        self.assertEqual(out, [("thinking", "рассуждение"), ("answer", "\n\nОтвет")])
        self.assertTrue(s.marker_found)

    def test_marker_split_between_chunks(self):
        out, s = self._run(["думаю", " ещё</th", "ink>\n\nПривет"])
        thinking = "".join(t for k, t in out if k == "thinking")
        answer = "".join(t for k, t in out if k == "answer")
        self.assertEqual(thinking, "думаю ещё")
        self.assertEqual(answer, "\n\nПривет")
        self.assertTrue(s.marker_found)

    def test_paired_open_tag_is_stripped(self):
        out, _ = self._run(["<think>размышление</think>ответ"])
        self.assertEqual(out, [("thinking", "размышление"), ("answer", "ответ")])

    def test_no_marker_keeps_text_until_finish(self):
        out, s = self._run(["просто", " текст"])
        self.assertFalse(s.marker_found)
        self.assertEqual("".join(t for _, t in out), "просто текст")

    def test_answer_after_marker_streams_directly(self):
        s = ThinkingSplitter()
        s.feed("x</think>")
        self.assertEqual(s.feed("ещё"), [("answer", "ещё")])
        self.assertEqual(s.feed("и ещё"), [("answer", "и ещё")])

    def test_marker_is_case_insensitive(self):
        out, s = self._run(["мысль</THINK>ответ"])
        self.assertTrue(s.marker_found)
        self.assertEqual("".join(t for k, t in out if k == "answer"), "ответ")

    def test_tail_is_held_so_marker_is_not_lost(self):
        # Маркер приходит разрезанным: часть попадает в придержанный хвост
        s = ThinkingSplitter()
        first = s.feed("текст</thi")
        # Первый кусок отдан не целиком — последние 7 символов придержаны
        self.assertEqual("".join(t for _, t in first), "тек")
        self.assertFalse(s.marker_found)

        second = s.feed("nk>да")
        self.assertTrue(s.marker_found)
        self.assertEqual(
            "".join(t for _, t in first + second), "текстда"
        )


class IterFlowEventsTests(unittest.TestCase):
    def _collect(self, tokens):
        class FakeClient:
            async def stream_flow(self, flow_id, input_value, session_id):
                for t in tokens:
                    yield ("token", t)

        async def run():
            from streaming import iter_flow_events

            return [e async for e in iter_flow_events(FakeClient(), "f", "q", None)]

        return asyncio.run(run())

    def test_splits_thinking_and_answer(self):
        events = self._collect(["думаю", "</think>", "ответ"])

        kinds = [k for k, _ in events]
        self.assertIn("thinking", kinds)
        self.assertIn("answer", kinds)
        self.assertNotIn("reset", kinds)

    def test_emits_reset_when_no_marker(self):
        events = self._collect(["без", " маркера"])

        self.assertEqual(events[-1], ("reset", "без маркера"))

    def test_forwards_error(self):
        class FakeClient:
            async def stream_flow(self, flow_id, input_value, session_id):
                yield ("error", "Langflow недоступен")

        async def run():
            from streaming import iter_flow_events

            return [e async for e in iter_flow_events(FakeClient(), "f", "q", None)]

        self.assertEqual(asyncio.run(run()), [("error", "Langflow недоступен")])

    def test_forwards_final_event(self):
        class FakeClient:
            async def stream_flow(self, flow_id, input_value, session_id):
                yield ("token", "мысль</think>от")
                yield ("token", "вет")
                yield ("final", "мысль</think>ответ")

        async def run():
            from streaming import iter_flow_events

            return [e async for e in iter_flow_events(FakeClient(), "f", "q", None)]

        events = asyncio.run(run())
        self.assertEqual(events[-1], ("final", "мысль</think>ответ"))


class StreamOutputTextTests(unittest.TestCase):
    def test_content_string(self):
        self.assertEqual(_stream_output_text({"content": "привет"}), "привет")

    def test_content_list(self):
        data = {"content": [{"text": "a"}, {"text": "b"}]}
        self.assertEqual(_stream_output_text(data), "ab")

    def test_results_message_fallback(self):
        data = {"results": {"message": {"text": "из message"}}}
        self.assertEqual(_stream_output_text(data), "из message")

    def test_empty(self):
        self.assertEqual(_stream_output_text({}), "")


if __name__ == "__main__":
    unittest.main()
