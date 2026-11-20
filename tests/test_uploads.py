"""Правила приёма файлов в чат."""

import unittest

import uploads


class AllowedFilesTests(unittest.TestCase):
    def test_docx_allowed(self):
        self.assertTrue(uploads.is_allowed("ТЗ.docx"))

    def test_text_formats_allowed(self):
        for name in ("a.txt", "a.md", "a.csv", "a.rtf", "a.odt", "a.pdf"):
            self.assertTrue(uploads.is_allowed(name), name)

    def test_extension_is_case_insensitive(self):
        self.assertTrue(uploads.is_allowed("Отчёт.DOCX"))
        self.assertTrue(uploads.is_allowed("scan.PDF"))

    def test_images_rejected(self):
        for name in ("photo.png", "scan.jpg", "anim.gif", "pic.webp"):
            self.assertFalse(uploads.is_allowed(name), name)

    def test_archives_and_binaries_rejected(self):
        for name in ("a.zip", "a.rar", "a.exe", "a.docx.exe"):
            self.assertFalse(uploads.is_allowed(name), name)

    def test_no_extension_rejected(self):
        self.assertFalse(uploads.is_allowed("README"))
        self.assertFalse(uploads.is_allowed(None))

    def test_rejected_message_mentions_file_and_formats(self):
        message = uploads.rejected_message("photo.png")

        self.assertIn("photo.png", message)
        self.assertIn(".png", message)
        self.assertIn("docx", message)

    def test_accept_attr_contains_all_extensions(self):
        for ext in uploads.ALLOWED_EXTENSIONS:
            self.assertIn(ext, uploads.ACCEPT_ATTR)

    def test_hints_mention_pdf_text_layer(self):
        self.assertIn("PDF", uploads.HINT)
        self.assertIn("текстовым слоем", uploads.DETAILED_HINT)


if __name__ == "__main__":
    unittest.main()
