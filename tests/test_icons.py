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


if __name__ == "__main__":
    unittest.main()
