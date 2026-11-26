"""Согласованность лимитов парсера, ингеста и моделей.

Три лимита связаны, и разъехавшись они дают либо 400 от модели, либо
молчаливое усечение (когда вектор считается по обрезанному тексту):

* ``max_child_tokens`` — размер того, что ищется вектором;
* ``max_parent_chars`` — размер parent, который тоже эмбеддится и вдобавок
  уходит в промпт при expand-to-parent;
* ``max_embed_chars`` — сколько символов отдаётся модели эмбеддингов.

Компоненты встраиваются в Langflow как файлы и на хосте не импортируются
(нет ``lfx``), поэтому проверяем исходники: значения по умолчанию и наличие
защиты в ``_emit``.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PARSER = ROOT / "langflow_components" / "eosz_structure_parser.py"
INGEST = ROOT / "langflow_components" / "qdrant_hybrid_ingest.py"

# Замеры по коллекции ЕОСЗ (3836 child, 1421 parent):
# медиана 2.12 символа на токен, худший случай на плотных таблицах — 1.35.
RATIO_TYPICAL = 2.12
RATIO_WORST = 1.35

EMBED_MAX_TOKENS = 4096    # --max-model-len у vllm-embedding
GEN_MAX_TOKENS = 12288     # --max-model-len у vllm-generate


def default_of(path: Path, name: str) -> int:
    """Значение по умолчанию для IntInput с указанным именем."""
    src = path.read_text(encoding="utf-8")
    m = re.search(
        r'name="%s",\s*\n\s*display_name=[^\n]*\n\s*value=(\d+)' % re.escape(name),
        src,
    )
    if not m:
        raise AssertionError("не найден дефолт для %s в %s" % (name, path.name))
    return int(m.group(1))


@unittest.skipUnless(PARSER.is_file() and INGEST.is_file(), "компоненты недоступны")
class ParserLimitTests(unittest.TestCase):
    def test_parent_fits_embedding_model(self):
        chars = default_of(PARSER, "max_parent_chars")
        tokens = chars / RATIO_WORST

        self.assertLess(
            tokens, EMBED_MAX_TOKENS,
            "parent в %d символов даёт ~%d токенов при худшем соотношении — "
            "это больше лимита модели %d" % (chars, tokens, EMBED_MAX_TOKENS),
        )

    def test_ingest_does_not_truncate_parents(self):
        # Если max_embed_chars меньше max_parent_chars, часть parent уйдёт в
        # модель усечённой — вектор будет считаться по началу текста.
        parent_chars = default_of(PARSER, "max_parent_chars")
        embed_chars = default_of(INGEST, "max_embed_chars")

        self.assertGreaterEqual(
            embed_chars, parent_chars,
            "max_embed_chars=%d меньше max_parent_chars=%d: parent будут "
            "усекаться" % (embed_chars, parent_chars),
        )

    def test_parent_fits_generation_context(self):
        # Ретривер тянет 4 parent (expand_parents) + 4 child + промпт + ответ.
        parent_chars = default_of(PARSER, "max_parent_chars")
        child_tokens = default_of(PARSER, "max_child_tokens")
        parents = 4 * parent_chars / RATIO_TYPICAL
        children = 4 * child_tokens
        overhead = 3000          # системный промпт и запас на ответ

        total = parents + children + overhead
        self.assertLess(
            total, GEN_MAX_TOKENS,
            "промпт с 4 parent по %d символов занимает ~%d токенов из %d"
            % (parent_chars, total, GEN_MAX_TOKENS),
        )

    def test_child_limit_is_enforced_with_breadcrumb(self):
        # Регрессия: лимит считался по «голому» телу, а breadcrumb добавлялся
        # в _emit после нарезки — 51% child выходили длиннее лимита
        # (p50 355 при 350, максимум 1781).
        src = PARSER.read_text(encoding="utf-8")
        emit = src[src.index("def _emit"):src.index("def _trim_body_to_limit")]

        self.assertIn('if chunk_type == "child":', emit)
        self.assertIn("self.max_child_tokens", emit)
        self.assertIn("_trim_body_to_limit", emit)

    def test_chunk_parts_reserves_room_for_breadcrumb(self):
        src = PARSER.read_text(encoding="utf-8")
        sig = re.search(r"def _chunk_parts\(self[^)]*\)", src)

        self.assertIsNotNone(sig)
        self.assertIn("reserve", sig.group(0))


if __name__ == "__main__":
    unittest.main()
