"""Качество чанков: обрывы посреди слова и чанки из одной шапки.

Оба дефекта видны только на живой коллекции, но чинятся в парсере:

* ``_hard_split`` резал «портянку» (строку таблицы длиннее лимита) по
  символам, а не по словам. В коллекции ЕОСЗ так появились обрывки
  «осуществляетс», «классификаци», «распоря» — 634 child-чанка из 5003
  (13 %). Такой обрывок не находится поиском и портит цитату.
* Заголовок-хлебная крошка не имел лимита: в Приложении 10 путь из
  названий ГЛАВЫ/РАЗДЕЛА занимал 577 символов, бюджет тела кончался,
  и в индекс уходил чанк из одной шапки (61 штука). По векторам он похож
  на заголовок вопроса, но ответа в нём нет.

Компоненты не импортируются на хосте (нужен ``lfx``), поэтому чистые
функции достаём из исходника через AST и исполняем отдельно.
"""

import ast
import unittest
from pathlib import Path

PARSER = (
    Path(__file__).resolve().parent.parent
    / "langflow_components"
    / "eosz_structure_parser.py"
)


def load_functions(*names: str) -> dict:
    """Достаёт модульные функции из компонента, не импортируя его."""
    source = PARSER.read_text(encoding="utf-8")
    tree = ast.parse(source)
    picked = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    missing = set(names) - {node.name for node in picked}
    if missing:
        raise AssertionError(f"в {PARSER.name} нет функций: {sorted(missing)}")

    namespace: dict = {}
    module = ast.Module(body=picked, type_ignores=[])
    exec(compile(module, str(PARSER), "exec"), namespace)  # noqa: S102
    return namespace


@unittest.skipUnless(PARSER.is_file(), "компонент недоступен")
class SplitWordsTests(unittest.TestCase):
    def setUp(self):
        self.split_words = load_functions("split_words")["split_words"]

    def test_does_not_break_words(self):
        text = (
            "по типу оборудования ИКиП (классификация) либо по конкретному "
            "наименованию оборудования, являющегося предметом закупки"
        )
        parts = self.split_words(text, 40)

        self.assertGreater(len(parts), 1)
        # Ни одно слово исходника не разрезано.
        self.assertEqual(" ".join(parts).split(), text.split())

    def test_covers_whole_text(self):
        text = " ".join(f"слово{i}" for i in range(50))
        parts = self.split_words(text, 60)

        self.assertEqual(" ".join(parts).split(), text.split())

    def test_long_word_without_spaces_is_cut(self):
        # Одно слово длиннее лимита: иначе шаг нулевой и цикл не кончится.
        parts = self.split_words("а" * 100, 30)

        self.assertGreater(len(parts), 1)
        self.assertEqual("".join(parts), "а" * 100)

    def test_short_text_is_single_part(self):
        self.assertEqual(self.split_words("короткий текст", 200), ["короткий текст"])

    def test_hard_split_uses_split_words(self):
        source = PARSER.read_text(encoding="utf-8")
        hard_split = source[source.index("def _hard_split"):]
        hard_split = hard_split[: hard_split.index("def ", 10)]

        self.assertIn("split_words", hard_split)
        self.assertNotIn("sentence[i : i + step]", hard_split)


@unittest.skipUnless(PARSER.is_file(), "компонент недоступен")
class BreadcrumbTests(unittest.TestCase):
    def setUp(self):
        self.fit = load_functions("fit_breadcrumb")["fit_breadcrumb"]
        # Замена tiktoken: один «токен» на 3 символа.
        self.count = lambda text: max(1, len(text) // 3)

    def test_short_path_is_kept_verbatim(self):
        bits = ["ЕОСЗ (ред. 0)", "Приложение 9", "п. 2.1", "стр. 237"]
        self.assertEqual(self.fit(bits, 120, self.count), " › ".join(bits))

    def test_long_path_fits_the_limit(self):
        bits = [
            "ЕОСЗ (ред. 0)",
            "Приложение 10. Назначение и область применения",
            "ГЛАВА 1. ПОРЯДОК УСТАНОВЛЕНИЯ ТРЕБОВАНИЙ, КРИТЕРИЕВ ОЦЕНКИ И "
            "МЕТОДИКИ ОЦЕНКИ ЗАЯВОК ПРИ ФОРМИРОВАНИИ ДОКУМЕНТАЦИИ",
            "РАЗДЕЛ 1. ДОПУСТИМЫЕ К УСТАНОВЛЕНИЮ В ДОКУМЕНТАЦИИ О ЗАКУПКЕ "
            "ТРЕБОВАНИЯ К УЧАСТНИКАМ ЗАКУПКИ, СУБПОДРЯДЧИКАМ",
            "1.6. Требования при закупках до 200 млн руб.",
            "стр. 276",
        ]
        result = self.fit(bits, 87, self.count)

        self.assertLessEqual(self.count(result), 87)
        # Хвост важнее середины: пункт и страница должны остаться.
        self.assertIn("стр. 276", result)
        self.assertTrue(result.startswith("ЕОСЗ (ред. 0)"))

    def test_drops_middle_not_tail(self):
        bits = ["ЕОСЗ (ред. 0)", "А" * 200, "п. 1.6", "стр. 276"]
        result = self.fit(bits, 60, self.count)

        self.assertIn("п. 1.6", result)
        self.assertIn("стр. 276", result)
        self.assertNotIn("А" * 200, result)

    def test_single_long_segment_is_truncated(self):
        result = self.fit(["ЕОСЗ (ред. 0)", "Б" * 400], 30, self.count)

        self.assertLessEqual(self.count(result), 30)
        self.assertIn("…", result)


@unittest.skipUnless(PARSER.is_file(), "компонент недоступен")
class EmptyChunkGuardTests(unittest.TestCase):
    def test_emit_skips_chunk_without_body(self):
        source = PARSER.read_text(encoding="utf-8")
        emit = source[source.index("def _emit"):]
        emit = emit[: emit.index("def _new_parent_acc")]

        self.assertIn("if not body.strip():", emit)

    def test_trim_returns_empty_when_head_exceeds_limit(self):
        source = PARSER.read_text(encoding="utf-8")
        trim = source[source.index("def _trim_body_to_limit"):]
        trim = trim[: trim.index("def _accumulate_parent")]

        # Раньше возвращалось тело целиком, и _emit вырезал его до нуля,
        # оставляя чанк из одной шапки.
        self.assertNotIn("return body\n", trim)

    def test_breadcrumb_has_token_limit(self):
        source = PARSER.read_text(encoding="utf-8")

        self.assertIn("def _breadcrumb_limit", source)
        self.assertIn("fit_breadcrumb(bits, self._breadcrumb_limit()", source)


if __name__ == "__main__":
    unittest.main()
