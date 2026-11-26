"""Структурный парсер нормативных документов для Langflow (варианты B + C).

Компонент принимает файл из «Read File», разбирает PDF через Docling
(layout + TableFormer, без OCR) и отдаёт список ``Data``, который без
доработок принимает «Qdrant Hybrid Ingest».

Прогресс. Пока идёт разбор, компонент отправляет его в enterprise_ai_shop
(``progress_url`` из tweaks). Источник данных — ``progress_callback`` самого
Docling: событие ``PageCompletedProgress`` содержит ``completed_pages`` и
``total_pages``, поэтому видно «страница 240 из 673», а не просто «running».
Отправка троттлится и никогда не роняет разбор.

Почему не «нарезать по 500 токенов» — проверено на Едином отраслевом
стандарте закупок (673 стр., 84 % объёма — приложения):

* номера пунктов лежат в ``item.orig``; в ``item.text`` Docling их выбрасывает
  («1.1. не применяются…» -> «не применяются…»), а без номера невозможна
  цитата «п. 2.1»;
* иерархия восстанавливается по ``section_header``: ГЛАВА / Статья /
  Приложение / Раздел / подзаголовок;
* таблицы собираются из ``table_cells`` с раскрытием ``row_span`` /
  ``col_span`` (в Приложении 9 ячейка «Оборудование компьютерное…»
  растянута на 12 строк — без раскрытия коды ОКПД2 остаются без названия);
* сноски под таблицей приходят отдельными ``footnote`` на той же странице и
  содержат условие применения («запрос котировок, если НМЦ ≤ 10 млн руб.»),
  поэтому они приклеиваются к таблице и повторяются в каждом её чанке;
* каждый чанк несёт breadcrumbs, номер пункта и печатную страницу, чтобы
  ответ мог цитировать «Приложение 12, п. 2.1, стр. 366».

Child-чанк — 250–400 токенов (то, что эмбеддится), parent-чанк — статья или
группа пунктов до 1800 токенов (то, что уходит в LLM при expand-to-parent).
"""

from __future__ import annotations

import hashlib
import os
import re
import time
import uuid

from lfx.custom.custom_component.component import Component
from lfx.io import BoolInput, HandleInput, IntInput, Output, StrInput
from langflow.schema import Data

# ---------------------------------------------------------------- разметка ---

RE_PAGE_NUMBER = re.compile(r"^\s*\d{1,3}\s*$")
RE_CHAPTER = re.compile(r"^\s*ГЛАВА\s+(\d+)\s*[.:]?\s*(.*)$", re.IGNORECASE)
RE_ARTICLE = re.compile(r"^\s*Статья\s+(\d+(?:\.\d+)*)\s*[.:]?\s*(.*)$", re.IGNORECASE)
RE_RAZDEL = re.compile(r"^\s*Раздел\s+(\d+)\s*[.:]?\s*(.*)$", re.IGNORECASE)

# «Приложение 9 к Единому отраслевому стандарту…» — колонтитул, повторяется
# на каждой странице приложения.
RE_APPENDIX_STD = re.compile(
    r"^\s*Приложение\s+(?:№\s*)?(\d+)\s+к\s+Единому\s+отраслевому", re.IGNORECASE
)
# «Приложение № 1 к Методике» — вложенное приложение внутри Приложения 8.
RE_APPENDIX_INNER = re.compile(
    r"^\s*Приложение\s+(?:№\s*)?(\d+)\s+к\s+Методике", re.IGNORECASE
)
RE_APPENDIX_BARE = re.compile(r"^\s*Приложение\s+(?:№\s*)?(\d+)\s*$", re.IGNORECASE)

# Нумерация пунктов внутри статьи/приложения.
RE_NUM_ITEM = re.compile(r"^\s*(\d+(?:\.\d+)*)\.?\s+(?=\S)")
RE_LET_ITEM = re.compile(r"^\s*([а-яё])\)\s+(?=\S)")
RE_DASH_ITEM = re.compile(r"^\s*[-\u2013\u2014]\s+(?=\S)")

RE_DATE = re.compile(r"от\s+(\d{1,2})\s+([а-яё]+)\s+(\d{4})", re.IGNORECASE)
RE_TOKEN_JOIN = re.compile(r"\s+")

MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11,
    "декабря": 12,
}

# Перекрёстные ссылки: «п. 4.2.2», «статьей 2.3», «ст. 7.12», «приложении № 9».
REF_PATTERNS = (
    (re.compile(r"(?<![\w.])пп?\.\s*(\d+(?:\.\d+)*)"), "clause"),
    (re.compile(r"стать[яиеюёй]+\s+(\d+(?:\.\d+)*)", re.IGNORECASE), "article"),
    (re.compile(r"(?<![\w.])ст\.\s*(\d+(?:\.\d+)*)"), "article"),
    (re.compile(r"подраздел[а-яё]*\s+(\d+(?:\.\d+)*)"), "subsection"),
    (re.compile(r"приложени[а-яё]*\s+(?:№\s*)?(\d+)", re.IGNORECASE), "appendix"),
    (re.compile(r"глав[аеуы]\s+(\d+)", re.IGNORECASE), "chapter"),
)

LABEL_PAGE_FURNITURE = {"page_header", "page_footer", "document_index"}


class EoszStructureParser(Component):
    display_name = "EOSZ Structure Parser (B+C)"
    description = (
        "Структурный парсер нормативных документов: пункты, статьи, приложения, "
        "таблицы со сносками. Отдаёт child- и parent-чанки для Qdrant."
    )
    icon = "file-text"

    _converter = None
    _enc = None

    inputs = [
        HandleInput(
            name="file_input",
            display_name="File from Read File",
            input_types=["Message", "DataFrame"],
            info="Подключите выход File Path или Files компонента Read File.",
        ),
        StrInput(
            name="job_id",
            display_name="Job ID (прогресс)",
            value="",
            info=(
                "Заполняется автоматически приложением через tweaks. "
                "Пусто — прогресс никуда не отправляется, компонент работает "
                "как обычно."
            ),
        ),
        StrInput(
            name="progress_url",
            display_name="Progress URL",
            value="",
            info=(
                "Полный адрес приёмника прогресса, например "
                "http://host.docker.internal:5001/api/jobs/<id>/progress. "
                "Подставляется приложением через tweaks — вручную заполнять "
                "не нужно. host.docker.internal работает только при наличии "
                "extra_hosts: host.docker.internal:host-gateway у сервиса "
                "langflow в compose."
            ),
        ),
        StrInput(
            name="progress_token",
            display_name="Progress token",
            value="",
            info="Общий секрет для приёмника прогресса. Подставляется через tweaks.",
        ),
        IntInput(
            name="progress_interval",
            display_name="Progress interval (s)",
            value=3,
            info=(
                "Как часто отправлять прогресс. 0 — отключить. Слишком частое "
                "обновление (на каждую страницу) создаёт лишние запросы: на "
                "673 страницах это 673 POST."
            ),
        ),
        StrInput(
            name="doc_id",
            display_name="Document ID",
            value="eosz",
            info=(
                "Стабильный идентификатор документа. По нему ретривер "
                "фильтрует коллекцию, а Qdrant Doc Admin удаляет старые "
                "редакции. Для другого НПА задайте своё значение."
            ),
        ),
        StrInput(
            name="doc_title",
            display_name="Document title",
            value="Единый отраслевой стандарт закупок",
            info="Полное название — попадает в payload и в начало каждого чанка.",
        ),
        StrInput(
            name="doc_short",
            display_name="Short title",
            value="ЕОСЗ",
            info="Короткое название для breadcrumbs в тексте чанка.",
        ),
        StrInput(
            name="doc_version",
            display_name="Document version",
            value="",
            info=(
                "Версия/редакция. Пусто — определяется автоматически по дате "
                "последнего изменения на титуле (например 2020-07-17). Версия "
                "входит в chunk_index, поэтому новая редакция получает новые "
                "point id, а повторный ингест той же редакции — те же самые."
            ),
        ),
        IntInput(
            name="max_child_tokens",
            display_name="Max child tokens",
            value=350,
            info=(
                "Размер child-чанка (то, что эмбеддится и ищется). Для "
                "нормативки 250–400 — рабочий диапазон: пункт целиком, но без "
                "склейки соседних статей."
            ),
        ),
        IntInput(
            name="max_parent_tokens",
            display_name="Max parent tokens",
            value=1800,
            info=(
                "Размер parent-чанка в токенах tiktoken. Parent не ищется "
                "вектором, но ингест его всё равно эмбеддит, поэтому реальный "
                "потолок задаёт max_parent_chars."
            ),
        ),
        IntInput(
            name="max_parent_chars",
            display_name="Max parent chars",
            value=2000,
            info=(
                "Жёсткий лимит parent в символах. Нужен из-за модели "
                "эмбеддингов: у Qwen3-Embedding-0.6B max_model_len=1024 "
                "токена, а русский юридический текст даёт ~2400–2800 символов "
                "на этот лимит. 2000 — безопасный запас. Если поднимете "
                "--max-model-len у vLLM до 4096, увеличьте и это значение."
            ),
        ),
        IntInput(
            name="page_from",
            display_name="Page from (PDF)",
            value=0,
            info=(
                "Первая страница PDF для пакетного прогона (0 — с начала). "
                "Документ на 673 страницы не проходит в один job: запускайте "
                "пакетами по 40–60 страниц."
            ),
        ),
        IntInput(
            name="page_to",
            display_name="Page to (PDF)",
            value=0,
            info="Последняя страница пакета включительно (0 — до конца).",
        ),
        BoolInput(
            name="attach_footnotes",
            display_name="Attach footnotes",
            value=True,
            info=(
                "Приклеивать сноски страницы к таблице. Отключать только для "
                "отладки: именно в сносках лежат условия применения норм."
            ),
        ),
        BoolInput(
            name="emit_parents",
            display_name="Emit parent chunks",
            value=True,
            info=(
                "Отдавать parent-чанки (chunk_type=parent). Их не ищут "
                "вектором, их подтягивает ретривер по parent_id."
            ),
        ),
        BoolInput(
            name="extract_refs",
            display_name="Extract cross-references",
            value=True,
            info=(
                "Извлекать перекрёстные ссылки (п. 4.2.2, ст. 7.12, "
                "приложение № 9) в payload refs_raw / ref_keys — основа для "
                "1-hop расширения в ретривере."
            ),
        ),
        BoolInput(
            name="emit_tables_as_rows",
            display_name="Table chunks by rows",
            value=True,
            info=(
                "Дробить большие таблицы по строкам с повтором шапки. Если "
                "выключить, таблица сверх лимита режется как обычный текст."
            ),
        ),
    ]

    outputs = [
        Output(name="chunks", display_name="Parsed Chunks", method="parse"),
    ]

    # ------------------------------------------------------------ прогресс ---

    def _progress_enabled(self) -> bool:
        return bool(
            (getattr(self, "progress_url", "") or "").strip()
            and getattr(self, "progress_interval", 3)
            and int(getattr(self, "progress_interval", 3) or 0) > 0
        )

    def _send_progress(
        self, stage: str, done=None, total=None, message=None, force=False
    ) -> None:
        """Отправляет прогресс в enterprise_ai_shop. Никогда не бросает.

        Прогресс — вспомогательная вещь: если приложение недоступно или
        ответило ошибкой, разбор обязан продолжиться. Поэтому здесь нет
        ни одного ``raise``, только лог.

        Троттлинг: Docling зовёт колбэк на каждую страницу, а на 673
        страницах это 673 HTTP-запроса. Отправляем не чаще, чем раз в
        ``progress_interval`` секунд, плюс всегда — финальные стадии.
        """
        if not self._progress_enabled():
            return

        url = str(getattr(self, "progress_url", "")).strip()
        interval = int(getattr(self, "progress_interval", 3) or 0)

        now = time.monotonic()
        last = getattr(self, "_progress_last_sent", 0.0)
        if not force and (now - last) < interval:
            return
        self._progress_last_sent = now

        payload = {"stage": stage}
        if done is not None:
            payload["done"] = int(done)
        if total is not None:
            payload["total"] = int(total)
        if message:
            payload["message"] = str(message)[:500]

        eta = self._estimate_eta(stage, done, total, now)
        if eta is not None:
            payload["eta_seconds"] = eta

        token = str(getattr(self, "progress_token", "") or "").strip()
        headers = {"X-Progress-Token": token} if token else {}

        try:
            import httpx

            httpx.post(
                url,
                json=payload,
                headers=headers,
                timeout=httpx.Timeout(2.0, connect=1.0),
            )
        except Exception as exc:  # noqa: BLE001 - прогресс не должен ломать разбор
            self.log(f"Прогресс не отправлен ({stage}): {exc}")

    def _estimate_eta(self, stage, done, total, now: float):
        """Сколько осталось по средней скорости с начала этапа.

        Средняя, а не мгновенная: на таблицах Docling замедляется, на
        текстовых страницах ускоряется, и «осталось 12 секунд / 40 минут»
        вперемешку выглядят как баг.

        Возвращает None, если оценивать нечего: нет ``total``, сделано 0
        или прошло слишком мало времени.
        """
        if not done or not total or done >= total:
            return 0 if done and total and done >= total else None

        start = getattr(self, "_progress_stage_start", None)
        if start is None or getattr(self, "_progress_stage_name", None) != stage:
            self._progress_stage_start = now
            self._progress_stage_name = stage
            return None

        elapsed = now - start
        if elapsed < 3:
            return None

        rate = done / elapsed
        if rate <= 0:
            return None

        return max(0, min(int((total - done) / rate), 24 * 3600))

    def _make_progress_callback(self):
        """Колбэк Docling: страницы и фазы разбора.

        ``PageCompletedProgress`` даёт ``completed_pages`` и ``total_pages`` —
        ровно то, что нужно для «240 из 673».
        """
        if not self._progress_enabled():
            return None

        def callback(event) -> None:
            try:
                kind = getattr(event, "kind", None)
                if kind == "page_completed":
                    self._send_progress(
                        "parsing",
                        getattr(event, "completed_pages", None),
                        getattr(event, "total_pages", None),
                        f"разбор страниц: {getattr(event, 'page_no', '?')}",
                    )
                elif kind == "phase_started":
                    phase = str(getattr(event, "phase", "") or "")
                    if phase:
                        self._send_progress("parsing", message=f"этап: {phase}")
            except Exception:  # noqa: BLE001
                pass

        return callback


    @classmethod
    def _get_converter(cls):
        """Конвертер Docling: layout + TableFormer, OCR выключен.

        В этом документе есть текстовый слой (проверено: 1 картинка на
        673 страницы), поэтому OCR только замедляет разбор на порядок.
        """
        if cls._converter is None:
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import (
                PdfPipelineOptions,
                TableFormerMode,
            )
            from docling.document_converter import DocumentConverter, PdfFormatOption

            opts = PdfPipelineOptions()
            opts.do_ocr = False
            opts.do_table_structure = True
            opts.table_structure_options.mode = TableFormerMode.ACCURATE
            opts.document_timeout = 7200

            cls._converter = DocumentConverter(
                format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
            )
        return cls._converter

    # -------------------------------------------------------------- токены ---

    @classmethod
    def _get_encoder(cls):
        if cls._enc is None:
            try:
                import tiktoken

                cls._enc = tiktoken.get_encoding("cl100k_base")
            except Exception:
                cls._enc = False
        return cls._enc

    @classmethod
    def count_tokens(cls, text: str) -> int:
        if not text:
            return 0
        enc = cls._get_encoder()
        if enc:
            try:
                return len(enc.encode(text))
            except Exception:
                pass
        return max(1, int(len(text) / 2.5))

    @staticmethod
    def _norm(text) -> str:
        """Схлопывает переносы строк и повторные пробелы."""
        if text is None:
            return ""
        value = str(text).replace("\u00ad", "")
        return RE_TOKEN_JOIN.sub(" ", value).strip()

    # --------------------------------------------------------------- state ---

    def _reset_state(self, meta: dict) -> None:
        self._meta = meta
        self._chunks: list[Data] = []
        self._idx = 0
        self._ctx = {
            "chapter": None,
            "chapter_no": None,
            "article": None,
            "article_no": None,
            "razdel": None,
            "appendix_no": None,
            "appendix_title": None,
            "inner_appendix_no": None,
            "subsection": None,
        }
        self._clause: dict | None = None
        self._loose: list[str] = []
        self._pending_table: dict | None = None
        self._pending_caption: str | None = None
        self._last_header: str | None = None
        self._active_parent: str | None = None
        self._table_carry: dict | None = None
        self._parents: dict[str, dict] = {}

    # ------------------------------------------------------------ разметка ---

    def _section_path(self) -> list[str]:
        """Иерархия текущего места в документе (breadcrumbs и payload)."""
        ctx = self._ctx
        path: list[str] = []
        if ctx["appendix_no"]:
            if ctx["appendix_title"]:
                path.append(f"Приложение {ctx['appendix_no']}. {ctx['appendix_title']}")
            else:
                path.append(f"Приложение {ctx['appendix_no']}")
            if ctx["inner_appendix_no"]:
                path.append(f"Приложение {ctx['inner_appendix_no']} к Методике")
        if ctx["chapter"]:
            path.append(ctx["chapter"])
        if ctx["razdel"]:
            path.append(ctx["razdel"])
        if ctx["article"]:
            path.append(ctx["article"])
        if ctx["subsection"]:
            path.append(ctx["subsection"])
        return path

    def _scope_meta(self) -> dict:
        """Метаданные места в документе — общая часть для всех чанков."""
        ctx = self._ctx
        return {
            "doc_id": self._meta["doc_id"],
            "doc_title": self._meta["doc_title"],
            "doc_version": self._meta["doc_version"],
            "doc_hash": self._meta["doc_hash"],
            "filename": self._meta["filename"],
            "file_path": self._meta["file_path"],
            "section_path": self._section_path(),
            "chapter": ctx["chapter"],
            "chapter_no": ctx["chapter_no"],
            "article": ctx["article"],
            "article_no": ctx["article_no"],
            "razdel": ctx["razdel"],
            "appendix_no": ctx["appendix_no"],
            "appendix_inner_no": ctx["inner_appendix_no"],
        }

    def _breadcrumb(self, meta: dict, page_from, page_to) -> str:
        bits = [f"{self._meta['doc_short']} (ред. {self._meta['doc_version']})"]
        bits.extend(meta.get("section_path") or [])
        clause = meta.get("clause_number")
        if clause:
            bits.append(f"п. {clause}")
        if page_from:
            if page_to and page_to != page_from:
                bits.append(f"стр. {page_from}–{page_to}")
            else:
                bits.append(f"стр. {page_from}")
        return " › ".join(bits)

    # ------------------------------------------------------------ emission ---

    def _emit(self, body: str, meta: dict, chunk_type: str, page_from, page_to) -> None:
        body = body.strip()
        if not body:
            return

        head = self._breadcrumb(meta, page_from, page_to)
        text = f"{head}\n\n{body}"

        data = dict(meta)
        data["chunk_type"] = chunk_type
        data["chunk_index"] = f"{self._meta['doc_version']}:{self._idx}"
        data["token_count"] = self.count_tokens(text)
        data["page_from"] = page_from
        data["page_to"] = page_to
        data["breadcrumb"] = head

        self._chunks.append(Data(text=text, data=data))
        self._idx += 1

        if chunk_type == "child" and self.emit_parents:
            self._accumulate_parent(body, meta, page_from, page_to)

    @staticmethod
    def _new_parent_acc(meta: dict) -> dict:
        cleaned = {
            k: v
            for k, v in meta.items()
            if k not in ("clause_number", "part", "row_range", "row_index")
        }
        return {"texts": [], "tokens": 0, "chars": 0, "meta": cleaned, "pages": []}

    def _accumulate_parent(self, body: str, meta: dict, page_from, page_to) -> None:
        """Копит текст child-чанков в parent до лимита токенов или символов.

        parent_key — статья для текста и таблица для таблицы. Когда лимит
        исчерпан, parent закрывается и открывается следующий сегмент:
        иначе Приложение 12 склеилось бы в один чанк на 200 тысяч токенов.

        Лимит в символах (``max_parent_chars``) нужен отдельно: parent не
        ищется вектором, но ингест его всё равно эмбеддит, а модель
        эмбеддингов режет запрос по max_model_len.
        """
        key = meta.get("parent_key") or "root"

        # Смена статьи/приложения/таблицы закрывает предыдущий parent,
        # чтобы порядок parent-чанков совпадал с порядком документа.
        if self._active_parent and self._active_parent != key:
            self._flush_parent(self._active_parent)
        self._active_parent = key

        acc = self._parents.get(key)
        if acc is None:
            acc = self._new_parent_acc(meta)
            self._parents[key] = acc

        extra = self.count_tokens(body)
        extra_chars = len(body)
        over_tokens = acc["tokens"] + extra > self.max_parent_tokens
        char_limit = self._parent_char_limit
        over_chars = char_limit > 0 and acc["chars"] + extra_chars > char_limit
        if acc["texts"] and (over_tokens or over_chars):
            self._flush_parent(key)
            acc = self._new_parent_acc(meta)
            self._parents[key] = acc

        acc["texts"].append(body)
        acc["tokens"] += extra
        acc["chars"] += extra_chars
        acc["pages"].extend([p for p in (page_from, page_to) if p])

    @property
    def _parent_char_limit(self) -> int:
        """Лимит parent в символах (0 — без ограничения)."""
        return int(getattr(self, "max_parent_chars", 0) or 0)

    def _split_for_embedding(self, text: str, reserve: int = 0) -> list[str]:
        """Делит parent так, чтобы он проходил в модель эмбеддингов.

        Один child может быть крупнее лимита (таблица со сносками, длинный
        пункт), поэтому одной проверки при накоплении мало. Режем по
        абзацам, а внутри абзаца — по предложениям.

        ``reserve`` — место под breadcrumbs, которые ``_emit`` добавит к
        тексту: лимит модели считается по итоговой строке, а не по body.
        """
        limit = self._parent_char_limit
        if limit > 0:
            limit = max(400, limit - reserve)
        if limit <= 0 or len(text) <= limit:
            return [text]

        parts: list[str] = []
        buf: list[str] = []
        cur = 0

        for paragraph in text.split("\n\n"):
            if len(paragraph) > limit:
                if buf:
                    parts.append("\n\n".join(buf))
                    buf, cur = [], 0
                for sub in self._hard_split(paragraph, max(120, limit // 3)):
                    while len(sub) > limit:
                        parts.append(sub[:limit])
                        sub = sub[limit:]
                    if sub:
                        parts.append(sub)
                continue

            if cur + len(paragraph) + 2 > limit and buf:
                parts.append("\n\n".join(buf))
                buf, cur = [], 0
            buf.append(paragraph)
            cur += len(paragraph) + 2

        if buf:
            parts.append("\n\n".join(buf))
        return [p for p in parts if p.strip()]

    def _flush_parent(self, key: str) -> None:
        acc = self._parents.pop(key, None)
        if not acc or not acc["texts"]:
            return
        # Один child уже содержит весь parent — дублировать нечего.
        if len(acc["texts"]) < 2:
            return

        pages = [p for p in acc["pages"] if p]
        meta = dict(acc["meta"])
        meta["parent_key"] = key
        meta["parent_id"] = str(
            uuid.uuid5(
                uuid.NAMESPACE_DNS,
                f"{self._meta['doc_id']}::{self._meta['doc_version']}::{key}",
            )
        )

        segments = self._split_for_embedding(
            "\n\n".join(acc["texts"]),
            reserve=len(self._breadcrumb(meta, min(pages) if pages else None,
                                         max(pages) if pages else None)) + 2,
        )
        for index, segment in enumerate(segments):
            segment_meta = dict(meta)
            if len(segments) > 1:
                # Первый сегмент сохраняет исходный parent_id: на него
                # ссылаются child-чанки, и expand-to-parent должен находить
                # начало статьи. Остальные сегменты получают свой id.
                segment_meta["parent_part"] = index
                if index > 0:
                    segment_meta["parent_id"] = str(
                        uuid.uuid5(
                            uuid.NAMESPACE_DNS,
                            f"{self._meta['doc_id']}::{self._meta['doc_version']}"
                            f"::{key}#{index}",
                        )
                    )
            self._emit(
                segment,
                segment_meta,
                chunk_type="parent",
                page_from=min(pages) if pages else None,
                page_to=max(pages) if pages else None,
            )

    # ------------------------------------------------------- ссылки и текст ---

    @staticmethod
    def _extract_refs(text: str):
        """Достаёт перекрёстные ссылки: ``п. 4.2.2``, ``ст. 7.12``, ``прил. 9``.

        Возвращает (refs_raw, ref_keys). ref_keys нормализованы до
        ``clause:4.2.2`` / ``article:7.12`` / ``appendix:9`` — по ним
        ретривер делает 1-hop расширение.
        """
        raw: list[str] = []
        keys: list[str] = []
        for pattern, kind in REF_PATTERNS:
            for match in pattern.finditer(text):
                token = f"{kind}:{match.group(1)}"
                if token in keys:
                    continue
                keys.append(token)
                raw.append(match.group(0).strip())
        return raw[:25], keys[:25]

    def _hard_split(self, text: str, max_tokens: int) -> list[str]:
        """Режет абзац, который сам по себе больше лимита."""
        if self.count_tokens(text) <= max_tokens:
            return [text]

        sentences = re.split(r"(?<=[.!?;:])\s+", text)
        parts: list[str] = []
        for sentence in sentences:
            if self.count_tokens(sentence) <= max_tokens:
                parts.append(sentence)
                continue
            chars_per_token = max(1, len(sentence) // max(1, self.count_tokens(sentence)))
            step = max(1, max_tokens * chars_per_token)
            for i in range(0, len(sentence), step):
                parts.append(sentence[i : i + step])

        chunks: list[str] = []
        buf: list[str] = []
        cur = 0
        for part in parts:
            tokens = self.count_tokens(part)
            if cur + tokens > max_tokens and buf:
                chunks.append(" ".join(buf).strip())
                buf, cur = [], 0
            buf.append(part)
            cur += tokens
        if buf:
            chunks.append(" ".join(buf).strip())
        return [c for c in chunks if c]

    def _chunk_parts(self, parts: list[str], lead: str) -> list[str]:
        """Группирует абзацы пункта в чанки, повторяя вводную фразу.

        Подпункты «а)–е)» без вводной фразы («Стандарт не применяется
        заказчиками…») теряют смысл, поэтому lead повторяется в каждом
        чанке, кроме первого.
        """
        max_tokens = max(80, self.max_child_tokens)
        chunks: list[str] = []
        buf: list[str] = []
        cur = 0
        lead_tokens = self.count_tokens(lead) if lead else 0

        for part in parts:
            tokens = self.count_tokens(part)
            if tokens > max_tokens:
                if buf:
                    chunks.append("\n".join(buf))
                    buf, cur = [], 0
                for sub in self._hard_split(part, max_tokens):
                    chunks.append(sub)
                continue

            if cur + tokens > max_tokens and buf:
                chunks.append("\n".join(buf))
                buf = [lead] if lead else []
                cur = lead_tokens

            buf.append(part)
            cur += tokens

        if buf:
            chunks.append("\n".join(buf))
        return [c.strip() for c in chunks if c and c.strip()]

    # -------------------------------------------------------------- пункты ---

    def _parent_key(self) -> str:
        ctx = self._ctx
        if ctx["article_no"]:
            return f"article:{ctx['article_no']}"
        if ctx["appendix_no"]:
            suffix = f"/{ctx['inner_appendix_no']}" if ctx["inner_appendix_no"] else ""
            return f"appendix:{ctx['appendix_no']}{suffix}"
        if ctx["chapter_no"]:
            return f"chapter:{ctx['chapter_no']}"
        return "doc"

    def _clause_key(self, number: str) -> str:
        ctx = self._ctx
        if ctx["article_no"]:
            return f"{ctx['article_no']}.{number}"
        if ctx["appendix_no"]:
            return f"{ctx['appendix_no']}.{number}"
        return number

    def _parent_id(self, parent_key: str) -> str:
        return str(
            uuid.uuid5(
                uuid.NAMESPACE_DNS,
                f"{self._meta['doc_id']}::{self._meta['doc_version']}::{parent_key}",
            )
        )

    def _start_clause(self, number: str, depth: int, text: str, page) -> None:
        self._flush_clause()
        self._clause = {
            "number": number,
            "depth": depth,
            "key": self._clause_key(number),
            "parent_key": self._parent_key(),
            "parts": [text],
            "page_from": page,
            "page_to": page,
        }

    def _append_to_clause(self, text: str, page) -> None:
        if self._clause is None:
            self._loose.append(text)
            return
        self._clause["parts"].append(text)
        if page:
            self._clause["page_to"] = page

    def _flush_clause(self) -> None:
        clause = self._clause
        if not clause:
            return
        self._clause = None

        parts = [p for p in clause["parts"] if p and p.strip()]
        if not parts:
            return

        meta = self._scope_meta()
        meta["clause_number"] = clause["key"]
        meta["clause_key"] = f"clause:{clause['key']}"
        meta["clause_depth"] = clause["depth"]
        meta["parent_key"] = clause["parent_key"]
        meta["parent_id"] = self._parent_id(clause["parent_key"])
        meta["block_type"] = "text"

        body = "\n".join(parts)
        if self.extract_refs:
            refs_raw, ref_keys = self._extract_refs(body)
            meta["refs_raw"] = refs_raw
            meta["ref_keys"] = ref_keys

        # Вводная фраза пункта повторяется в каждом чанке с подпунктами.
        lead = parts[0][:250].strip() if len(parts) > 1 else ""

        for part in self._chunk_parts(parts, lead):
            self._emit(
                part,
                meta,
                chunk_type="child",
                page_from=clause["page_from"],
                page_to=clause["page_to"],
            )

    def _flush_loose(self) -> None:
        """Текст вне нумерованных пунктов: вводки, примечания к разделу."""
        if not self._loose:
            return
        body = "\n".join(self._loose).strip()
        self._loose = []
        if not body:
            return

        parent_key = self._parent_key()
        meta = self._scope_meta()
        meta["clause_number"] = None
        meta["parent_key"] = parent_key
        meta["parent_id"] = self._parent_id(parent_key)
        meta["block_type"] = "text"
        if self.extract_refs:
            refs_raw, ref_keys = self._extract_refs(body)
            meta["refs_raw"] = refs_raw
            meta["ref_keys"] = ref_keys

        for part in self._chunk_parts([body], ""):
            self._emit(part, meta, chunk_type="child", page_from=None, page_to=None)

    # ------------------------------------------------------------- таблицы ---

    @staticmethod
    def _is_graph_numbering(row: list[str]) -> bool:
        """Строка «1 | 2 | 3 | 4» — нумерация графов, а не данные."""
        values = [v.strip() for v in row if v and v.strip()]
        if len(values) < 2 or not all(v.isdigit() for v in values):
            return False
        return [int(v) for v in values] == list(range(1, len(values) + 1))

    def _table_grid(self, table_item, carry: dict | None = None):
        """Разворачивает ``table_cells`` в сетку, раскрывая row_span/col_span.

        В Приложении 9 ячейка «Оборудование компьютерное…» занимает 12 строк:
        без раскрытия коды ОКПД2 остаются без наименования продукции.
        ``carry`` переносит «№ | Наименование» с предыдущей страницы, когда
        таблица продолжается и объединённая ячейка не повторяется.
        """
        data = table_item.data
        n_rows = int(data.num_rows or 0)
        n_cols = int(data.num_cols or 0)
        if not n_rows or not n_cols:
            return [], []

        grid = [["" for _ in range(n_cols)] for _ in range(n_rows)]
        header_rows: set[int] = set()

        for cell in data.table_cells:
            r0 = max(0, int(cell.start_row_offset_idx))
            c0 = max(0, int(cell.start_col_offset_idx))
            r1 = min(n_rows, max(r0 + 1, int(cell.end_row_offset_idx)))
            c1 = min(n_cols, max(c0 + 1, int(cell.end_col_offset_idx)))
            text = self._norm(cell.text)
            for r in range(r0, r1):
                for c in range(c0, c1):
                    if not grid[r][c]:
                        grid[r][c] = text
            if getattr(cell, "column_header", False):
                header_rows.update(range(r0, r1))

        if not header_rows:
            header_rows = {0}

        header: list[str] = []
        for c in range(n_cols):
            parts: list[str] = []
            for r in sorted(header_rows):
                value = grid[r][c]
                if value and value not in parts:
                    parts.append(value)
            header.append(" / ".join(parts))

        skip = set(header_rows)
        for r in range(n_rows):
            if r not in skip and self._is_graph_numbering(grid[r]):
                skip.add(r)

        rows: list[list[str]] = []
        for r in range(n_rows):
            if r in skip:
                continue
            row = [grid[r][c] for c in range(n_cols)]
            # Перенос применяем только к строкам, где «№» и «Наименование»
            # пусты: это признак продолжения таблицы с прошлой страницы.
            if carry and not row[0] and (len(row) < 2 or not row[1]):
                for column, value in carry.items():
                    if column < len(row) and not row[column] and value:
                        row[column] = value
            while row and not row[-1]:
                row.pop()
            if any(cell for cell in row):
                rows.append(row)

        return header, rows

    def _handle_table(self, table_item, page) -> None:
        self._flush_table()
        self._flush_clause()
        self._flush_loose()

        caption = self._pending_caption or self._last_header
        self._pending_caption = None
        if not caption:
            # Таблица-продолжение на следующей странице: реальная подпись
            # осталась на первой странице, поэтому берём название приложения.
            if self._ctx["appendix_title"]:
                caption = f"{self._ctx['appendix_title']} (таблица, стр. {page})"
            elif self._ctx["appendix_no"]:
                caption = f"Приложение {self._ctx['appendix_no']}, таблица на стр. {page}"
            else:
                caption = f"Таблица на стр. {page}" if page else "Таблица"

        # Перенос «№ | Наименование» действует только внутри одной таблицы:
        # сменился заголовок или приложение — начинаем заново.
        scope = (self._ctx["appendix_no"], self._ctx["article_no"])
        carry = None
        if (
            self._table_carry
            and self._table_carry.get("scope") == scope
            and self._table_carry.get("header") == self._last_header
        ):
            carry = self._table_carry["values"]

        header, rows = self._table_grid(table_item, carry)
        if not rows:
            return

        # «№ | Наименование» берём из последней строки, где заполнены обе
        # ячейки: они относятся к одной объединённой ячейке и меняются вместе.
        values = {}
        for row in reversed(rows):
            if len(row) >= 2 and row[0] and row[1]:
                values = {0: row[0], 1: row[1]}
                break
        if not values:
            for row in reversed(rows):
                if len(row) >= 2:
                    if 0 not in values and row[0]:
                        values[0] = row[0]
                    if 1 not in values and row[1]:
                        values[1] = row[1]
                if len(values) == 2:
                    break
        if values:
            self._table_carry = {
                "scope": scope,
                "header": self._last_header,
                "values": values,
            }

        meta = self._scope_meta()
        meta["block_type"] = "table"
        meta["table_caption"] = caption
        meta["table_columns"] = header
        meta["table_row_count"] = len(rows)
        meta["parent_key"] = f"table:{page}:{self._idx}"

        self._pending_table = {
            "caption": caption,
            "header": header,
            "rows": rows,
            "notes": [],
            "meta": meta,
            "page": page,
        }

    def _handle_footnote(self, text: str, page) -> None:
        """Сноска под таблицей — часть нормы, а не мусор.

        «(1) …если плановая стоимость закупки (лота) не превышает 10 млн
        рублей с НДС»: без неё ответ про способ закупки будет неверным.
        """
        if not self.attach_footnotes:
            return
        if self._pending_table and (
            page is None or self._pending_table["page"] == page
        ):
            self._pending_table["notes"].append(text)
            return
        self._append_to_clause(f"Сноска: {text}", page)

    def _flush_table(self) -> None:
        table = self._pending_table
        if not table:
            return
        self._pending_table = None

        header = table["header"]
        rows = table["rows"]
        notes = table["notes"]
        caption = table["caption"]
        meta = table["meta"]
        page = table["page"]

        head_lines = [caption]
        if any(header):
            head_lines.append("Колонки: " + " | ".join(header))
        head = "\n".join(head_lines)

        note_block = ""
        if notes:
            note_block = "\n\nУсловия применения (сноски):\n" + "\n".join(
                f"— {note}" for note in notes
            )

        # Ссылки в таблицах: в Приложении 10/12 ячейки «Содержание» полны
        # ссылок («ст. 7.12.3», «п. 2.1»). Без них 1-hop по таблицам не
        # работал бы вовсе.
        if self.extract_refs:
            table_text = head + "\n" + "\n".join(
                " | ".join(cell for cell in row) for row in rows
            ) + note_block
            refs_raw, ref_keys = self._extract_refs(table_text)
            if ref_keys:
                meta["refs_raw"] = refs_raw
                meta["ref_keys"] = ref_keys

        # Глоссарий «термин | определение»: одна строка = один чанк.
        if len(header) == 2 and re.search(r"термин|определен", caption, re.IGNORECASE):
            for index, row in enumerate(rows):
                term = row[0] if row else ""
                definition = row[1] if len(row) > 1 else ""
                if not term and not definition:
                    continue
                body = f"{caption}\nТермин: {term}\nОпределение: {definition}"
                chunk_meta = dict(meta)
                chunk_meta["block_type"] = "glossary"
                chunk_meta["row_index"] = index
                self._emit(body, chunk_meta, "child", page, page)
            return

        def render(rows_slice: list[list[str]]) -> str:
            lines = [head]
            lines.extend(" | ".join(cell for cell in row) for row in rows_slice)
            return "\n".join(lines) + note_block

        full = render(rows)
        if (
            self.count_tokens(full) <= self.max_child_tokens
            or not self.emit_tables_as_rows
        ):
            chunk_meta = dict(meta)
            chunk_meta["part"] = "full"
            chunk_meta["row_range"] = f"0-{len(rows) - 1}"
            self._emit(full, chunk_meta, "child", page, page)
            return

        # Сноски повторяются в каждом чанке намеренно — в них условие
        # применения нормы. Поэтому в бюджет они не входят: иначе таблица
        # из 10 строк рассыпалась бы на 19 чанков по одной строке.
        budget = max(60, self.max_child_tokens - self.count_tokens(head) - 10)

        buf: list[list[str]] = []
        cur = 0
        start = 0
        for index, row in enumerate(rows):
            line = " | ".join(row)
            tokens = self.count_tokens(line)

            if tokens > budget:
                # Строка «портянка» (Приложение 12: один пункт = страница
                # текста). Сначала сбрасываем накопленное, потом режем строку,
                # возвращая в каждый кусок «№ | Раздел»: иначе «− для
                # заказчиков первой группы…» повисает без привязки к пункту.
                if buf:
                    chunk_meta = dict(meta)
                    chunk_meta["part"] = f"part_{start}"
                    chunk_meta["row_range"] = f"{start}-{index - 1}"
                    self._emit(render(buf), chunk_meta, "child", page, page)
                    buf, cur = [], 0

                identity = " | ".join(cell for cell in row[:2] if cell).strip()[:160]
                prefix = f"{identity}\n" if identity else ""
                for part_index, sub in enumerate(
                    self._hard_split(
                        head + "\n" + line + note_block, self.max_child_tokens
                    )
                ):
                    chunk_meta = dict(meta)
                    chunk_meta["part"] = f"part_{index}_{part_index}"
                    chunk_meta["row_range"] = f"{index}-{index}"
                    if part_index and prefix:
                        sub = prefix + sub
                    self._emit(sub, chunk_meta, "child", page, page)
                start = index + 1
                continue

            if cur + tokens > budget and buf:
                chunk_meta = dict(meta)
                chunk_meta["part"] = f"part_{start}"
                chunk_meta["row_range"] = f"{start}-{index - 1}"
                self._emit(render(buf), chunk_meta, "child", page, page)
                buf, cur = [], 0
                start = index
            buf.append(row)
            cur += tokens

        if buf:
            chunk_meta = dict(meta)
            chunk_meta["part"] = f"part_{start}"
            chunk_meta["row_range"] = f"{start}-{len(rows) - 1}"
            self._emit(render(buf), chunk_meta, "child", page, page)

    # ------------------------------------------------------------- разбор ---

    def _handle_item(self, item, level: int) -> None:
        label = getattr(item, "label", None)
        label = getattr(label, "value", label)

        prov = None
        if getattr(item, "prov", None):
            prov = item.prov[0]
        page = getattr(prov, "page_no", None)

        if label == "table":
            self._handle_table(item, page)
            return
        if label in LABEL_PAGE_FURNITURE:
            return

        raw = getattr(item, "orig", None) or getattr(item, "text", "") or ""
        text = self._norm(raw)
        if not text:
            return

        if label == "footnote":
            self._handle_footnote(text, page)
            return
        if label == "caption":
            self._pending_caption = text
            return
        if RE_PAGE_NUMBER.match(text):
            return

        # --- колонтитулы приложений -------------------------------------
        match = RE_APPENDIX_STD.match(text)
        if match:
            number = match.group(1)
            if number != self._ctx["appendix_no"]:
                self._reset_section(appendix_no=number)
            return

        match = RE_APPENDIX_INNER.match(text)
        if match:
            number = match.group(1)
            if number != self._ctx["inner_appendix_no"]:
                self._flush_clause()
                self._flush_loose()
                self._ctx["inner_appendix_no"] = number
                self._ctx["razdel"] = None
                self._ctx["subsection"] = None
            return

        match = RE_APPENDIX_BARE.match(text)
        if match:
            number = match.group(1)
            if number != self._ctx["appendix_no"]:
                self._reset_section(appendix_no=number)
            return

        # --- главы, статьи, разделы -------------------------------------
        match = RE_CHAPTER.match(text)
        if match:
            self._reset_section(
                chapter=text, chapter_no=match.group(1), keep_appendix=True
            )
            return

        match = RE_ARTICLE.match(text)
        if match:
            self._flush_table()
            self._flush_clause()
            self._flush_loose()
            self._ctx.update(
                {
                    "article": text,
                    "article_no": match.group(1),
                    "razdel": None,
                    "subsection": None,
                }
            )
            self._last_header = text
            return

        match = RE_RAZDEL.match(text)
        if match:
            self._flush_table()
            self._flush_clause()
            self._flush_loose()
            self._ctx["razdel"] = text[:160]
            self._ctx["subsection"] = None
            self._last_header = text
            return

        # --- заголовки ---------------------------------------------------
        if label in ("section_header", "title"):
            self._flush_table()
            self._flush_clause()
            self._flush_loose()
            self._last_header = text
            if self._ctx["appendix_no"] and not self._ctx["appendix_title"]:
                self._ctx["appendix_title"] = text[:200]
            elif label == "section_header":
                self._ctx["subsection"] = text[:200]
            return

        # --- нумерованные пункты -----------------------------------------
        match = RE_NUM_ITEM.match(text)
        if match and self._looks_like_clause(text, label):
            self._flush_loose()
            number = match.group(1)
            self._start_clause(number, number.count(".") + 1, text, page)
            return

        # --- подпункты и продолжение -------------------------------------
        if RE_LET_ITEM.match(text) or RE_DASH_ITEM.match(text):
            self._append_to_clause(text, page)
            return

        self._append_to_clause(text, page)

    @staticmethod
    def _looks_like_clause(text: str, label) -> bool:
        """Отличает «10 млн руб.» от пункта «10. Заказчики вправе…».

        Docling помечает настоящие пункты как ``list_item``; для ``text``
        требуем заглавную букву после номера, иначе строка «10 млн рублей»
        создала бы фиктивный пункт 10.
        """
        match = RE_NUM_ITEM.match(text)
        if not match:
            return False
        if label == "list_item":
            return True
        rest = text[match.end() :]
        return bool(rest) and (rest[0].isupper() or rest[0] in "«\"(")

    def _reset_section(self, **kwargs) -> None:
        """Сбрасывает контекст при переходе в новую главу или приложение."""
        self._flush_table()
        self._flush_clause()
        self._flush_loose()

        keep_appendix = kwargs.pop("keep_appendix", False)
        if not keep_appendix:
            self._ctx.update(
                {
                    "appendix_no": None,
                    "appendix_title": None,
                    "inner_appendix_no": None,
                }
            )
        self._ctx.update(
            {
                "chapter": None,
                "chapter_no": None,
                "article": None,
                "article_no": None,
                "razdel": None,
                "subsection": None,
            }
        )
        self._ctx.update({k: v for k, v in kwargs.items() if v is not None})
        self._last_header = None
        self._table_carry = None

    # -------------------------------------------------------------- запуск ---

    def _detect_version(self, doc, fallback: str) -> str:
        """Берёт самую позднюю дату изменения с титульного листа.

        На титуле перечислены все редакции 2012–2020; действующей является
        последняя, поэтому максимум по (год, месяц, день).
        """
        try:
            texts = [
                item.text
                for item in doc.texts[:600]
                if item.prov and item.prov[0].page_no <= 2
            ]
        except Exception:
            texts = []

        best = None
        for day, month, year in RE_DATE.findall(" ".join(texts)):
            month_no = MONTHS.get(month.lower())
            if not month_no:
                continue
            key = (int(year), month_no, int(day))
            if best is None or key > best:
                best = key

        if best:
            return f"{best[0]:04d}-{best[1]:02d}-{best[2]:02d}"
        return fallback

    def _flush_all_parents(self) -> None:
        active = getattr(self, "_active_parent", None)
        if active:
            self._flush_parent(active)
            self._active_parent = None
        for key in list(self._parents):
            self._flush_parent(key)

    def _parse_single(self, path: str) -> list[Data]:
        filename = os.path.basename(path)

        digest = hashlib.sha1()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        doc_hash = digest.hexdigest()[:16]

        converter = self._get_converter()
        start = int(self.page_from or 0)
        end = int(self.page_to or 0)

        kwargs = {}
        if start and end and end >= start:
            kwargs["page_range"] = (start, end)
        elif start:
            kwargs["page_range"] = (start, 2_147_483_647)

        self.log(f"Docling: разбираю {filename} (page_range={kwargs.get('page_range')})")

        page_range = kwargs.get("page_range")
        planned_total = (
            page_range[1] - page_range[0] + 1
            if page_range and page_range[1] < 2_000_000
            else None
        )
        self._send_progress(
            "parsing", done=0, total=planned_total, message="старт разбора", force=True
        )

        progress_callback = self._make_progress_callback()
        if progress_callback is not None:
            kwargs["progress_callback"] = progress_callback

        try:
            result = converter.convert(path, **kwargs)
        except TypeError:
            # Старая версия Docling без progress_callback — разбор без прогресса
            # важнее, чем отсутствие прогресса.
            kwargs.pop("progress_callback", None)
            result = converter.convert(path, **kwargs)

        doc = result.document

        self._send_progress(
            "chunking", message="разбор структуры и нарезка", force=True
        )

        version = (self.doc_version or "").strip() or self._detect_version(doc, doc_hash)

        meta = {
            "doc_id": (self.doc_id or "document").strip(),
            "doc_title": (self.doc_title or filename).strip(),
            "doc_short": (self.doc_short or self.doc_id or "Документ").strip(),
            "doc_version": version,
            "doc_hash": doc_hash,
            "filename": filename,
            "file_path": path,
            "page_range": f"{start}-{end}" if start else "",
        }
        self._reset_state(meta)

        for item, level in doc.iterate_items():
            self._handle_item(item, level)

        self._flush_table()
        self._flush_clause()
        self._flush_loose()
        self._flush_all_parents()

        self.log(
            f"{filename}: {len(self._chunks)} чанков, редакция {version}"
        )
        self._send_progress(
            "done",
            done=len(self._chunks),
            total=len(self._chunks),
            message=f"готово: {len(self._chunks)} чанков",
            force=True,
        )
        return self._chunks

    def _resolve_paths(self, input_data) -> list[str]:
        """Достаёт пути из Message / DataFrame / Data, как это делает Read File."""
        paths: list[str] = []

        try:
            import pandas as pd

            if isinstance(input_data, pd.DataFrame):
                column = next(
                    (
                        name
                        for name in ("file_path", "files", "path")
                        if name in input_data.columns
                    ),
                    None,
                )
                if column:
                    for value in input_data[column].tolist():
                        candidate = str(value).strip().strip('"').strip("'")
                        if candidate and candidate.lower() != "nan":
                            paths.append(candidate)
                return paths
        except Exception:
            pass

        if isinstance(input_data, (list, tuple)):
            for element in input_data:
                paths.extend(self._resolve_paths(element))
            return paths

        if input_data is None:
            return paths

        payload = getattr(input_data, "data", None)
        if isinstance(payload, dict):
            for key in ("file_path", "path", "files"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    paths.append(value.strip().strip('"').strip("'"))
                    return paths

        candidate = getattr(input_data, "file_path", None) or getattr(
            input_data, "text", None
        )
        if candidate:
            paths.append(str(candidate).strip().strip('"').strip("'"))
        return paths

    def parse(self) -> list[Data]:
        paths = [p for p in self._resolve_paths(self.file_input) if p]

        if not paths:
            self.log("Не найден путь к файлу: подключите выход Read File.")
            return []

        chunks: list[Data] = []
        for path in paths:
            if not os.path.exists(path):
                self.log(f"Файл не найден: {path}")
                continue
            try:
                chunks.extend(self._parse_single(path))
            except Exception as exc:  # noqa: BLE001 - показываем причину в UI
                self.log(f"Ошибка разбора {os.path.basename(path)}: {exc}")

        return chunks









