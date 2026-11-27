"""Обрыв ответа на 6003 символах.

Langflow сериализует результат flow с ``MAX_TEXT_LENGTH = 6000``
(``lfx/serialization/constants.py``, применяется в
``lfx/utils/util_strings.py: truncate_long_strings``): всё, что длиннее,
приходит обрезанным посреди слова с «...» в конце. В чате это выглядело как
ответ, оборванный на полуслове («... | Зап...», «...request for propo...»).

Поток токенов через то же ограничение не проходит, поэтому приложение
обязано предпочитать более длинный текст.
"""

import unittest

from langflow_client import prefer_full_answer

# Ровно то, что лежит в llm_request_log: 6000 символов + "..."
TRUNCATED = "а" * 6000 + "..."
FULL = "а" * 9000


class PreferFullAnswerTests(unittest.TestCase):
    def test_prefers_streamed_over_truncated_final(self):
        self.assertEqual(prefer_full_answer(FULL, TRUNCATED), FULL)

    def test_keeps_final_when_it_is_longer(self):
        self.assertEqual(prefer_full_answer("коротко", FULL), FULL)

    def test_equal_lengths_take_final(self):
        # Финальный текст прошёл parse_thinking — в нём нет тегов.
        self.assertEqual(prefer_full_answer("тег", "да!"), "да!")

    def test_empty_final_falls_back_to_stream(self):
        self.assertEqual(prefer_full_answer(FULL, ""), FULL)
        self.assertEqual(prefer_full_answer(FULL, "   "), FULL)

    def test_empty_stream_falls_back_to_final(self):
        self.assertEqual(prefer_full_answer("", TRUNCATED), TRUNCATED)

    def test_both_empty(self):
        self.assertEqual(prefer_full_answer("", ""), "")


if __name__ == "__main__":
    unittest.main()
