"""Библиотека иконок агентов.

Иконки двух видов:

* **базовые** — лежат в репозитории (``static/vendor/image/agents``) и
  доступны сразу, без загрузки;
* **загруженные** — из админки, хранятся в БД (``icon_library``).

Обе привязываются к опубликованным агентам через
``flow_publications.icon_id``. Базовые имеют вид ``base:<slug>``,
загруженные — uuid4 hex. По умолчанию у агента иконки нет.
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

# Публичный адрес отдачи загруженных иконок
ICON_URL_PREFIX = "/icons"

# Базовая библиотека: файлы лежат в static/vendor/image/agents
BASE_PREFIX = "base:"
BASE_DIR_URL = "/static/vendor/image/agents"

BASE_ICONS = (
    ("robot", "Робот"),
    ("analytics", "Аналитик"),
    ("chart", "Отчёты"),
    ("document", "Документы"),
    ("support", "Поддержка"),
    ("sales", "Продажи"),
    ("legal", "Юрист"),
    ("hr", "Персонал"),
    ("code", "Разработка"),
    ("cloud", "Инфраструктура"),
    ("shield", "Безопасность"),
    ("search", "Поиск"),
    ("chat", "Консультант"),
    ("idea", "Идеи"),
    ("gear", "Настройки"),
    ("book", "База знаний"),
    ("database", "Данные"),
    ("mail", "Рассылки"),
)

BASE_SLUGS = {slug for slug, _ in BASE_ICONS}

# Подсказка для формы загрузки
HINT = "SVG, PNG, JPEG, WEBP или GIF, до 1 МБ"


def base_icon_id(slug: str) -> str:
    return f"{BASE_PREFIX}{slug}"


def is_base(icon_id: str | None) -> bool:
    return bool(icon_id) and str(icon_id).startswith(BASE_PREFIX)


def base_icons() -> list[dict]:
    """Базовая библиотека для галерей: id, name, url."""
    return [
        {
            "id": base_icon_id(slug),
            "name": name,
            "url": f"{BASE_DIR_URL}/{slug}.svg",
            "base": True,
        }
        for slug, name in BASE_ICONS
    ]


def base_ids() -> set[str]:
    return {base_icon_id(slug) for slug in BASE_SLUGS}


def url(icon_id: str | None) -> str:
    """Публичный URL иконки или пустая строка, если иконки нет."""
    if not icon_id:
        return ""
    if is_base(icon_id):
        slug = str(icon_id)[len(BASE_PREFIX):]
        return f"{BASE_DIR_URL}/{slug}.svg" if slug in BASE_SLUGS else ""
    return f"{ICON_URL_PREFIX}/{icon_id}"


def is_known(icon_id: str | None, uploaded_ids: set[str] | None = None) -> bool:
    """Существует ли иконка: базовая или загруженная."""
    if not icon_id:
        return False
    if is_base(icon_id):
        return str(icon_id)[len(BASE_PREFIX):] in BASE_SLUGS
    return bool(uploaded_ids) and icon_id in uploaded_ids


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
