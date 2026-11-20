"""Какие файлы принимаем в чат.

Агент читает документ и извлекает текст, поэтому принимаем только
текстовые форматы и PDF с текстовым слоем. Сканы без текста (картинка
внутри PDF) не подойдут — это предупреждаем в подсказке.
"""

import os

# Расширения в нижнем регистре. docx — основной формат, остальное —
# текстовые документы, которые умеет читать Read File.
ALLOWED_EXTENSIONS = (
    ".txt",
    ".md",
    ".csv",
    ".rtf",
    ".odt",
    ".docx",
    ".pdf",
)

# Для <input type="file" accept="...">
ACCEPT_ATTR = ",".join(ALLOWED_EXTENSIONS)

# Короткая подсказка для кнопки
HINT = "Можно прикреплять только текстовые документы: docx, txt, md, csv, rtf, odt и PDF с текстовым слоем"

# Развёрнутая подсказка для формы
DETAILED_HINT = (
    "Поддерживаются DOCX, TXT, MD, CSV, RTF, ODT и PDF с текстовым слоем. "
    "Сканы и фотографии документов не подойдут — в них нет текста, "
    "который можно прочитать."
)


def extension_of(filename: str | None) -> str:
    """Расширение файла в нижнем регистре, включая точку."""
    if not filename:
        return ""
    return os.path.splitext(filename)[1].lower()


def is_allowed(filename: str | None) -> bool:
    return extension_of(filename) in ALLOWED_EXTENSIONS


def rejected_message(filename: str | None) -> str:
    """Понятное сообщение для неподдерживаемого файла."""
    ext = extension_of(filename) or "без расширения"
    return (
        f"Файл «{filename or 'без имени'}» ({ext}) не поддерживается. "
        f"Разрешены: {', '.join(e.lstrip('.') for e in ALLOWED_EXTENSIONS)}."
    )
