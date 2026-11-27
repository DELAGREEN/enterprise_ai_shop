"""System prompt для Agent в чат-флоу ЕОСЗ.

У Agent поле ``system_prompt`` было пустым, и Langflow подставлял свой
английский дефолт «You are a Langflow Agent…». Он уходил в system-роль и
перебивал русские инструкции, которые лежат в user-сообщении (вывод
Prompt Template). Наблюдаемые следствия: модель подставляла название
документа из своих знаний («ГОСТ Р 56.0-2024» вместо ЕОСЗ) и вставляла
английские слова в русский текст («в документации **this** указано»).

Тест держит три свойства промпта, без которых дефект возвращается:
он на русском, он запрещает брать название документа из ссылок внутри
текста и он требует брать название из начала фрагмента.
"""

import unittest

from langflow_components.fix_agent_prompt import SYSTEM_PROMPT


class SystemPromptTests(unittest.TestCase):
    def test_is_russian(self):
        # Английский system prompt — исходная причина дефекта.
        self.assertNotIn("You are", SYSTEM_PROMPT)
        self.assertIn("документации", SYSTEM_PROMPT)

    def test_forbids_taking_document_name_from_references(self):
        lowered = SYSTEM_PROMPT.lower()
        self.assertIn("гост", lowered)
        self.assertIn("ссылк", lowered)

    def test_requires_verbatim_name_from_fragment_start(self):
        self.assertIn("дословно", SYSTEM_PROMPT.lower())
        self.assertIn("›", SYSTEM_PROMPT)

    def test_forbids_english_words_in_answer(self):
        self.assertIn("английских", SYSTEM_PROMPT.lower())

    def test_is_short_enough_for_a_system_prompt(self):
        # System prompt идёт в каждый запрос; он не должен конкурировать
        # по объёму с самим контекстом.
        self.assertLess(len(SYSTEM_PROMPT), 800)


if __name__ == "__main__":
    unittest.main()
