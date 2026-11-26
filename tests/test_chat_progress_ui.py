"""Прогресс ингеста в чате: индикатор задачи показывает стадию, процент и ETA.

Прогресс рисуется в браузере, поэтому проверяем контракт между шаблоном и
бэкендом: все стадии, которые реально шлют компоненты Langflow, должны иметь
подпись в ``STAGE_LABELS``, а индикатор — получать данные из ``/api/jobs/{id}``.

Если тест упал на «нет подписи для стадии» — значит в компоненте появилась
новая стадия, и её нужно добавить в ``templates/chat.html`` (и в админку),
иначе пользователь увидит сырое английское слово вместо «разбор PDF».
"""

import re
import unittest
from pathlib import Path
from types import SimpleNamespace

from templating import templates

ROOT = Path(__file__).resolve().parent.parent
COMPONENTS = ROOT / "langflow_components"


def render_chat() -> str:
    """Рендерит страницу чата с минимальным контекстом."""
    ctx = {
        "flow_id": "flow-1",
        "chat": SimpleNamespace(id="chat-1"),
        "files_allowed": [".pdf"],
        "files_detailed_hint": "",
        "files_accept": ".pdf",
        "files_hint": "",
        "chats": [],
    }
    return templates.TemplateResponse("chat.html", ctx).body.decode()


def stage_labels(html: str) -> dict:
    """Достаёт объект ``STAGE_LABELS`` из отрендеренного скрипта."""
    block = re.search(r"const STAGE_LABELS = \{(.*?)\};", html, re.S)
    if not block:
        return {}
    return dict(re.findall(r"(\w+)\s*:\s*'([^']*)'", block.group(1)))


def component_stages() -> set:
    """Стадии, которые компоненты отправляют в приложение."""
    stages = set()
    for path in COMPONENTS.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        stages.update(
            re.findall(r"_send_progress\(\s*[\"'](\w+)[\"']", source)
        )
    return stages


class ChatProgressMarkupTests(unittest.TestCase):
    def setUp(self):
        self.html = render_chat()

    def test_indicator_has_progress_elements(self):
        # Без этих узлов прогресс некуда рисовать: останется только спиннер.
        for probe in ("job-stage", "job-bar", "job-bar-fill", "job-meta", "job-stale"):
            self.assertIn(probe, self.html)

    def test_poll_redraws_indicator_while_running(self):
        # Опрос /api/jobs/{id} должен перерисовывать индикатор, а не только
        # менять текст «Обработка…».
        self.assertIn("renderJobProgress(status, job)", self.html)

    def test_old_text_only_switch_is_gone(self):
        # Раньше здесь подменялся текст единственного span. Если этот шаблон
        # вернётся, прогресс снова перестанет отображаться.
        self.assertNotIn("status.querySelector('span').textContent", self.html)

    def test_eta_uses_server_side_deadline(self):
        # «осталось N секунд» устаревает между опросами — показываем время
        # окончания, посчитанное сервером.
        self.assertIn("p.eta_at", self.html)
        self.assertIn("осталось ~", self.html)


class StageLabelCoverageTests(unittest.TestCase):
    """Каждая стадия компонентов должна быть переведена для пользователя."""

    @unittest.skipUnless(COMPONENTS.is_dir(), "каталог компонентов недоступен")
    def test_every_component_stage_has_label(self):
        labels = stage_labels(render_chat())
        self.assertTrue(labels, "STAGE_LABELS не найден в шаблоне чата")

        missing = sorted(component_stages() - set(labels))
        self.assertEqual(
            missing, [],
            "в chat.html нет подписи для стадий: %s" % ", ".join(missing),
        )

    @unittest.skipUnless(COMPONENTS.is_dir(), "каталог компонентов недоступен")
    def test_labels_are_human_readable(self):
        labels = stage_labels(render_chat())
        for stage, label in labels.items():
            self.assertTrue(label.strip(), "пустая подпись для стадии %s" % stage)
            # Английское слово вместо перевода — признак забытой подписи.
            self.assertNotEqual(
                label.lower(), stage.lower(),
                "стадия %s осталась без перевода" % stage,
            )


if __name__ == "__main__":
    unittest.main()
