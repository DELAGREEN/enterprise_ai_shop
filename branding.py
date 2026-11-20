"""Брендинг интерфейса: значения из админки важнее переменных окружения.

Ключи (таблица ``app_settings``):
    app_name               — название в шапке и <title>
    app_icon_url           — иконка сайта и логотип в шапке
    app_header_color       — фон шапки
    app_header_text_color  — цвет текста шапки

Картинка эмблемы может быть загружена файлом — она лежит в ``app_assets``
и имеет приоритет над ``app_icon_url``. Отдаётся через ``/branding/icon``.

Значения кэшируются в памяти: их читает каждый рендер шаблона, а меняются
они раз в жизни. Кэш сбрасывается при сохранении из админки.
"""

import logging
from datetime import datetime

from sqlalchemy import select

import config
from models import AppAsset, AppSetting

logger = logging.getLogger(__name__)

KEYS = (
    "app_name",
    "app_icon_url",
    "app_header_color",
    "app_header_text_color",
)

# Ключ картинки в app_assets и адрес её отдачи
ASSET_LOGO = "logo"
ASSET_URL = "/branding/icon"

# Что принимаем в загрузку эмблемы
LOGO_MIME = {
    "image/svg+xml": ".svg",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
}
LOGO_MAX_BYTES = 2 * 1024 * 1024      # 2 МБ — с запасом для логотипа


def env_defaults() -> dict:
    """Значения по умолчанию из .env."""
    return {
        "app_name": config.APP_NAME,
        "app_icon_url": config.APP_ICON_URL,
        "app_header_color": config.APP_HEADER_COLOR,
        "app_header_text_color": config.APP_HEADER_TEXT_COLOR,
    }


_cache: dict[str, str] | None = None
_asset_version: str | None = None


def asset_url(version: str | None = None) -> str:
    """Адрес загруженной эмблемы с параметром для сброса кэша браузера."""
    ver = version if version is not None else _asset_version
    return f"{ASSET_URL}?v={ver}" if ver else ASSET_URL


def get() -> dict[str, str]:
    """Текущий брендинг: кэш, иначе значения из env."""
    values = env_defaults() if _cache is None else dict(_cache)
    if _asset_version:
        values["app_icon_url"] = asset_url()
    return values


def has_asset() -> bool:
    return _asset_version is not None


def invalidate() -> None:
    global _cache, _asset_version
    _cache = None
    _asset_version = None


async def load(db) -> dict[str, str]:
    """Читает брендинг из БД. Ошибки не критичны — остаёмся на env."""
    global _cache, _asset_version
    values = env_defaults()
    version = None
    try:
        res = await db.execute(select(AppSetting).where(AppSetting.key.in_(KEYS)))
        for row in res.scalars().all():
            if row.value is not None and str(row.value).strip():
                values[row.key] = str(row.value).strip()

        asset = await db.get(AppAsset, ASSET_LOGO)
        if asset is not None:
            stamp = asset.updated_at
            version = str(int(stamp.timestamp())) if stamp else "1"
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось прочитать брендинг из БД: %s", exc)
        return values

    _cache = values
    _asset_version = version
    return get()


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
    return get()


async def save_logo(db, data: bytes, mime: str) -> dict:
    """Сохраняет загруженную картинку эмблемы в БД."""
    global _asset_version
    asset = await db.get(AppAsset, ASSET_LOGO)
    if asset is None:
        asset = AppAsset(key=ASSET_LOGO, mime=mime, size=len(data), data=data)
        db.add(asset)
    else:
        asset.mime = mime
        asset.size = len(data)
        asset.data = data
        asset.updated_at = datetime.utcnow()

    await db.commit()
    await db.refresh(asset)
    stamp = asset.updated_at
    _asset_version = str(int(stamp.timestamp())) if stamp else "1"
    logger.info("Эмблема обновлена: %s, %s байт", mime, len(data))
    return get()


async def delete_logo(db) -> dict:
    """Удаляет загруженную эмблему — снова действует URL из настроек/env."""
    global _asset_version
    asset = await db.get(AppAsset, ASSET_LOGO)
    if asset is not None:
        await db.delete(asset)
        await db.commit()
    _asset_version = None
    logger.info("Эмблема удалена, используется URL по умолчанию")
    return get()


def is_overridden(key: str, db_values: dict[str, str] | None = None) -> bool:
    """Переопределён ли ключ в админке (для подписи в UI)."""
    env = env_defaults()
    current = (db_values or get()).get(key)
    return bool(current) and current != env.get(key)
