"""Шапка таблицы: Docling помечает данные как column_header.

На стр. 29 ЕОСЗ Docling ставит ``column_header=True`` не только на
настоящую шапку, но и на первую строку данных:

    r2-3 c0-1 hdr=True | 1. Закупка продукции, влияющей на безопасность…
    r2-3 c1-2 hdr=True | запрос предложений/ конкурс
    r2-3 c3-4 hdr=True | конкурс

Парсер верил этой пометке и уносил строку в «Колонки:», а из данных
выкидывал. В результате чанк выглядел так:

    Колонки: Вид продукции / 1. Закупка продукции… | Плановая стоимость
    закупки, млн руб. с НДС / до 10 включи тельно / запрос предложений/
    конкурс | свыше 10 до 20 включительно | свыше 20 / конкурс

Модель читала «до 10 включительно» рядом с «запрос котировок» и отвечала,
что котировки возможны до 10 млн. На самом деле «до 10» относится к
запросу предложений и конкурсу для продукции, влияющей на безопасность.

Тест держит эвристику: строка с длинной ячейкой или с номером пункта —
это данные, даже если Docling пометил её шапкой.
"""

import ast
import re
import unittest
from pathlib import Path

PARSER = (
    Path(__file__).resolve().parent.parent
    / "langflow_components"
    / "eosz_structure_parser.py"
)


def load_methods(*names: str) -> dict:
    """Достаёт методы компонента, не импортируя его (нужен lfx)."""
    source = PARSER.read_text(encoding="utf-8")
    tree = ast.parse(source)
    picked = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    missing = set(names) - {node.name for node in picked}
    if missing:
        raise AssertionError(f"в {PARSER.name} нет методов: {sorted(missing)}")

    namespace: dict = {"RE_NUM_ITEM": None, "RE_LET_ITEM": None}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in (
                    "RE_NUM_ITEM", "RE_LET_ITEM",
                ):
                    namespace[target.id] = eval(  # noqa: S307
                        compile(ast.Expression(node.value), "<re>", "eval"),
                        {"re": re},
                    )
    module = ast.Module(body=picked, type_ignores=[])
    exec(compile(module, str(PARSER), "exec"), namespace)  # noqa: S102
    return namespace


@unittest.skipUnless(PARSER.is_file(), "компонент недоступен")
class RowLooksLikeDataTests(unittest.TestCase):
    def setUp(self):
        ns = load_methods("_row_looks_like_data")
        self.func = ns["_row_looks_like_data"]
        # Метод не использует self, кроме константы класса.
        self.obj = type("T", (), {"HEADER_CELL_CHARS": 60})()
        self.check = lambda row: self.func(self.obj, row)

    def test_real_header_is_not_data(self):
        row = ["Вид продукции", "до 10 включи тельно", "свыше 20"]
        self.assertFalse(self.check(row))

    def test_data_row_with_long_cell_is_data(self):
        row = [
            "1. Закупка продукции, влияющей на безопасность объектов "
            "использования атомной энергии",
            "запрос предложений/ конкурс",
            "",
            "конкурс",
        ]
        self.assertTrue(self.check(row))

    def test_row_with_clause_number_is_data(self):
        self.assertTrue(self.check(["2. Закупка работ (услуг)", "конкурс"]))

    def test_row_with_letter_item_is_data(self):
        self.assertTrue(self.check(["а) требования к продукции", "ТЗ"]))

    def test_empty_row_is_not_data(self):
        self.assertFalse(self.check(["", "", ""]))

    def test_numeric_column_codes_are_not_clause_numbers(self):
        # «20.14» — код ОКПД2, а не номер пункта: без текста после номера
        # RE_NUM_ITEM не срабатывает, длина короткая.
        self.assertFalse(self.check(["20.14", "Вещества химические"]))


@unittest.skipUnless(PARSER.is_file(), "компонент недоступен")
class HeaderFilteringTests(unittest.TestCase):
    def test_parser_returns_misplaced_rows_to_data(self):
        source = PARSER.read_text(encoding="utf-8")
        grid = source[source.index("def _table_grid"):]
        grid = grid[: grid.index("def _handle_table")]

        self.assertIn("_row_looks_like_data", grid)
        self.assertIn("remaining", grid)
        # Строки не должны молча пропадать: при пустом remaining
        # header_rows остаётся прежним.
        self.assertNotIn("header_rows = set()", grid)


if __name__ == "__main__":
    unittest.main()
