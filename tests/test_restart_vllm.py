"""Скрипт перезапуска vLLM: порядок и защита от регрессий.

Генератор не может подняться, пока embedding держит память: свободно
20.19 ГиБ при потребности 20.45. Проверено на живом стенде. Поэтому
порядок «сначала генератор, потом embedding» — не стилистика, а условие
работоспособности, и он должен быть зафиксирован в коде, а не в памяти
того, кто дежурит.

Тест проверяет три вещи: синтаксис, порядок шагов и то, что скрипт
дожидается `healthy` генератора до старта embedding.
"""

import shutil
import subprocess
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "docker" / "restart-vllm.sh"


@unittest.skipUnless(SCRIPT.is_file(), "скрипт недоступен")
class RestartVllmScriptTests(unittest.TestCase):
    def setUp(self):
        self.text = SCRIPT.read_text(encoding="utf-8")

    def test_bash_syntax(self):
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("bash не найден")

        result = subprocess.run(
            [bash, "-n", str(SCRIPT)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_stops_embedding_before_starting_generator(self):
        # Индексы вхождений в тексте: остановка embedding должна быть
        # раньше запуска генератора.
        stop = self.text.index("stop vllm-embedding")
        start_generate = self.text.index("up -d --force-recreate vllm-generate")

        self.assertLess(stop, start_generate)

    def test_waits_for_generator_health_before_embedding(self):
        wait_generate = self.text.index("wait_healthy vllm-generate")
        start_embedding = self.text.index("up -d --force-recreate vllm-embedding")

        self.assertLess(wait_generate, start_embedding)

    def test_uses_compose_file_from_its_own_directory(self):
        # Скрипт запускают из docker/, но и из корня тоже — путь должен
        # считаться от расположения файла, а не от текущего каталога.
        self.assertIn("BASH_SOURCE", self.text)
        self.assertIn("docker-compose.yml", self.text)

    def test_fails_loudly_when_model_does_not_start(self):
        # Молчаливый выход 0 при неподнявшейся модели хуже падения.
        self.assertIn("set -euo pipefail", self.text)
        self.assertIn("return 1", self.text)


if __name__ == "__main__":
    unittest.main()
