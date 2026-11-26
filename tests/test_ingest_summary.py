"""Формулировка итога ингеста: «ошибка» только когда есть потери.

Пользователь видел в результате грозное «Первая ошибка модели: 400 …
maximum context length is 1024 tokens», хотя пропущено было 0: усечение и
поштучный пересчёт батча — штатная деградация, данные не теряются.

Проверяем контракт по исходнику компонента: он встраивается в Langflow как
один файл и не импортируется на хосте (нет `lfx`), поэтому смотрим структуру
кода, а саму сборку строки прогоняем в контейнере Langflow.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "langflow_components" / "qdrant_hybrid_ingest.py"


@unittest.skipUnless(COMPONENT.is_file(), "компонент ингеста недоступен")
class IngestSummaryWordingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = COMPONENT.read_text(encoding="utf-8")
        start = src.index("def _build_summary")
        end = src.index("def ingest(self)", start)
        cls.body = src[start:end]

    def test_summary_is_built_by_one_helper(self):
        # Раньше строка собиралась инлайном внутри многосотстрочного метода —
        # её никто не проверял. Вынесение в метод позволяет тестировать.
        self.assertIn("def _build_summary", self.body)
        self.assertIn("Заметки: ", self.body)

    def test_model_error_only_in_losses_branch(self):
        # `_embed_error` должен попадать в текст только вместе с потерями.
        # Иначе при skipped=0 пользователь снова увидит «Первая ошибка модели».
        # Ищем именно обращение в коде, а не упоминание в докстринге.
        losses_at = self.body.index("if losses:")
        error_at = self.body.index('getattr(self, "_embed_error"')
        self.assertGreater(
            error_at, losses_at,
            "_embed_error должен упоминаться только внутри ветки потерь",
        )

    def test_truncation_goes_to_notes_not_losses(self):
        # Усечение — не потеря: в payload полный текст, BM25 по полному.
        notes_block = self.body[
            self.body.index("notes = []"):self.body.index("summary = (")
        ]
        self.assertIn("truncated", notes_block)
        self.assertIn("batch_failures", notes_block)
        self.assertNotIn("losses.append", notes_block)

    def test_notes_are_labelled_as_notes(self):
        # «Причины» подразумевают потери. Когда их нет — только «Заметки».
        self.assertIn('". Причины: "', self.body)
        self.assertIn('". Заметки: "', self.body)


if __name__ == "__main__":
    unittest.main()
