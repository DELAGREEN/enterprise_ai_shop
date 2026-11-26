"""Служебный компонент Qdrant для версионирования документов в Langflow.

Зачем отдельный компонент, если ингест уже есть:

* payload-индексы по ``doc_id`` / ``doc_version`` / ``clause_key`` нужны,
  чтобы фильтры ретривера не превращались в полный перебор точек;
* при выходе новой редакции НПА старые чанки надо удалять **точечно**
  (``doc_id == X AND doc_version != keep``), а не пересоздавать коллекцию —
  иначе вместе с редакцией ЕОСЗ вынесет и все остальные документы;
* перед ингестом полезно увидеть, какие редакции уже лежат в коллекции.

Компонент ничего не пишет в векторы и не требует модели эмбеддингов,
поэтому запускается быстро и безопасно.
"""

from __future__ import annotations

from lfx.custom.custom_component.component import Component
from lfx.io import BoolInput, DropdownInput, IntInput, Output, StrInput
from langflow.schema import Data

# Поля, по которым ретривер фильтрует и делает точный lookup.
INDEXED_FIELDS = {
    "doc_id": "keyword",
    "doc_version": "keyword",
    "chunk_type": "keyword",
    "block_type": "keyword",
    "clause_key": "keyword",
    "clause_number": "keyword",
    "ref_keys": "keyword",
    "appendix_no": "keyword",
    "chapter": "keyword",
    "chapter_no": "keyword",
    "article": "keyword",
    "article_no": "keyword",
    "razdel": "keyword",
    "parent_key": "keyword",
    "parent_id": "keyword",
    "page_from": "integer",
}

ACTIONS = [
    "ensure_indexes",
    "list_documents",
    "delete_old_versions",
    "delete_document",
    "collection_info",
]


class QdrantDocAdmin(Component):
    display_name = "Qdrant Doc Admin (versions)"
    description = (
        "Payload-индексы, список редакций и точечное удаление устаревших "
        "версий документа в Qdrant."
    )
    icon = "database"

    inputs = [
        StrInput(
            name="qdrant_url",
            display_name="Qdrant URL",
            value="http://qdrant:6333",
            info=(
                "Адрес REST API Qdrant внутри Docker-сети (имя сервиса, "
                "не localhost)."
            ),
        ),
        StrInput(
            name="collection_name",
            display_name="Collection",
            value="gost_chunks",
            info="Коллекция, куда пишет Qdrant Hybrid Ingest.",
        ),
        DropdownInput(
            name="action",
            display_name="Action",
            options=ACTIONS,
            value="ensure_indexes",
            info=(
                "ensure_indexes — создать payload-индексы (один раз после "
                "создания коллекции).\n"
                "list_documents — показать документы и редакции с числом точек.\n"
                "delete_old_versions — удалить все редакции doc_id, кроме "
                "keep_version.\n"
                "delete_document — удалить документ целиком.\n"
                "collection_info — размер коллекции и наличие векторов."
            ),
        ),
        StrInput(
            name="doc_id",
            display_name="Document ID",
            value="",
            info=(
                "Идентификатор документа из парсера (например eosz). "
                "Обязателен для delete_old_versions и delete_document; "
                "для list_documents пусто = показать все."
            ),
        ),
        StrInput(
            name="keep_version",
            display_name="Keep version",
            value="",
            info=(
                "Редакция, которую нужно оставить (например 2020-07-17). "
                "Пусто — удаляются все редакции указанного doc_id."
            ),
        ),
        BoolInput(
            name="dry_run",
            display_name="Dry run",
            value=True,
            info=(
                "Только показать, что будет удалено, без удаления. "
                "Выключайте осознанно: удаление в Qdrant необратимо."
            ),
        ),
        IntInput(
            name="timeout",
            display_name="Timeout (s)",
            value=120,
            info=(
                "Сколько ждать HTTP-ответа Qdrant. Создание payload-индекса "
                "блокирует запрос до окончания постройки, поэтому на больших "
                "коллекциях значение приходится увеличивать."
            ),
        ),
    ]

    outputs = [
        Output(name="result", display_name="Admin Result", method="run"),
    ]

    # ------------------------------------------------------------- helpers ---

    def _client(self):
        from qdrant_client import QdrantClient

        return QdrantClient(url=self.qdrant_url, timeout=int(self.timeout or 60))

    def _collection_exists(self, client) -> bool:
        try:
            client.get_collection(self.collection_name)
            return True
        except Exception:
            return False

    @staticmethod
    def _schema(kind: str):
        from qdrant_client import models

        return {
            "keyword": models.PayloadSchemaType.KEYWORD,
            "integer": models.PayloadSchemaType.INTEGER,
        }[kind]

    def _doc_filter(self, doc_id: str, keep_version: str = ""):
        """Фильтр «этот документ, но не эта редакция»."""
        from qdrant_client import models

        must = [
            models.FieldCondition(key="doc_id", match=models.MatchValue(value=doc_id))
        ]
        must_not = []
        if keep_version:
            must_not.append(
                models.FieldCondition(
                    key="doc_version", match=models.MatchValue(value=keep_version)
                )
            )
        return models.Filter(must=must, must_not=must_not or None)

    # ------------------------------------------------------------- actions ---

    def _wait_for_indexes(self, client, expected: set[str]) -> tuple[set[str], set[str]]:
        """Ждёт, пока Qdrant построит индексы, но не дольше ``timeout``.

        ``create_payload_index(wait=True)`` на реальных коллекциях упирается
        в таймаут HTTP-клиента: индекс создаётся, а ответ приходит позже.
        Поэтому создаём без ожидания и опрашиваем схему сами.
        """
        import time

        deadline = time.time() + max(5, int(self.timeout))
        while True:
            try:
                info = client.get_collection(self.collection_name)
                have = set((getattr(info, "payload_schema", None) or {}).keys())
            except Exception:
                have = set()

            if expected <= have or time.time() >= deadline:
                return have & expected, expected - have

            time.sleep(2)

    def _ensure_indexes(self, client) -> Data:
        if not self._collection_exists(client):
            return Data(
                text=(
                    f"Коллекция '{self.collection_name}' не найдена — "
                    f"сначала загрузите документ."
                ),
                data={"ok": False, "action": "ensure_indexes"},
            )

        existing: set[str] = set()
        try:
            info = client.get_collection(self.collection_name)
            existing = set((getattr(info, "payload_schema", None) or {}).keys())
        except Exception:
            pass

        created, skipped = [], []
        errors: dict[str, str] = {}
        for field, kind in INDEXED_FIELDS.items():
            if field in existing:
                skipped.append(field)
                continue
            try:
                client.create_payload_index(
                    collection_name=self.collection_name,
                    field_name=field,
                    field_schema=self._schema(kind),
                    wait=False,
                )
                created.append(field)
            except Exception as exc:  # noqa: BLE001
                # На реальных коллекциях REST-вызов создания индекса
                # блокируется до окончания постройки и упирается в таймаут
                # клиента, хотя индекс всё равно создаётся. Поэтому не
                # считаем это ошибкой — проверяем схему опросом ниже.
                created.append(field)
                errors[field] = f"{type(exc).__name__}: {exc}"

        ready, missing = (
            self._wait_for_indexes(client, set(created)) if created else (set(), set())
        )

        lines = [
            f"Индексы коллекции '{self.collection_name}':",
            f"уже были — {', '.join(skipped) or '—'}",
            f"готовы — {', '.join(sorted(ready)) or '—'}",
        ]
        if missing:
            lines.append(
                "пока не видны — "
                + ", ".join(sorted(missing))
                + " (Qdrant достраивает индекс; повторите collection_info позже)"
            )
            timed_out = [
                field
                for field in missing
                if "timeout" in errors.get(field, "").lower()
                or "ResponseHandling" in errors.get(field, "")
            ]
            if timed_out:
                lines.append(
                    "таймаут HTTP при создании: "
                    + ", ".join(sorted(timed_out))
                    + " — индекс создаётся асинхронно, это норма для больших коллекций"
                )
            hard_failed = {
                field: err
                for field, err in errors.items()
                if field not in timed_out
            }
            if hard_failed:
                lines.append(
                    "ошибки: "
                    + "; ".join(f"{k} -> {v}" for k, v in sorted(hard_failed.items()))
                )

        return Data(
            text="\n".join(lines),
            data={
                "ok": not missing,
                "action": "ensure_indexes",
                "created": created,
                "skipped": skipped,
                "ready": sorted(ready),
                "missing": sorted(missing),
                "errors": errors,
            },
        )

    def _list_documents(self, client) -> Data:
        if not self._collection_exists(client):
            return Data(
                text=f"Коллекция '{self.collection_name}' не найдена.",
                data={"ok": False, "action": "list_documents"},
            )

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

        # facet вместо scroll: scroll читает payload с диска десятками секунд,
        # а для списка редакций достаточно агрегации по полю.
        try:
            docs_resp = client.facet(
                collection_name=self.collection_name,
                key="doc_id",
                limit=100,
                filter=point_filter,
            )
            doc_values = [str(hit.value) for hit in docs_resp.hits if hit.value]
        except Exception as exc:  # noqa: BLE001
            return Data(
                text=f"Не удалось получить список документов: {exc}",
                data={"ok": False, "action": "list_documents"},
            )

        if not doc_values:
            return Data(
                text="В коллекции нет точек (или фильтр ничего не нашёл).",
                data={"ok": True, "action": "list_documents", "documents": []},
            )

        lines = [f"Документы в '{self.collection_name}':"]
        documents = []
        for value in sorted(doc_values):
            doc_filter = models.Filter(
                must=[
                    models.FieldCondition(
                        key="doc_id", match=models.MatchValue(value=value)
                    )
                ]
            )
            try:
                ver_resp = client.facet(
                    collection_name=self.collection_name,
                    key="doc_version",
                    limit=100,
                    filter=doc_filter,
                )
                versions = sorted(str(h.value) for h in ver_resp.hits if h.value)
            except Exception:
                versions = []

            for version in versions or ["—"]:
                count = client.count(
                    collection_name=self.collection_name,
                    count_filter=(
                        doc_filter
                        if version == "—"
                        else models.Filter(
                            must=doc_filter.must
                            + [
                                models.FieldCondition(
                                    key="doc_version",
                                    match=models.MatchValue(value=version),
                                )
                            ]
                        )
                    ),
                    exact=True,
                ).count
                lines.append(f"— {value} · редакция {version}: {count} точек")
                documents.append(
                    {"doc_id": value, "doc_version": version, "points": count}
                )

        return Data(
            text="\n".join(lines),
            data={"ok": True, "action": "list_documents", "documents": documents},
        )

    def _delete_old_versions(self, client) -> Data:
        doc_id = (self.doc_id or "").strip()
        if not doc_id:
            return Data(
                text="Укажите Document ID — иначе удалять нечего.",
                data={"ok": False, "action": "delete_old_versions"},
            )
        if not self._collection_exists(client):
            return Data(
                text=f"Коллекция '{self.collection_name}' не найдена.",
                data={"ok": False, "action": "delete_old_versions"},
            )

        keep = (self.keep_version or "").strip()
        point_filter = self._doc_filter(doc_id, keep)
        affected = client.count(
            collection_name=self.collection_name,
            count_filter=point_filter,
            exact=True,
        ).count

        target = f"все редакции, кроме {keep}" if keep else "все редакции"

        if self.dry_run:
            return Data(
                text=(
                    f"[dry run] Будет удалено {affected} точек: "
                    f"doc_id={doc_id}, {target}."
                ),
                data={
                    "ok": True,
                    "action": "delete_old_versions",
                    "dry_run": True,
                    "deleted": 0,
                    "matched": affected,
                },
            )

        client.delete(
            collection_name=self.collection_name,
            points_selector=point_filter,
            wait=True,
        )
        return Data(
            text=f"Удалено {affected} точек: doc_id={doc_id}, {target}.",
            data={
                "ok": True,
                "action": "delete_old_versions",
                "dry_run": False,
                "deleted": affected,
                "matched": affected,
            },
        )

    def _delete_document(self, client) -> Data:
        doc_id = (self.doc_id or "").strip()
        if not doc_id:
            return Data(
                text="Укажите Document ID — иначе удалять нечего.",
                data={"ok": False, "action": "delete_document"},
            )
        if not self._collection_exists(client):
            return Data(
                text=f"Коллекция '{self.collection_name}' не найдена.",
                data={"ok": False, "action": "delete_document"},
            )

        point_filter = self._doc_filter(doc_id)
        affected = client.count(
            collection_name=self.collection_name,
            count_filter=point_filter,
            exact=True,
        ).count

        if self.dry_run:
            return Data(
                text=f"[dry run] Будет удалён документ {doc_id}: {affected} точек.",
                data={
                    "ok": True,
                    "action": "delete_document",
                    "dry_run": True,
                    "deleted": 0,
                    "matched": affected,
                },
            )

        client.delete(
            collection_name=self.collection_name,
            points_selector=point_filter,
            wait=True,
        )
        return Data(
            text=f"Документ {doc_id} удалён: {affected} точек.",
            data={
                "ok": True,
                "action": "delete_document",
                "dry_run": False,
                "deleted": affected,
                "matched": affected,
            },
        )

    def _collection_info(self, client) -> Data:
        if not self._collection_exists(client):
            return Data(
                text=f"Коллекция '{self.collection_name}' не найдена.",
                data={"ok": False, "action": "collection_info"},
            )

        info = client.get_collection(self.collection_name)
        vectors = list((info.config.params.vectors or {}).keys())
        sparse = list((info.config.params.sparse_vectors or {}).keys())
        payload_schema = list((getattr(info, "payload_schema", None) or {}).keys())

        lines = [
            f"Коллекция '{self.collection_name}': {info.points_count} точек.",
            f"dense-векторы: {', '.join(vectors) or '—'}",
            f"sparse-векторы: {', '.join(sparse) or '—'}",
            (
                "payload-индексы: "
                + (", ".join(payload_schema) or "— (запустите ensure_indexes)")
            ),
        ]
        missing = [field for field in INDEXED_FIELDS if field not in payload_schema]
        if missing:
            lines.append("нет индексов: " + ", ".join(missing))

        return Data(
            text="\n".join(lines),
            data={
                "ok": True,
                "action": "collection_info",
                "points": info.points_count,
                "dense_vectors": vectors,
                "sparse_vectors": sparse,
                "payload_indexes": payload_schema,
                "missing_indexes": missing,
            },
        )

    # ---------------------------------------------------------------- entry ---

    def run(self) -> Data:
        action = (self.action or "ensure_indexes").strip()
        if action not in ACTIONS:
            return Data(
                text=(
                    f"Неизвестное действие '{action}'. "
                    f"Доступны: {', '.join(ACTIONS)}."
                ),
                data={"ok": False, "action": action},
            )

        client = self._client()
        try:
            if action == "ensure_indexes":
                return self._ensure_indexes(client)
            if action == "list_documents":
                return self._list_documents(client)
            if action == "delete_old_versions":
                return self._delete_old_versions(client)
            if action == "delete_document":
                return self._delete_document(client)
            return self._collection_info(client)
        finally:
            try:
                client.close()
            except Exception:
                pass



