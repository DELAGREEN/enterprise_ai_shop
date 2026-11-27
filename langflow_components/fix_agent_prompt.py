"""Правка system prompt у Agent в чат-флоу ЕОСЗ.

У Agent в flow поле ``system_prompt`` пустое, и Langflow подставляет свой
дефолт — **английский** «You are a Langflow Agent — an AI assistant that
completes user tasks using the tools configured in this flow». Он попадает
в system-сообщение и перебивает русские инструкции, которые лежат в
user-сообщении (вывод кубика Prompt Template).

Последствия видны в ответах:

* модель берёт название документа не из контекста, а из своего знания,
  подставляя ГОСТ, который в тексте встречается лишь как ссылка
  («по ГОСТ 2.103-2013» в чанке про литеру «О1»);
* в русский текст просачиваются английские слова («в документации
  **this** указано»).

Замер до правки: 2 выдуманных ГОСТа из 18 прогонов через flow и 0 из 10
при подаче того же промпта напрямую в модель (без Agent). То есть дело не
в тексте промпта, а в подмене system-сообщения.
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

SYSTEM_PROMPT = (
    "Ты — помощник по нормативной документации на русском языке. "
    "Отвечай только по предоставленному контексту.\n"
    "Название документа бери дословно из начала фрагмента (до первого «›»). "
    "Не подставляй названия ГОСТов, законов и стандартов, которые "
    "встречаются внутри текста как ссылки: это ссылки, а не документ, "
    "из которого взят ответ.\n"
    "Пиши по-русски, без английских слов и без латиницы, кроме аббревиатур "
    "из самого документа (ЕОСЗ, НМЦ, ОКПД2)."
)

CHAT_FLOW = "0eb8ed18-2ac5-4aa4-b42b-49d35bd8d65f"


def api(method: str, path: str, payload=None):
    base = os.environ.get("LANGFLOW_URL", "http://localhost:7860/api/v1")
    key = os.environ.get("LANGFLOW_API_KEY", "")
    headers = {"Accept-Encoding": "identity"}
    if key:
        headers["x-api-key"] = key
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(
        base.rstrip("/") + path, data=data, headers=headers, method=method
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        body = response.read().decode()
    return json.loads(body) if body.strip() else {}


def main() -> None:
    flow = api("GET", f"/flows/{CHAT_FLOW}")

    backup = Path("/tmp/langflow_backup")
    backup.mkdir(parents=True, exist_ok=True)
    (backup / f"{CHAT_FLOW}.json").write_text(
        json.dumps(flow, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    changed = []
    for node in flow["data"]["nodes"]:
        template = node["data"].get("node", {}).get("template") or {}
        entry = template.get("system_prompt")
        if not isinstance(entry, dict):
            continue
        before = entry.get("value") or ""
        if before.strip() == SYSTEM_PROMPT:
            continue
        entry["value"] = SYSTEM_PROMPT
        changed.append((node["id"], len(before), len(SYSTEM_PROMPT)))

    if not changed:
        print("уже установлено")
        return

    payload = {key: flow[key] for key in (
        "name", "description", "data", "is_component", "endpoint_name",
        "webhook", "tags", "locked", "mcp_enabled", "action_name",
        "action_description",
    ) if key in flow}
    api("PATCH", f"/flows/{CHAT_FLOW}", payload)

    for node_id, before, after in changed:
        print(f"{node_id}: system_prompt {before} -> {after} символов")
    print(f"записано, копия: {backup / (CHAT_FLOW + '.json')}")


if __name__ == "__main__":
    main()
