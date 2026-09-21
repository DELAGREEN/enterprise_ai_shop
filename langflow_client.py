import logging
import re

import httpx

import config

import time 

from typing import Any

logger = logging.getLogger(__name__)

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

    async def get_all_flows(self) -> list:
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

                # Сохраняем в кэш
                _FLOW_CACHE["data"] = flows
                _FLOW_CACHE["ts"] = now
                logger.debug("Кэш flow обновлён: %d записей", len(flows))
                return flows
            
        except Exception as e:
            logger.error("Не удалось получить список flow из Langflow: %s", e)

            # Устаревший кэш лечше, чем пустой список
            stale = _FLOW_CACHE["data"]
            if stale is not None:
                logger.warning("Отдаём устаревший кэш %d записей", len(stale))
                return stale
            return []

    async def run_flow(self, flow_id: str, input_value: str, session_id: str | None = None) -> dict:
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
        outputs = data.get("outputs")
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
        for key in ("result", "text", "output"):
            if key in data:
                return str(data[key])
        return str(data)
    return str(data)