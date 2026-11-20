"""Библиотека иконок агентов."""

import unittest

import icons


class IconHelpersTests(unittest.TestCase):
    def test_url_empty_when_no_icon(self):
        self.assertEqual(icons.url(None), "")
        self.assertEqual(icons.url(""), "")

    def test_url_for_icon(self):
        self.assertEqual(icons.url("abc123"), "/icons/abc123")

    def test_new_id_is_unique(self):
        ids = {icons.new_id() for _ in range(50)}
        self.assertEqual(len(ids), 50)
        self.assertTrue(all(len(i) == 32 for i in ids))

    def test_allowed_mimes(self):
        for mime in ("image/svg+xml", "image/png", "image/jpeg", "image/webp", "image/gif"):
            self.assertTrue(icons.is_allowed_mime(mime), mime)

    def test_rejected_mimes(self):
        for mime in ("text/plain", "application/pdf", "image/bmp", None, ""):
            self.assertFalse(icons.is_allowed_mime(mime), mime)

    def test_mime_is_case_insensitive(self):
        self.assertTrue(icons.is_allowed_mime("IMAGE/PNG"))

    def test_rejected_message_lists_formats(self):
        message = icons.rejected_message("text/plain")
        self.assertIn("text/plain", message)
        self.assertIn("PNG", message)

    def test_clean_name_uses_fallback(self):
        self.assertEqual(icons.clean_name("", "logo"), "logo")
        self.assertEqual(icons.clean_name(None, "logo"), "logo")
        self.assertEqual(icons.clean_name("  Аналитик  ", "x"), "Аналитик")

    def test_clean_name_truncates(self):
        self.assertEqual(len(icons.clean_name("x" * 500)), 200)


class BaseIconTests(unittest.TestCase):
    """Базовая библиотека иконок из static/vendor/image/agents."""

    def test_base_icons_not_empty(self):
        self.assertGreaterEqual(len(icons.base_icons()), 12)

    def test_base_icon_fields(self):
        for icon in icons.base_icons():
            self.assertTrue(icon["id"].startswith("base:"))
            self.assertTrue(icon["name"])
            self.assertTrue(icon["url"].startswith("/static/vendor/image/agents/"))
            self.assertTrue(icon["base"])

    def test_every_base_icon_file_exists(self):
        import os

        for icon in icons.base_icons():
            rel = icon["url"].lstrip("/")
            self.assertTrue(os.path.isfile(rel), f"нет файла {rel}")

    def test_base_url_resolution(self):
        self.assertEqual(
            icons.url("base:robot"), "/static/vendor/image/agents/robot.svg"
        )

    def test_unknown_base_slug_gives_empty_url(self):
        self.assertEqual(icons.url("base:nope"), "")

    def test_is_base(self):
        self.assertTrue(icons.is_base("base:robot"))
        self.assertFalse(icons.is_base("abc123"))
        self.assertFalse(icons.is_base(None))
        self.assertFalse(icons.is_base(""))

    def test_is_known_base(self):
        self.assertTrue(icons.is_known("base:robot"))
        self.assertFalse(icons.is_known("base:nope"))

    def test_is_known_uploaded(self):
        self.assertTrue(icons.is_known("abc", {"abc"}))
        self.assertFalse(icons.is_known("abc", {"def"}))
        self.assertFalse(icons.is_known("abc", None))

    def test_uploaded_url_unchanged(self):
        self.assertEqual(icons.url("deadbeef"), "/icons/deadbeef")

    def test_base_ids_match_base_icons(self):
        self.assertEqual(icons.base_ids(), {i["id"] for i in icons.base_icons()})


if __name__ == "__main__":
    unittest.main()
