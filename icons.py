"""Библиотека иконок агентов.

Иконки загружаются из админки, хранятся в БД (``icon_library``) и
привязываются к опубликованным агентам через ``flow_publications.icon_id``.
По умолчанию у агента иконки нет.
"""

import uuid

# Что принимаем: только растровые/векторные картинки
ICON_MIME = {
    "image/svg+xml": ".svg",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
ICON_MAX_BYTES = 1024 * 1024          # 1 МБ — иконке больше не нужно

# Человекочитаемые названия форматов (для подсказок и ошибок)
FORMAT_NAMES = {
    "image/svg+xml": "SVG",
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/webp": "WEBP",
    "image/gif": "GIF",
}

# Публичный адрес отдачи
ICON_URL_PREFIX = "/icons"

# Подсказка для формы загрузки
HINT = "SVG, PNG, JPEG, WEBP или GIF, до 1 МБ"


def url(icon_id: str | None) -> str:
    """Публичный URL иконки или пустая строка, если иконки нет."""
    return f"{ICON_URL_PREFIX}/{icon_id}" if icon_id else ""


def new_id() -> str:
    return uuid.uuid4().hex


def clean_name(raw: str | None, fallback: str = "Иконка") -> str:
    """Подпись иконки в админке."""
    name = (raw or "").strip()
    return (name or fallback)[:200]


def is_allowed_mime(mime: str | None) -> bool:
    return (mime or "").lower() in ICON_MIME


def rejected_message(mime: str | None) -> str:
    formats = ", ".join(FORMAT_NAMES.values())
    return (
        f"Тип файла «{mime or 'неизвестен'}» не поддерживается. "
        f"Разрешены: {formats}."
    )
