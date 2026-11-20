"""Брендинг интерфейса: значения из админки важнее переменных окружения.

Ключи (таблица ``app_settings``):
    app_name               — название в шапке и <title>
    app_icon_url           — иконка сайта и логотип в шапке
    app_header_color       — фон шапки
    app_header_text_color  — цвет текста шапки

Значения кэшируются в памяти: их читает каждый рендер шаблона, а меняются
они раз в жизни. Кэш сбрасывается при сохранении из админки.
"""

import logging

from sqlalchemy import select

import config
from models import AppSetting

logger = logging.getLogger(__name__)

KEYS = (
    "app_name",
    "app_icon_url",
    "app_header_color",
    "app_header_text_color",
)


def env_defaults() -> dict:
    """Значения по умолчанию из .env."""
    return {
        "app_name": config.APP_NAME,
        "app_icon_url": config.APP_ICON_URL,
        "app_header_color": config.APP_HEADER_COLOR,
        "app_header_text_color": config.APP_HEADER_TEXT_COLOR,
    }


_cache: dict[str, str] | None = None


def get() -> dict[str, str]:
    """Текущий брендинг: кэш, иначе значения из env."""
    if _cache is None:
        return env_defaults()
    return dict(_cache)


def invalidate() -> None:
    global _cache
    _cache = None


async def load(db) -> dict[str, str]:
    """Читает брендинг из БД. Ошибки не критичны — остаёмся на env."""
    global _cache
    values = env_defaults()
    try:
        res = await db.execute(select(AppSetting).where(AppSetting.key.in_(KEYS)))
        for row in res.scalars().all():
            if row.value is not None and str(row.value).strip():
                values[row.key] = str(row.value).strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось прочитать брендинг из БД: %s", exc)
        return values

    _cache = values
    return dict(values)


async def save(db, values: dict) -> dict:
    """Сохраняет переданные ключи. Пустая строка = сброс к значению из env."""
    defaults = env_defaults()
    for key in KEYS:
        if key not in values:
            continue
        raw = values[key]
        value = str(raw).strip() if raw is not None else ""

        row = await db.get(AppSetting, key)
        if row is None:
            db.add(AppSetting(key=key, value=value or None))
        else:
            row.value = value or None

        if value:
            defaults[key] = value

    await db.commit()
    global _cache
    _cache = defaults
    return dict(defaults)


def is_overridden(key: str, db_values: dict[str, str] | None = None) -> bool:
    """Переопределён ли ключ в админке (для подписи в UI)."""
    env = env_defaults()
    current = (db_values or get()).get(key)
    return bool(current) and current != env.get(key)
