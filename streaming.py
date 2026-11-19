"""Общая логика потоковой отдачи ответа чата (Server-Sent Events).

Используется и в полной версии чата, и в embed-чате, чтобы поведение
(разделение «мышления» и ответа, формат событий) не расходилось.
"""

import json
from typing import AsyncIterator

from langflow_client import LangflowClient, ThinkingSplitter


def sse_pack(event: str, data: dict) -> str:
    """Одно SSE-событие. Разделитель — пустая строка после data."""
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


async def iter_flow_events(
    client: LangflowClient,
    flow_id: str,
    input_value: str,
    session_id: str | None,
) -> AsyncIterator[tuple[str, str]]:
    """Поток токенов, разделённый на «мышление» и ответ.

    Отдаёт:
      * ``("thinking", текст)`` — кусок рассуждения модели;
      * ``("answer", текст)`` — кусок ответа;
      * ``("final", текст)`` — финальный текст flow (событие ``output``),
        авторитетный источник для сохранения в БД;
      * ``("error", сообщение)`` — сбой;
      * ``("reset", полный текст)`` — маркер ``</think>`` не пришёл,
        значит всё, что успели отдать как «мышление», на самом деле ответ.
    """
    splitter = ThinkingSplitter()
    collected: list[str] = []

    async for kind, payload in client.stream_flow(flow_id, input_value, session_id):
        if kind in ("error", "final"):
            yield (kind, payload)
            if kind == "error":
                return
            continue

        collected.append(payload)
        for out_kind, text in splitter.feed(payload):
            yield (out_kind, text)

    for out_kind, text in splitter.finish():
        yield (out_kind, text)

    if not splitter.marker_found:
        yield ("reset", "".join(collected))
