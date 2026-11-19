import logging
import re
import httpx
import config
import time 
import json
from typing import Any
from typing import BinaryIO
from pathlib import Path

logger = logging.getLogger(__name__)


def _demo_flow_list() -> list[dict]:
    """Возвращает список flow для демо-режима без Langflow."""
    if not config.ENABLE_DEMO_MODE:
        return []
    return [{
        "id": config.DEMO_FLOW_ID,
        "name": config.DEMO_FLOW_NAME,
        "description": config.DEMO_FLOW_DESCRIPTION,
        "is_published": True,
    }]


def _demo_flow_response(input_value: str) -> dict:
    """Возвращает mock-ответ, который распознаётся текущим парсером ответов."""
    prompt = (input_value or "").strip() or "тестовый запрос"
    answer = f"[DEMO MODE] Это демо-ответ для: {prompt}. Langflow и vLLM сейчас недоступны, но интерфейс уже работает как надо."
    return {
        "outputs": [{
            "outputs": [{
                "results": {
                    "message": {"text": answer}
                }
            }]
        }]
    }

def find_disconnected_inputs(flow: dict) -> list[str]:
    """ID входных компонентов (Chat Input) без исходящих связей.

    Langflow стартует запуск с входного компонента. Если он никуда не
    подключён, граф «выполняется» за доли секунды и не делает ничего.
    """
    data = (flow or {}).get("data") or {}
    nodes = data.get("nodes") or []
    edges = data.get("edges") or []
    sources = {edge.get("source") for edge in edges if isinstance(edge, dict)}

    disconnected = []
    for node in nodes:
        node_id = node.get("id")
        node_type = (node.get("data") or {}).get("type")
        if node_id and node_type == "ChatInput" and node_id not in sources:
            disconnected.append(node_id)
    return disconnected


def build_file_tweaks(flow: dict, file_paths: list[str]) -> dict:
    """Собирает tweaks, чтобы Read File получил загруженные файлы.

    В Langflow 1.11.2 поле ``files`` из запроса на запуск попадает ТОЛЬКО
    в Chat Input (``lfx/graph/vertex/base.py``:
    ``if self._is_chat_input() ... chat_input.update({"files": files})``).
    Read File читает файлы из своего поля ``path`` (FileInput), поэтому путь
    нужно передавать через tweaks конкретного компонента.
    """
    if not file_paths:
        return {}

    nodes = ((flow or {}).get("data") or {}).get("nodes") or []
    tweaks: dict[str, dict] = {}

    for node in nodes:
        node_id = node.get("id")
        template = ((node.get("data") or {}).get("node") or {}).get("template") or {}
        if not node_id or not isinstance(template, dict):
            continue

        for field_name, field in template.items():
            if not isinstance(field, dict):
                continue
            if field.get("_input_type") != "FileInput":
                continue
            # Chat Input.files Langflow заполняет сам из payload["files"]
            if field.get("name") == "files":
                continue

            tweaks.setdefault(node_id, {})[field_name] = (
                list(file_paths) if field.get("list", True) else file_paths[0]
            )

    return tweaks


def _response_detail(response: httpx.Response) -> Any:
    """Достаёт тело ошибки Langflow для логов и ответа API."""
    try:
        data = response.json()
    except Exception:
        return response.text
    if isinstance(data, dict):
        return data.get("detail", data)
    return data


#-------- Кэш списка flow---------
_FLOW_CACHE: dict[str, Any] = {"data": None, "ts": 0.0}
_FLOW_TTL = 30.0 # секунды

#-------- Regex для "Мышления" --------
_THINK_PAIRED = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)
_THINK_CLOSE_ONLY = re.compile(r"^(.*?)</think>", re.DOTALL | re.IGNORECASE)


def invalidate_flow_cache() -> None:
    """Сбросить кэш списка flow (вызвать после publish/edit) в админке."""
    _FLOW_CACHE["data"] = None
    _FLOW_CACHE["ts"] = 0.0
    logger.info("Кэш списка flow сброшен")

class LangflowClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        self.base_url = (base_url or config.LANGFLOW_URL).rstrip("/")
        self.api_key = api_key or config.LANGFLOW_API_KEY

    @property
    def headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["x-api-key"] = self.api_key
        return h

    @property
    def _api_root(self) -> str:
        """Корневой URL Langflow без /api/v1 (если он был)."""
        root = self.base_url.rstrip("/")
        if root.endswith("/api/v1"):
            root = root[:-7]  # отрезаем /api/v1
        return root

    async def get_all_flows(self) -> list:
        if config.ENABLE_DEMO_MODE:
            flows = _demo_flow_list()
            _FLOW_CACHE["data"] = flows
            _FLOW_CACHE["ts"] = time.monotonic()
            return flows

        now = time.monotonic()

        # Свежий кэш - отдаём сразу
        if _FLOW_CACHE["data"] is not None and (now - _FLOW_CACHE["ts"]) <_FLOW_TTL:
            return _FLOW_CACHE["data"]
        # Идём в Langflow
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.get(f"{self.base_url}/flows/", headers=self.headers)
                r.raise_for_status()
                data = r.json()
                if isinstance(data, list):
                    flows = data
                # некоторые версии Langflow заворачивают в {"flows": [...]}
                elif isinstance(data, dict) and "flows" in data:
                    flows = data["flows"]
                else:
                    flows = []

                _FLOW_CACHE["data"] = flows
                _FLOW_CACHE["ts"] = now
                return flows
        except Exception:
            # Устаревший кэш лечше, чем пустой список
            stale = _FLOW_CACHE["data"]
            if stale is not None:
                logger.warning("Отдаём устаревший кэш %d записей", len(stale))
                return stale
            return []

    async def run_flow(self, flow_id: str, input_value: str, session_id: str | None = None) -> dict:
        if config.ENABLE_DEMO_MODE:
            return _demo_flow_response(input_value)

        payload = {
            "input_value": input_value,
            "output_type": "chat",
            "input_type": "chat",
        }
        if session_id:
            payload["session_id"] = session_id

        try:
            async with httpx.AsyncClient(timeout=180.0) as client:
                r = await client.post(
                    f"{self.base_url}/run/{flow_id}",
                    json=payload,
                    headers=self.headers,
                )
                r.raise_for_status()
                return r.json()
        except Exception as e:
            logger.exception("Ошибка запуска flow %s: %s", flow_id, e)
            return {"error": str(e)}

    async def get_flow(self, flow_id: str) -> dict:
        """Полное определение flow (нужно для tweaks файловых компонентов)."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(
                f"{self.base_url}/flows/{flow_id}",
                headers=self.headers,
            )
            r.raise_for_status()
            return r.json()

    async def list_user_files(self) -> list:
        """Файлы текущего пользователя API-ключа (v2, user-scoped)."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(
                f"{self._api_root}/api/v2/files",
                headers=self.headers,
            )
            r.raise_for_status()
            data = r.json()
        return data if isinstance(data, list) else []

    async def delete_user_file(self, file_id: str) -> None:
        """Удаляет файл из хранилища пользователя по id."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.delete(
                f"{self._api_root}/api/v2/files/{file_id}",
                headers=self.headers,
            )
            if r.status_code not in (200, 204, 404):
                r.raise_for_status()

    async def upload_file(self, file: BinaryIO, filename: str, flow_id: str) -> dict:
        """Загружает файл, сохраняя его оригинальное имя.

        Flow-scoped ``/api/v1/files/upload/{flow_id}`` добавляет к имени
        timestamp (``2026-11-19_13-09-24_gost.docx``), и этот префикс потом
        попадает в payload Qdrant. Поэтому используем ``/api/v2/files``:
        он хранит файл как ``{user_id}/{filename}`` без переименования.

        Перед загрузкой удаляем прежнюю версию с тем же именем — иначе
        Langflow добавит суффикс ``" (1)"`` и имя снова «поедет».
        """
        name = Path(filename).name

        try:
            for existing in await self.list_user_files():
                if Path(str(existing.get("path") or "")).name == name:
                    await self.delete_user_file(existing["id"])
                    logger.info("Удалён прежний файл %s (%s)", name, existing["id"])
        except httpx.HTTPError as e:
            # Не блокируем загрузку из-за неудачной очистки
            logger.warning("Не удалось очистить прежнюю версию %s: %s", name, e)

        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(
                f"{self._api_root}/api/v2/files",
                files={"file": (name, file)},
                headers={"x-api-key": self.api_key} if self.api_key else {},
            )
            r.raise_for_status()
            data = r.json()

        logger.info("Langflow upload %r → flow %s: %s", filename, flow_id, data)

        # Нормализуем ответ: jobs.py ожидает path/file_path.
        if isinstance(data, dict):
            if not data.get("path"):
                data["path"] = data.get("file_path")
            if not data.get("file_path"):
                data["file_path"] = data.get("path")
        return data

    async def run_flow_background(
        self, flow_id: str, inputs: dict, session_id: str | None = None
    ) -> dict:
        """Ставит flow в очередь Langflow (``mode=background``).

        ВАЖНО: ``/api/v2/workflows`` принимает ``WorkflowRunRequest`` с
        ``additionalProperties=false``. ``output_type``/``input_type`` —
        параметры старого ``/api/v1/run``; в v2 они запрещены (HTTP 422).
        """
        file_paths = [p for p in inputs.get("file_paths", []) if p]
        if not file_paths and inputs.get("file_path"):
            file_paths = [inputs["file_path"]]
        input_value = inputs.get("input_value") or "Проанализируй файл"

        payload = {
            "flow_id": flow_id,
            "input_value": input_value,
            "mode": "background",
        }
        if file_paths:
            # ``files`` Langflow кладёт в Chat Input (если он есть во flow).
            payload["files"] = file_paths
            # Read File берёт файлы из своего поля ``path`` → нужны tweaks.
            flow: dict | None = None
            try:
                flow = await self.get_flow(flow_id)
                tweaks = build_file_tweaks(flow, file_paths)
            except httpx.HTTPError as e:
                logger.warning("Не удалось получить flow %s для tweaks: %s", flow_id, e)
                tweaks = {}
            if tweaks:
                payload["tweaks"] = tweaks
                logger.info("File tweaks: %s", json.dumps(tweaks, default=str))
            else:
                logger.warning(
                    "Во flow %s не найден файловый компонент (FileInput) — "
                    "файлы уйдут только в Chat Input",
                    flow_id,
                )

            disconnected = find_disconnected_inputs(flow) if flow else []
            if disconnected:
                logger.warning(
                    "Во flow %s входной компонент %s не подключён к графу: "
                    "Langflow стартует с него и завершит запуск без результата",
                    flow_id,
                    ", ".join(disconnected),
                )
        if session_id:
            payload["session_id"] = session_id

        logger.info("Calling Langflow API: %s/api/v2/workflows", self._api_root)
        logger.info("Payload: %s", json.dumps(payload, default=str))

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                r = await client.post(
                    f"{self._api_root}/api/v2/workflows",
                    json=payload,
                    headers=self.headers,
                )
                logger.info("Response status: %s", r.status_code)
                logger.info("Response body: %s", r.text)

                if r.status_code >= 400:
                    detail = _response_detail(r)
                    logger.error("Langflow API вернул %s: %s", r.status_code, detail)
                    return {
                        "error": True,
                        "status_code": r.status_code,
                        "detail": detail,
                        "message": (
                            "Ошибка валидации запроса к Langflow"
                            if r.status_code == 422
                            else "Ошибка запуска flow в Langflow"
                        ),
                    }
                return r.json()
        except httpx.HTTPError as e:
            logger.exception("Ошибка соединения с Langflow: %s", e)
            return {
                "error": True,
                "status_code": 502,
                "detail": str(e),
                "message": "Langflow недоступен",
            }

    async def get_job_status(self, langflow_job_id: str) -> dict:
        """Возвращает статус background-job.

        Langflow отдаёт HTTP 500 с ``detail.code == "JOB_FAILED"``, когда
        job завершился ошибкой. Это штатный статус, а не сетевая ошибка,
        поэтому превращаем его в ``{"status": "failed"/"timeout", ...}``.
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(
                f"{self._api_root}/api/v2/workflows",
                params={"job_id": langflow_job_id},
                headers=self.headers,
            )

            try:
                data = r.json()
            except Exception:
                data = None

            detail = data.get("detail") if isinstance(data, dict) else None
            if isinstance(detail, dict) and detail.get("code") == "JOB_FAILED":
                error_detail = detail.get("error_detail") or detail
                status = (
                    "timeout"
                    if "timed out" in str(error_detail).lower()
                    else "failed"
                )
                return {
                    "job_id": langflow_job_id,
                    "status": status,
                    "errors": [error_detail],
                }

            r.raise_for_status()
            return data or {}


def parse_thinking(text: str) -> tuple[str, str]:
    """Возвращает (clean_text, thinking). Поддерживает <think>...</think> и ...</think>."""
    if not text:
        return "", ""

    paired = _THINK_PAIRED.findall(text)
    if paired:
        thinking = "\n\n".join(p.strip() for p in paired if p.strip())
        clean = _THINK_PAIRED.sub("", text).strip()
        return clean, thinking

    if "</think>" in text.lower():
        m = _THINK_CLOSE_ONLY.match(text)
        if m:
            thinking = m.group(1).strip()
            clean = text[m.end():].strip()
            return clean, thinking

    return text.strip(), ""


def extract_output_text(data) -> str:
    """Достаём текст из ответа Langflow (структура зависит от версии)."""
    if not data:
        return ""
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        if "error" in data:
            return f"Ошибка Langflow: {data['error']}"

        # v2 (/api/v2/workflows): {"output": {"reason": ..., "text": ...}}
        output = data.get("output")
        if isinstance(output, dict):
            if output.get("text") is not None:
                return str(output["text"])
        elif isinstance(output, str):
            return output

        outputs = data.get("outputs")

        # v1: outputs — список вложенных структур
        if isinstance(outputs, list):
            for out in outputs:
                for inner in out.get("outputs", []) or []:
                    results = inner.get("results") or {}
                    msg = results.get("message")
                    if isinstance(msg, dict) and msg.get("text"):
                        return msg["text"]
                    if isinstance(msg, str):
                        return msg
                    for o in inner.get("outputs", []) or []:
                        if isinstance(o, dict) and o.get("text"):
                            return o["text"]

        # v2: outputs — словарь component_id -> ComponentOutput
        if isinstance(outputs, dict):
            for item in outputs.values():
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                if isinstance(content, dict):
                    if content.get("text"):
                        return str(content["text"])
                    message = content.get("message")
                    if isinstance(message, str) and message:
                        return message
                    if isinstance(message, dict) and message.get("text"):
                        return str(message["text"])
                if isinstance(content, str) and content:
                    return content

        # v2-ответ явно сообщает, что текста нет (reason: none/multiple/...)
        if isinstance(output, dict) and "reason" in output:
            return ""

        for key in ("result", "text"):
            if key in data:
                return str(data[key])
        return str(data)
    return str(data)


def extract_error_text(data) -> str:
    """Человекочитаемое описание ошибок job/flow."""
    if not data:
        return ""
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        errors = data.get("errors")
        if errors:
            return extract_error_text(errors)
        for key in ("error", "message", "detail"):
            value = data.get(key)
            if value:
                return extract_error_text(value)
        return ""
    if isinstance(data, list):
        parts = []
        for item in data:
            if isinstance(item, dict):
                value = (
                    item.get("error")
                    or item.get("message")
                    or (item.get("data") or {}).get("text")
                )
                if value:
                    parts.append(str(value))
            else:
                parts.append(str(item))
        return "; ".join(p for p in parts if p)
    return str(data)