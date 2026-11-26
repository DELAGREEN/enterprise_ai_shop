"""Гибридный ретривер для структурных чанков НПА (вариант B+C+E).

Что делает поверх обычного dense+BM25 поиска:

* **Точный lookup по номеру.** «Что устанавливает п. 4.2.2?» решается не
  вектором, а фильтром по ``clause_key``: номер пункта — это точный ключ, и
  векторный поиск на таких запросах регулярно промахивается.
* **Фильтры области.** ``doc_id``, ``doc_version``, ``appendix_no``,
  ``article_no`` — чтобы вопрос про Приложение 9 не поднимал пункты Глав.
* **expand-to-parent.** Ищется child (250–400 токенов, высокая точность), а в
  LLM уходит parent (статья целиком), потому что условие или исключение часто
  лежит абзацем выше найденного пункта.
* **1-hop по перекрёстным ссылкам.** Нашли п. 7.12.3, который ссылается на
  ст. 4.2.2 — подтягиваем и её, с маркировкой ``via_ref``. Область разрешения
  ссылки настраивается: внутри приложения (точнее) или по всему документу.
* **Цитата.** Каждый результат несёт «Приложение 12, п. 2.1, стр. 366», чтобы
  ответ можно было проверить, а не принимать на веру.

Компонент ожидает payload, который пишет ``EOSZ Structure Parser (B+C)``:
``chunk_type``, ``clause_key``, ``clause_number``, ``parent_id``, ``ref_keys``,
``section_path``, ``page_from``/``page_to``.
"""

from __future__ import annotations

import re

from lfx.custom.custom_component.component import Component
from lfx.io import (
    BoolInput,
    DropdownInput,
    FloatInput,
    HandleInput,
    IntInput,
    MessageInput,
    Output,
    StrInput,
)
from langflow.schema import Data

# Ссылки в запросе пользователя: «п. 4.2.2», «пункт 4.2.2», «ст. 7.12».
RE_QUERY_CLAUSE = re.compile(
    r"(?:^|[\s,;(])пп?\.?\s*(\d+(?:\.\d+)*)", re.IGNORECASE
)
RE_QUERY_CLAUSE_WORD = re.compile(
    r"пункт[а-яё]*\s+(\d+(?:\.\d+)*)", re.IGNORECASE
)
RE_QUERY_ARTICLE = re.compile(
    r"стать[яиеюёй]+\s+(\d+(?:\.\d+)*)", re.IGNORECASE
)
RE_QUERY_APPENDIX = re.compile(
    r"приложени[а-яё]*\s+(?:№\s*)?(\d+)", re.IGNORECASE
)
RE_QUERY_CHAPTER = re.compile(r"глав[аеуы]\s+(\d+)", re.IGNORECASE)

REF_SCOPE_APPENDIX = "appendix"
REF_SCOPE_DOCUMENT = "document"


class EoszRetriever(Component):
    display_name = "EOSZ Hybrid Retriever (Dense + BM25 + refs)"
    description = (
        "Гибридный поиск по структурным чанкам НПА: точный lookup по номеру "
        "пункта, expand-to-parent и 1-hop по перекрёстным ссылкам."
    )
    icon = "search"

    _sparse_encoder = None

    inputs = [
        MessageInput(name="query", display_name="Query"),
        HandleInput(
            name="embedding_model",
            display_name="Embedding Model",
            input_types=["Embeddings"],
            info="Выход Embeddings компонента EmbeddingModelComponent.",
        ),
        StrInput(
            name="qdrant_url",
            display_name="Qdrant URL",
            value="http://qdrant:6333",
        ),
        StrInput(
            name="collection_name",
            display_name="Collection",
            value="gost_chunks",
        ),
        StrInput(
            name="dense_vector_name",
            display_name="Dense vector name",
            value="text-dense",
        ),
        StrInput(
            name="sparse_vector_name",
            display_name="Sparse vector name",
            value="text-sparse",
        ),
        StrInput(
            name="doc_id",
            display_name="Document ID filter",
            value="eosz",
            info=(
                "Ограничить поиск одним документом. Пусто — искать по всей "
                "коллекции (полезно, когда документов несколько)."
            ),
        ),
        StrInput(
            name="doc_version",
            display_name="Document version filter",
            value="",
            info=(
                "Пусто — актуальная редакция выбирается автоматически "
                "(самая поздняя в коллекции для этого doc_id). Иначе — "
                "конкретная, например 2020-07-17."
            ),
        ),
        IntInput(
            name="top_k",
            display_name="Top K",
            value=8,
            info="Сколько child-чанков вернуть после слияния и дедупликации.",
        ),
        IntInput(
            name="prefetch_limit",
            display_name="Prefetch limit",
            value=30,
            info=(
                "Сколько кандидатов берёт каждый из двух поисков (dense и "
                "BM25) до слияния через RRF."
            ),
        ),
        IntInput(
            name="expand_parents",
            display_name="Expand to parent (count)",
            value=4,
            info=(
                "Сколько результатов заменить полным parent-чанком (статьёй). "
                "0 — выключить. Parent крупнее, но в нём есть условие, "
                "которое осталось абзацем выше найденного пункта."
            ),
        ),
        BoolInput(
            name="use_exact_lookup",
            display_name="Exact clause lookup",
            value=True,
            info=(
                "Если в вопросе есть номер пункта или статьи — сначала "
                "точный поиск по clause_key/article_no, потом вектор."
            ),
        ),
        BoolInput(
            name="use_ref_expansion",
            display_name="1-hop by references",
            value=True,
            info=(
                "Подтягивать пункты, на которые ссылаются найденные чанки "
                "(п. 7.12.3 -> ст. 4.2.2)."
            ),
        ),
        DropdownInput(
            name="ref_scope",
            display_name="Reference scope",
            value=REF_SCOPE_APPENDIX,
            options=[REF_SCOPE_APPENDIX, REF_SCOPE_DOCUMENT],
            info=(
                "appendix — ссылка разрешается внутри текущего приложения "
                "(точнее: «п. 2.1» в Приложении 12 — это п. 12.2.1).\n"
                "document — если в приложении не нашлось, ищем по всему "
                "документу (покрывает ссылки из приложений на Стандарт)."
            ),
        ),
        IntInput(
            name="max_ref_hops",
            display_name="Max reference expansions",
            value=3,
            info="Сколько дополнительных чанков добавлять по ссылкам за запрос.",
        ),
        FloatInput(
            name="min_score",
            display_name="Min RRF score",
            value=0.0,
            info=(
                "Порог по score слияния. 0 — не отсекать. RRF-оценки "
                "сравнимы только внутри одного запроса, поэтому порог "
                "подбирается под корпус, а не «на глаз»."
            ),
        ),
        BoolInput(
            name="include_citation",
            display_name="Add citation to text",
            value=True,
            info=(
                "Начинать текст каждого результата строкой-цитатой: "
                "документ, приложение, пункт, страница."
            ),
        ),
    ]

    outputs = [
        Output(name="documents", display_name="Retrieved Chunks", method="retrieve"),
        Output(name="context", display_name="Prompt Context", method="context"),
    ]

    # ------------------------------------------------------------- helpers ---

    @classmethod
    def _get_sparse_encoder(cls):
        """BM25-энкодер кэшируем на классе: инициализация ~1 с.

        Модель должна быть та же, что при ингесте, иначе sparse-векторы
        запроса и документа окажутся в разных пространствах.
        """
        if cls._sparse_encoder is None:
            from fastembed import SparseTextEmbedding

            cls._sparse_encoder = SparseTextEmbedding(model_name="Qdrant/bm25")
        return cls._sparse_encoder

    def _client(self):
        from qdrant_client import QdrantClient

        return QdrantClient(url=self.qdrant_url, timeout=120)

    def _embed_query(self, text: str) -> list[float]:
        model = self.embedding_model
        if hasattr(model, "embed_query"):
            return model.embed_query(text)
        if hasattr(model, "embed_documents"):
            return model.embed_documents([text])[0]
        raise ValueError(
            "Подключённая модель не умеет embed_query/embed_documents — "
            "используйте компонент Embeddings (EmbeddingModelComponent)."
        )

    def _parse_query(self, text: str) -> dict:
        """Достаёт из вопроса номера пунктов, статей, приложений, глав."""
        clauses: list[str] = []
        for pattern in (RE_QUERY_CLAUSE, RE_QUERY_CLAUSE_WORD):
            for match in pattern.finditer(text):
                value = match.group(1).rstrip(".")
                if value and value not in clauses:
                    clauses.append(value)

        articles = []
        for match in RE_QUERY_ARTICLE.finditer(text):
            value = match.group(1).rstrip(".")
            if value and value not in articles:
                articles.append(value)

        appendixes = []
        for match in RE_QUERY_APPENDIX.finditer(text):
            value = match.group(1)
            if value and value not in appendixes:
                appendixes.append(value)

        chapters = []
        for match in RE_QUERY_CHAPTER.finditer(text):
            value = match.group(1)
            if value and value not in chapters:
                chapters.append(value)

        return {
            "clauses": clauses,
            "articles": articles,
            "appendixes": appendixes,
            "chapters": chapters,
        }

    def _resolve_version(self, client) -> str:
        """Определяет редакцию: явную из настроек или самую позднюю в базе.

        Без этого при двух редакциях в коллекции ответы смешивались бы:
        старый и новый пункт с одним номером оба попадали бы в выдачу.

        Используется ``facet``, а не ``scroll``: на больших коллекциях
        scroll читает payload с диска десятками секунд, facet отвечает
        мгновенно.
        """
        explicit = (self.doc_version or "").strip()
        if explicit:
            return explicit

        from qdrant_client import models

        doc_id = (self.doc_id or "").strip()
        point_filter = None
        if doc_id:
            point_filter = models.Filter(
                must=[
                    models.FieldCondition(
                        key="doc_id", match=models.MatchValue(value=doc_id)
                    )
                ]
            )

        versions: set[str] = set()
        try:
            response = client.facet(
                collection_name=self.collection_name,
                key="doc_version",
                limit=50,
                filter=point_filter,
            )
            versions = {str(hit.value) for hit in response.hits if hit.value}
        except TypeError:
            # Старые версии клиента не знают аргумент filter.
            try:
                response = client.facet(
                    collection_name=self.collection_name, key="doc_version", limit=50
                )
                versions = {str(hit.value) for hit in response.hits if hit.value}
            except Exception:
                versions = set()
        except Exception:
            versions = set()

        if not versions:
            return ""
        return max(versions)

    def _base_must(self, version: str) -> list:
        """Общие условия: документ, редакция, только child-чанки."""
        from qdrant_client import models

        must = [
            models.FieldCondition(
                key="chunk_type", match=models.MatchValue(value="child")
            )
        ]
        if (self.doc_id or "").strip():
            must.append(
                models.FieldCondition(
                    key="doc_id", match=models.MatchValue(value=self.doc_id.strip())
                )
            )
        if version:
            must.append(
                models.FieldCondition(
                    key="doc_version", match=models.MatchValue(value=version)
                )
            )
        return must

    @staticmethod
    def _payload_of(point) -> dict:
        return point.payload or {}

    def _to_hit(self, point, source: str) -> dict:
        payload = self._payload_of(point)
        return {
            "point_id": str(point.id),
            "score": float(getattr(point, "score", 0.0) or 0.0),
            "source": source,
            "payload": payload,
        }

    def _filtered_points(
        self, client, must: list, limit: int, dense: list[float], source: str = "exact"
    ) -> list[dict]:
        """Точки по жёсткому фильтру, отсортированные по близости к запросу.

        Вместо ``scroll`` используется ``query_points`` с вектором запроса:
        фильтр всё равно жёсткий, а HNSW-поиск читает данные из памяти.
        ``scroll`` на этой же коллекции занимал десятки секунд, потому что
        тянет payload с диска.
        """
        from qdrant_client import models

        response = client.query_points(
            collection_name=self.collection_name,
            query=dense,
            using=self.dense_vector_name,
            query_filter=models.Filter(must=must),
            limit=max(1, limit),
            with_payload=True,
        )
        return [self._to_hit(point, source) for point in response.points]

    def _exact_lookup(
        self, client, parsed: dict, version: str, dense: list[float]
    ) -> list[dict]:
        """Точный поиск по номеру пункта, статьи, приложения или главы.

        Для «п. 4.2.2» кандидаты ключа: ``clause:4.2.2`` (пункт статьи 4.2)
        и ``clause:4.2.2`` из приложения. Порядок важен: сначала пробуем
        самый вероятный вариант, а не отдаём всё подряд.
        """
        from qdrant_client import models

        base = self._base_must(version)
        hits: list[dict] = []
        seen: set[str] = set()

        def add(new_hits: list[dict]) -> None:
            for hit in new_hits:
                key = hit["point_id"]
                if key in seen:
                    continue
                seen.add(key)
                hits.append(hit)

        # 1. Номера пунктов.
        keys: list[str] = []
        for clause in parsed["clauses"]:
            keys.append(f"clause:{clause}")
            for appendix in parsed["appendixes"]:
                keys.append(f"clause:{appendix}.{clause}")
            for article in parsed["articles"]:
                if not clause.startswith(f"{article}."):
                    keys.append(f"clause:{article}.{clause}")

        if keys:
            add(
                self._filtered_points(
                    client,
                    base
                    + [
                        models.FieldCondition(
                            key="clause_key", match=models.MatchAny(any=keys)
                        )
                    ],
                    limit=max(3, self.top_k),
                    dense=dense,
                )
            )

        # 2. Статьи целиком (когда спросили «что говорит статья 7.12»).
        if parsed["articles"]:
            add(
                self._filtered_points(
                    client,
                    base
                    + [
                        models.FieldCondition(
                            key="article_no",
                            match=models.MatchAny(any=parsed["articles"]),
                        )
                    ],
                    limit=max(3, self.top_k),
                    dense=dense,
                )
            )

        # 3. Приложение без номера пункта — берём его начало.
        if parsed["appendixes"] and not parsed["clauses"]:
            add(
                self._filtered_points(
                    client,
                    base
                    + [
                        models.FieldCondition(
                            key="appendix_no",
                            match=models.MatchAny(any=parsed["appendixes"]),
                        )
                    ],
                    limit=max(2, self.top_k // 2),
                    dense=dense,
                )
            )

        # 4. Глава.
        if parsed["chapters"]:
            add(
                self._filtered_points(
                    client,
                    base
                    + [
                        models.FieldCondition(
                            key="chapter_no",
                            match=models.MatchAny(any=parsed["chapters"]),
                        )
                    ],
                    limit=max(2, self.top_k // 2),
                    dense=dense,
                )
            )

        for hit in hits:
            hit["source"] = "exact"
        return hits

    def _hybrid_search(
        self, client, text: str, version: str, dense: list[float]
    ) -> list[dict]:
        """dense + BM25, слияние через RRF на стороне Qdrant."""
        from qdrant_client import models

        sparse_emb = list(self._get_sparse_encoder().embed([text]))[0]
        sparse = models.SparseVector(
            indices=sparse_emb.indices.tolist(),
            values=sparse_emb.values.tolist(),
        )

        response = client.query_points(
            collection_name=self.collection_name,
            prefetch=[
                models.Prefetch(
                    query=dense,
                    using=self.dense_vector_name,
                    limit=self.prefetch_limit,
                    filter=models.Filter(must=self._base_must(version)),
                ),
                models.Prefetch(
                    query=sparse,
                    using=self.sparse_vector_name,
                    limit=self.prefetch_limit,
                    filter=models.Filter(must=self._base_must(version)),
                ),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            limit=max(self.top_k, self.prefetch_limit),
            with_payload=True,
        )
        return [self._to_hit(point, "hybrid") for point in response.points]

    def _fetch_parents(
        self, client, hits: list[dict], version: str, dense: list[float]
    ) -> dict[str, dict]:
        """Достаёт parent-чанки для первых ``expand_parents`` результатов."""
        from qdrant_client import models

        if self.expand_parents <= 0:
            return {}

        parent_ids: list[str] = []
        for hit in hits[: self.expand_parents]:
            value = hit["payload"].get("parent_id")
            if value and value not in parent_ids:
                parent_ids.append(value)
        if not parent_ids:
            return {}

        must = [
            models.FieldCondition(
                key="chunk_type", match=models.MatchValue(value="parent")
            ),
            models.FieldCondition(
                key="parent_id", match=models.MatchAny(any=parent_ids)
            ),
        ]
        if (self.doc_id or "").strip():
            must.append(
                models.FieldCondition(
                    key="doc_id", match=models.MatchValue(value=self.doc_id.strip())
                )
            )
        if version:
            must.append(
                models.FieldCondition(
                    key="doc_version", match=models.MatchValue(value=version)
                )
            )

        found = self._filtered_points(
            client, must, limit=len(parent_ids) * 2, dense=dense, source="parent"
        )

        result: dict[str, dict] = {}
        for hit in found:
            payload = hit["payload"]
            key = payload.get("parent_id")
            if key and key not in result:
                result[key] = payload
        return result

    @staticmethod
    def _field_for_key(target: str) -> tuple[str, str]:
        """Какое поле payload соответствует ключу ссылки.

        ``clause:7.12.3`` живёт в ``clause_key``, ``article:4.2`` — в
        ``article_no``, ``appendix:9`` — в ``appendix_no``. Искать всё по
        ``clause_key`` бессмысленно: таких значений там нет.
        """
        kind, _, value = target.partition(":")
        field = {
            "clause": "clause_key",
            "article": "article_no",
            "appendix": "appendix_no",
            "chapter": "chapter_no",
            "subsection": "razdel",
        }.get(kind)
        if not field:
            return "", ""
        if kind == "clause":
            return field, target
        return field, value

    def _expand_refs(
        self, client, hits: list[dict], version: str, dense: list[float]
    ) -> list[dict]:
        """1-hop по ссылкам найденных чанков.

        «п. 2.1» внутри Приложения 12 — это п. 12.2.1, а «ст. 7.12» — всегда
        Стандарт. Поэтому при ``ref_scope=appendix`` ссылка сначала
        разрешается в текущем приложении, и только потом (если там пусто) —
        по всему документу.
        """
        from qdrant_client import models

        if not self.use_ref_expansion or self.max_ref_hops <= 0:
            return []

        wanted: list[tuple[str, str]] = []
        for hit in hits[: self.top_k]:
            payload = hit["payload"]
            appendix = payload.get("appendix_no")
            for key in payload.get("ref_keys") or []:
                if not isinstance(key, str) or ":" not in key:
                    continue
                kind, value = key.split(":", 1)
                if kind == "clause" and appendix:
                    wanted.append((f"clause:{appendix}.{value}", str(appendix)))
                wanted.append((key, str(appendix) if appendix else ""))

        seen_keys: set[str] = set()
        for hit in hits:
            payload = hit["payload"]
            if payload.get("clause_key"):
                seen_keys.add(str(payload["clause_key"]))
            if payload.get("article_no"):
                seen_keys.add(f"article:{payload['article_no']}")
            if payload.get("appendix_no"):
                seen_keys.add(f"appendix:{payload['appendix_no']}")
            if payload.get("chapter_no"):
                seen_keys.add(f"chapter:{payload['chapter_no']}")

        collected: list[dict] = []
        base = self._base_must(version)
        used = 0

        for target, appendix in wanted:
            if used >= self.max_ref_hops:
                break
            if target in seen_keys:
                continue

            field, value = self._field_for_key(target)
            if not field or not value:
                continue

            must = base + [
                models.FieldCondition(
                    key=field, match=models.MatchValue(value=value)
                )
            ]
            if self.ref_scope == REF_SCOPE_APPENDIX and appendix:
                must.append(
                    models.FieldCondition(
                        key="appendix_no", match=models.MatchValue(value=appendix)
                    )
                )

            found = self._filtered_points(client, must, limit=2, dense=dense)

            if not found and self.ref_scope == REF_SCOPE_APPENDIX:
                # Ссылка из приложения на Стандарт: разрешаем по документу.
                found = self._filtered_points(
                    client,
                    base
                    + [
                        models.FieldCondition(
                            key=field, match=models.MatchValue(value=value)
                        )
                    ],
                    limit=2,
                    dense=dense,
                )

            for hit in found:
                hit["source"] = "ref"
                hit["via_ref"] = target
                collected.append(hit)
                seen_keys.add(target)
                used += 1
                break

        return collected

    # ------------------------------------------------------------- выдача ---

    @staticmethod
    def _short_clause(payload: dict) -> str:
        """Номер пункта в читаемом виде: 12.2.1 -> 2.1 для Приложения 12."""
        value = payload.get("clause_number")
        if not value:
            return ""
        appendix = payload.get("appendix_no")
        if appendix:
            prefix = f"{appendix}."
            if str(value).startswith(prefix):
                return str(value)[len(prefix) :]
        return str(value)

    def _citation(self, payload: dict) -> str:
        bits: list[str] = []
        title = payload.get("doc_title") or payload.get("doc_id") or "Документ"
        version = payload.get("doc_version")
        bits.append(f"{title} (ред. {version})" if version else str(title))

        path = payload.get("section_path") or []
        if isinstance(path, list) and path:
            bits.append(str(path[-1]))

        clause = self._short_clause(payload)
        if clause:
            bits.append(f"п. {clause}")

        page = payload.get("page_from")
        page_to = payload.get("page_to")
        if page:
            if page_to and page_to != page:
                bits.append(f"стр. {page}–{page_to}")
            else:
                bits.append(f"стр. {page}")

        # Части одной таблицы иначе выглядят в выдаче одинаково: «стр. 32»
        # четыре раза подряд. Указываем фрагмент, чтобы цитаты различались.
        part = payload.get("part")
        if isinstance(part, str) and part.startswith("part_"):
            bits.append(f"фрагмент {part[5:]}")

        return ", ".join(bits)

    @staticmethod
    def _dedupe_key(hit: dict) -> str:
        payload = hit["payload"]
        clause = payload.get("clause_key")
        if clause:
            return f"clause::{clause}"
        caption = payload.get("table_caption")
        if caption:
            return f"table::{caption}::{payload.get('row_range')}"
        text = payload.get("text") or ""
        return "text::" + text[:120]

    def _assemble(
        self, client, hits: list[dict], version: str, dense: list[float]
    ) -> list[Data]:
        """Собирает выдачу: parent вместо child, цитаты, дедупликация."""
        parents = self._fetch_parents(client, hits, version, dense)

        results: list[Data] = []
        seen: set[str] = set()
        seen_parents: set[str] = set()

        for hit in hits:
            if len(results) >= self.top_k:
                break

            payload = hit["payload"]
            key = self._dedupe_key(hit)
            if key in seen:
                continue
            seen.add(key)

            text = payload.get("text") or ""
            if not text:
                continue

            parent_id = payload.get("parent_id")
            expanded = False
            if parent_id and parent_id in parents and parent_id not in seen_parents:
                parent_payload = parents[parent_id]
                parent_text = parent_payload.get("text") or ""
                if parent_text and len(parent_text) > len(text):
                    text = parent_text
                    for field in ("page_from", "page_to", "section_path", "breadcrumb"):
                        if parent_payload.get(field):
                            payload = {**payload, field: parent_payload[field]}
                    expanded = True
                    seen_parents.add(parent_id)

            citation = self._citation(payload)

            # Текст чанка уже начинается с breadcrumb: его пишет парсер в
            # ``_emit`` (``f"{head}\n\n{body}"``). Если добавить citation
            # сверху, заголовок задвоится — это видно в контексте и зря
            # съедает токены промпта. Добавляем только когда его нет.
            head = (payload.get("breadcrumb") or "").strip()
            already_headed = bool(head) and text.lstrip().startswith(head)
            if self.include_citation and not already_headed:
                body = f"{citation}\n{text}"
            else:
                body = text

            results.append(
                Data(
                    text=body,
                    data={
                        "score": hit["score"],
                        "source": hit["source"],
                        "via_ref": hit.get("via_ref"),
                        "expanded_to_parent": expanded,
                        "citation": citation,
                        "clause_number": payload.get("clause_number"),
                        "clause_key": payload.get("clause_key"),
                        "article_no": payload.get("article_no"),
                        "appendix_no": payload.get("appendix_no"),
                        "section_path": payload.get("section_path"),
                        "page_from": payload.get("page_from"),
                        "page_to": payload.get("page_to"),
                        "block_type": payload.get("block_type"),
                        "table_caption": payload.get("table_caption"),
                        "part": payload.get("part"),
                        "row_range": payload.get("row_range"),
                        "doc_id": payload.get("doc_id"),
                        "doc_version": payload.get("doc_version"),
                        "ref_keys": payload.get("ref_keys"),
                    },
                )
            )

        return results

    # -------------------------------------------------------------- запуск ---

    def retrieve(self) -> list[Data]:
        query_text = self.query.text if hasattr(self.query, "text") else str(self.query)
        query_text = (query_text or "").strip()
        if not query_text:
            return []

        client = self._client()
        try:
            version = self._resolve_version(client)
            parsed = self._parse_query(query_text)
            dense = self._embed_query(query_text)

            hits: list[dict] = []
            if self.use_exact_lookup:
                hits.extend(self._exact_lookup(client, parsed, version, dense))
            hits.extend(self._hybrid_search(client, query_text, version, dense))

            # Точные попадания идут первыми и не теряются при обрезке до
            # top_k: на запросе «что устанавливает п. 4.2.2» вектор может
            # промахнуться, а фильтр по ключу — нет.
            ordered: list[dict] = []
            seen_points: set[str] = set()
            for hit in hits:
                if hit["point_id"] in seen_points:
                    continue
                seen_points.add(hit["point_id"])
                ordered.append(hit)

            refs = self._expand_refs(client, ordered, version, dense)
            if refs:
                # Слабый ref (нашёлся по ссылке, но далеко от запроса) не
                # должен вытеснять сильное hybrid-попадание. Оставляем ref,
                # если он хотя бы вдвое близок к лучшему hybrid.
                best_hybrid = max(
                    (hit["score"] for hit in ordered if hit["source"] == "hybrid"),
                    default=0.0,
                )
                refs = [
                    hit
                    for hit in refs
                    if best_hybrid <= 0 or hit["score"] >= best_hybrid * 0.5
                ]
            ordered.extend(refs)

            if self.min_score > 0:
                ordered = [
                    hit
                    for hit in ordered
                    if hit["source"] != "hybrid" or hit["score"] >= self.min_score
                ]

            # exact — попадание по номеру пункта, его пользователь и просил;
            # он всегда первым. ref и hybrid сортируются по score: жёсткий
            # приоритет ref вытеснял сильно релевантные hybrid-попадания
            # (например, Приложение 9 из вопроса про аукцион).
            #
            # Ничьи разрешаем детерминированно (страница, затем point id):
            # RRF часто даёт одинаковый счёт нескольким кандидатам, и без
            # этого «первый» результат менялся от запуска к запуску —
            # hit@1 на одном и том же наборе скакал 60–90%.
            ordered.sort(
                key=lambda hit: (
                    0 if hit["source"] == "exact" else 1,
                    -hit["score"],
                    hit["payload"].get("page_from") or 0,
                    str(hit["point_id"]),
                )
            )

            return self._assemble(client, ordered, version, dense)
        finally:
            try:
                client.close()
            except Exception:
                pass

    def context(self) -> Data:
        """Готовый блок для промпта: пронумерованные фрагменты с цитатами."""
        documents = self.retrieve()
        if not documents:
            return Data(
                text="По запросу ничего не найдено.",
                data={"count": 0, "citations": []},
            )

        blocks = [
            f"[{index}] {doc.text}" for index, doc in enumerate(documents, start=1)
        ]
        return Data(
            text="\n\n".join(blocks),
            data={
                "count": len(documents),
                "citations": [doc.data.get("citation") for doc in documents],
            },
        )







