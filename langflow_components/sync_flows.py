"""Синхронизация компонентов из репозитория в кубики Langflow.

Компонент в Langflow — это копия файла внутри ``flow.data``. Правка в
репозитории на работающий flow не влияет: код в кубике остаётся старым,
пока его не заменят руками. Так уже случалось: в кубике парсера лежала
версия без исправления лимита child, а в кубике ингеста — значения
``max_embed_chars=1800`` и ``max_parent_chars=2000`` при дефолтах 3600 и
3000. Код обновили, значения забыли, и правка не доехала до коллекции.

Скрипт делает две вещи:

* заменяет ``code.value`` в каждом узле, где встречается класс компонента;
* сообщает о значениях, которые разъехались с дефолтами из кода
  (``--fix-values`` приводит их к дефолтам).

Запуск (нужны ``LANGFLOW_URL`` и ``LANGFLOW_API_KEY`` из ``.env``):

    python langflow_components/sync_flows.py            # только показать
    python langflow_components/sync_flows.py --apply    # записать

Перед записью сохраняется резервная копия каждого flow в ``--backup-dir``.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Класс компонента -> файл с его исходником.
COMPONENTS = {
    "EoszStructureParser": "eosz_structure_parser.py",
    "QdrantHybridIngest": "qdrant_hybrid_ingest.py",
    "EoszRetriever": "eosz_retriever.py",
}

RE_INPUT_NAME = re.compile(r'name="([a-z_]+)"')
RE_INPUT_VALUE = re.compile(r"^\s*value=(.+?),?\s*$")

# Значения, которые обязаны совпадать с кодом. Это лимиты, связанные с
# ёмкостью моделей: если они разъедутся, чанки молча усекаются, и об этом
# нигде не сообщается. Остальные входы (collection_name, top_k, doc_title)
# задаются на каждый документ свои — сравнивать их с дефолтами бессмысленно.
WATCHED_VALUES = (
    "max_child_tokens",
    "max_parent_chars",
    "max_parent_tokens",
    "max_embed_chars",
)


def api(method: str, path: str, payload=None):
    base = os.environ.get("LANGFLOW_URL", "http://localhost:7860/api/v1")
    url = base.rstrip("/") + path
    key = os.environ.get("LANGFLOW_API_KEY", "")
    # Langflow отвечает gzip'ом на большие flow, а urllib его не распаковывает.
    headers = {"Accept-Encoding": "identity"}
    if key:
        headers["x-api-key"] = key
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read()
            if (response.headers.get("Content-Encoding") or "").lower() == "gzip":
                raw = gzip.decompress(raw)
            body = raw.decode()
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"{method} {url} -> HTTP {exc.code}: {exc.read()[:300]!r}")
    return json.loads(body) if body.strip() else {}


def declared_defaults(code: str) -> dict:
    """Значения по умолчанию для входов, объявленных в коде компонента.

    Разбор построчный, а не через AST: код компонента — это класс с
    ``inputs = [...]``, а нам нужны только пары «имя входа -> value=…».
    """
    defaults: dict[str, object] = {}
    pending: str | None = None

    for line in code.splitlines():
        match = RE_INPUT_NAME.search(line)
        if match:
            pending = match.group(1)
            continue
        if pending is None:
            continue
        match = RE_INPUT_VALUE.match(line)
        if not match:
            continue
        raw = match.group(1).strip()
        try:
            defaults[pending] = json.loads(
                raw.replace("True", "true").replace("False", "false")
            )
        except ValueError:
            defaults[pending] = raw.strip("\"'")
        pending = None

    return defaults


def iter_component_nodes(flow: dict):
    """Узлы flow, в которых лежит код одного из наших компонентов."""
    for node in (flow.get("data") or {}).get("nodes") or []:
        template = (node.get("data") or {}).get("node", {}).get("template") or {}
        code = template.get("code")
        if not isinstance(code, dict):
            continue
        value = code.get("value") or ""
        for cls, filename in COMPONENTS.items():
            if f"class {cls}" in value:
                yield node, template, cls, filename
                break


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="записать изменения (без флага только показать)")
    parser.add_argument("--fix-values", action="store_true",
                        help="привести значения входов к дефолтам из кода")
    parser.add_argument("--backup-dir", default="/tmp/langflow_backup",
                        help="куда сохранить исходные flow перед записью")
    parser.add_argument("--flow", action="append", default=[],
                        help="ограничить конкретным flow_id (можно несколько)")
    args = parser.parse_args()

    flows = api("GET", "/flows/")
    if isinstance(flows, dict):
        flows = flows.get("items") or []

    sources = {cls: (HERE / name).read_text(encoding="utf-8")
               for cls, name in COMPONENTS.items()}

    total = 0
    for flow in flows:
        flow_id = flow.get("id")
        if args.flow and flow_id not in args.flow:
            continue

        nodes = list(iter_component_nodes(flow))
        if not nodes:
            continue

        changes: list[str] = []
        touched = False

        for node, template, cls, _filename in nodes:
            code = template["code"]
            new_code = sources[cls]

            if code.get("value") != new_code:
                changes.append(
                    f"  {node['id']} ({cls}): код {len(code.get('value') or '')} "
                    f"-> {len(new_code)} символов"
                )
                code["value"] = new_code
                touched = True

            for key, want in declared_defaults(new_code).items():
                if key not in WATCHED_VALUES:
                    continue
                entry = template.get(key)
                if not isinstance(entry, dict) or "value" not in entry:
                    continue
                have = entry["value"]
                if have == want:
                    continue
                changes.append(f"  {node['id']}: {key} = {have!r} (в коде {want!r})")
                if args.fix_values:
                    entry["value"] = want
                    touched = True

        if not changes:
            continue

        total += 1
        print(f"{flow.get('name')} ({flow_id})")
        print("\n".join(changes))

        if not (args.apply and touched):
            continue

        backup = Path(args.backup_dir)
        backup.mkdir(parents=True, exist_ok=True)
        (backup / f"{flow_id}.json").write_text(
            json.dumps(flow, ensure_ascii=False, indent=1), encoding="utf-8"
        )

        payload = {key: flow[key] for key in (
            "name", "description", "data", "is_component", "endpoint_name",
            "webhook", "tags", "locked", "mcp_enabled", "action_name",
            "action_description",
        ) if key in flow}
        api("PATCH", f"/flows/{flow_id}", payload)
        print(f"  записано, копия: {backup / (flow_id + '.json')}")

    if not total:
        print("расхождений нет")
    elif not args.apply:
        print("\nэто был просмотр; для записи добавьте --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
