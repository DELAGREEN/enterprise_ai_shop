"""Брендинг интерфейса: приоритет админки над .env и подстановка в шаблоны."""

import asyncio
import unittest
from datetime import datetime
from unittest.mock import patch

import branding
import config
from models import AppAsset, AppSetting
from templating import templates


class FakeResult:
    def __init__(self, rows=None):
        self._rows = rows or []

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class FakeSetting:
    def __init__(self, key, value=None):
        self.key = key
        self.value = value


class FakeAsset:
    """Подмена AppAsset: branding трогает mime/size/data/updated_at."""

    def __init__(self, key="logo", mime="image/png", data=b"", updated_at=None):
        self.key = key
        self.mime = mime
        self.size = len(data)
        self.data = data
        self.updated_at = updated_at or datetime(2026, 1, 1, 12, 0, 0)


class FakeDb:
    """Минимальная БД: настройки + загруженные картинки."""

    def __init__(self, rows=None, assets=None):
        self.rows = {r.key: r for r in (rows or [])}
        self.assets = dict(assets or {})
        self.committed = 0
        self.deleted = []

    async def execute(self, stmt):
        return FakeResult(list(self.rows.values()))

    async def get(self, model, key):
        if model is AppAsset:
            return self.assets.get(key)
        return self.rows.get(key)

    def add(self, obj):
        if isinstance(obj, AppSetting):
            self.rows[obj.key] = obj
        else:
            self.assets[obj.key] = obj

    async def delete(self, obj):
        self.deleted.append(obj)
        self.assets.pop(obj.key, None)

    async def refresh(self, obj):
        return obj

    async def commit(self):
        self.committed += 1


class BrandingTests(unittest.TestCase):
    def setUp(self):
        self._env = patch.multiple(
            config,
            APP_NAME="Env Name",
            APP_ICON_URL="/env/icon.svg",
            APP_HEADER_COLOR="#111111",
            APP_HEADER_TEXT_COLOR="#eeeeee",
        )
        self._env.start()
        branding.invalidate()

    def tearDown(self):
        self._env.stop()
        branding.invalidate()

    def test_defaults_come_from_env(self):
        self.assertEqual(branding.get()["app_name"], "Env Name")
        self.assertEqual(branding.get()["app_header_color"], "#111111")

    def test_db_values_win_over_env(self):
        db = FakeDb([
            FakeSetting("app_name", "Из админки"),
            FakeSetting("app_header_color", "#ff0000"),
        ])

        values = asyncio.run(branding.load(db))

        self.assertEqual(values["app_name"], "Из админки")
        self.assertEqual(values["app_header_color"], "#ff0000")
        # Ключи, которых нет в БД, остаются из env
        self.assertEqual(values["app_header_text_color"], "#eeeeee")
        self.assertEqual(branding.get()["app_name"], "Из админки")

    def test_empty_value_falls_back_to_env(self):
        db = FakeDb([FakeSetting("app_name", None)])

        values = asyncio.run(branding.load(db))

        self.assertEqual(values["app_name"], "Env Name")

    def test_save_creates_and_updates_rows(self):
        db = FakeDb([FakeSetting("app_name", "Старое")])

        values = asyncio.run(branding.save(db, {"app_name": "Новое"}))

        self.assertEqual(values["app_name"], "Новое")
        self.assertEqual(db.rows["app_name"].value, "Новое")
        self.assertEqual(branding.get()["app_name"], "Новое")

    def test_save_with_empty_value_resets_to_env(self):
        db = FakeDb([FakeSetting("app_name", "Старое")])

        values = asyncio.run(branding.save(db, {"app_name": ""}))

        self.assertEqual(values["app_name"], "Env Name")
        self.assertIsNone(db.rows["app_name"].value)

    def test_is_overridden(self):
        db = FakeDb([FakeSetting("app_name", "Своё")])
        asyncio.run(branding.load(db))

        self.assertTrue(branding.is_overridden("app_name"))
        self.assertFalse(branding.is_overridden("app_header_color"))

    def test_broken_db_does_not_break_rendering(self):
        class BoomDb:
            async def execute(self, stmt):
                raise RuntimeError("db down")

        values = asyncio.run(branding.load(BoomDb()))

        self.assertEqual(values["app_name"], "Env Name")


class TemplateInjectionTests(unittest.TestCase):
    def setUp(self):
        self._env = patch.multiple(
            config,
            APP_NAME="Шапка",
            APP_ICON_URL="/static/vendor/image/logo.svg",
            APP_HEADER_COLOR="#123456",
            APP_HEADER_TEXT_COLOR="#ffffff",
        )
        self._env.start()
        branding.invalidate()

    def tearDown(self):
        self._env.stop()
        branding.invalidate()

    def test_branding_and_default_theme_in_html(self):
        html = templates.TemplateResponse("login.html", {}).body.decode()

        self.assertIn('data-theme="light"', html)
        self.assertIn("Шапка", html)
        self.assertIn("--header-bg: #123456", html)
        self.assertIn("/static/vendor/image/logo.svg", html)
        self.assertIn("themeToggle", html)

    def test_theme_comes_from_cookie(self):
        class FakeRequest:
            cookies = {"ui_theme": "dark"}

        html = templates.TemplateResponse(
            "login.html", {"request": FakeRequest()}
        ).body.decode()

        self.assertIn('data-theme="dark"', html)

    def test_unknown_cookie_falls_back_to_light(self):
        class FakeRequest:
            cookies = {"ui_theme": "neon"}

        html = templates.TemplateResponse(
            "login.html", {"request": FakeRequest()}
        ).body.decode()

        self.assertIn('data-theme="light"', html)

    def test_db_branding_reaches_template(self):
        db = FakeDb([FakeSetting("app_name", "Из БД")])
        asyncio.run(branding.load(db))

        html = templates.TemplateResponse("login.html", {}).body.decode()

        self.assertIn("Из БД", html)


class ThemeCookieTests(unittest.TestCase):
    def test_theme_cookie_set_from_user_row(self):
        from routers.auth import _apply_theme_cookie

        class Row:
            theme = "dark"

        class Resp:
            def __init__(self):
                self.calls = []

            def set_cookie(self, *args, **kwargs):
                self.calls.append((args, kwargs))

        resp = Resp()
        _apply_theme_cookie(resp, Row())

        self.assertEqual(resp.calls[0][0][0], config.THEME_COOKIE)
        self.assertEqual(resp.calls[0][0][1], "dark")

    def test_no_cookie_when_theme_not_set(self):
        from routers.auth import _apply_theme_cookie

        class Row:
            theme = None

        class Resp:
            def __init__(self):
                self.calls = []

            def set_cookie(self, *args, **kwargs):
                self.calls.append((args, kwargs))

        resp = Resp()
        _apply_theme_cookie(resp, Row())

        self.assertEqual(resp.calls, [])


class LogoAssetTests(unittest.TestCase):
    """Эмблема, загруженная файлом: хранение в БД и приоритет над URL."""

    def setUp(self):
        self._env = patch.multiple(
            config,
            APP_NAME="Env Name",
            APP_ICON_URL="/env/icon.svg",
            APP_HEADER_COLOR="#111111",
            APP_HEADER_TEXT_COLOR="#eeeeee",
        )
        self._env.start()
        branding.invalidate()

    def tearDown(self):
        self._env.stop()
        branding.invalidate()

    def test_asset_url_overrides_configured_url(self):
        db = FakeDb()
        asyncio.run(branding.save_logo(db, b"<svg/>", "image/svg+xml"))

        self.assertTrue(branding.has_asset())
        self.assertTrue(branding.get()["app_icon_url"].startswith("/branding/icon?v="))

    def test_delete_asset_restores_url(self):
        db = FakeDb()
        asyncio.run(branding.save_logo(db, b"<svg/>", "image/svg+xml"))
        asyncio.run(branding.delete_logo(db))

        self.assertFalse(branding.has_asset())
        self.assertEqual(branding.get()["app_icon_url"], "/env/icon.svg")

    def test_asset_loaded_from_db_on_startup(self):
        db = FakeDb(assets={"logo": FakeAsset()})

        values = asyncio.run(branding.load(db))

        self.assertIn("/branding/icon?v=", values["app_icon_url"])

    def test_asset_beats_saved_url(self):
        db = FakeDb()
        asyncio.run(branding.save(db, {"app_icon_url": "/custom.svg"}))
        asyncio.run(branding.save_logo(db, b"png", "image/png"))

        self.assertIn("/branding/icon", branding.get()["app_icon_url"])


if __name__ == "__main__":
    unittest.main()
