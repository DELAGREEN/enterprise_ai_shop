"""Qdrant Hybrid Ingest с отчётом о прогрессе.

Версия вашего компонента «Qdrant Hybrid Ingest (Dense + BM25)» с одним
отличием: пока идут эмбеддинги и запись, компонент отправляет прогресс в
enterprise_ai_shop (``progress_url`` приходит через tweaks).

Порядок стадий: ``embedding`` → ``upserting`` → ``done``. Парсер отдаёт
свою часть (``parsing`` / ``chunking``), поэтому в админке видна вся цепочка:
разбор PDF → нарезка → эмбеддинги → запись в Qdrant.

Отправка троттлится и никогда не роняет ингест: недоступный приёмник
прогресса не должен ломать загрузку документа.
"""

from __future__ import annotations

import math
import time
import uuid

from lfx.custom.custom_component.component import Component
from lfx.io import BoolInput, HandleInput, IntInput, Output, StrInput
from langflow.schema import Data


class QdrantHybridIngest(Component):
    display_name = "Qdrant Hybrid Ingest (Dense + BM25 + progress)"
    description = (
        "Загружает чанки в Qdrant с dense и sparse векторами и отчитывается "
        "о прогрессе в приложение."
    )
    icon = "database"

    _sparse_encoder = None

    inputs = [
        HandleInput(
            name="chunks",
            display_name="Parsed Chunks",
            input_types=["Data"],
            is_list=True,
            info="Подключите выход Parsed Chunks парсера",
        ),
        HandleInput(
            name="embedding_model",
            display_name="Embedding Model",
            input_types=["Embeddings"],
            info="Подключите выход компонента EmbeddingModelComponent",
        ),
        StrInput(
            name="job_id",
            display_name="Job ID (прогресс)",
            value="",
            info="Заполняется приложением через tweaks. Пусто — без прогресса.",
        ),
        StrInput(
            name="progress_url",
            display_name="Progress URL",
            value="",
            info=(
                "Адрес приёмника прогресса. Заполняется приложением через "
                "tweaks — вручную не нужно. host.docker.internal работает "
                "только при наличии extra_hosts: "
                "host.docker.internal:host-gateway у сервиса langflow."
            ),
        ),
        StrInput(
            name="progress_token",
            display_name="Progress token",
            value="",
            info="Общий секрет для приёмника прогресса.",
        ),
        IntInput(
            name="progress_interval",
            display_name="Progress interval (s)",
            value=2,
            info="Как часто отправлять прогресс. 0 — отключить.",
        ),
        StrInput(
            name="qdrant_url",
            display_name="Qdrant URL",
            value="http://qdrant:6333",
            info=(
                "Полный адрес REST API Qdrant. Внутри Docker-сети указывается "
                "имя сервиса и порт 6333, например http://qdrant:6333. "
                "Не используйте localhost — внутри контейнера он указывает на "
                "сам Langflow."
            ),
        ),
        StrInput(
            name="collection_name",
            display_name="Collection",
            value="gost_chunks",
            info="Имя коллекции в Qdrant. Если её нет — будет создана.",
        ),
        StrInput(
            name="dense_vector_name",
            display_name="Dense vector name",
            value="text-dense",
            info="Имя именованного dense-вектора. Должно совпадать с ретривером.",
        ),
        StrInput(
            name="sparse_vector_name",
            display_name="Sparse vector name",
            value="text-sparse",
            info="Имя sparse-вектора (BM25). Должно совпадать с ретривером.",
        ),
        IntInput(
            name="max_embed_chars",
            display_name="Max chars for embedding",
            value=1800,
            info=(
                "Сколько символов текста отдавать модели эмбеддингов. Текст в "
                "payload остаётся полным — усекается только то, что уходит в "
                "модель. Нужно из-за max_model_len: у Qwen3-Embedding-0.6B это "
                "1024 токена, а русский текст даёт ~2.2 символа на токен. "
                "0 — не усекать (тогда слишком длинные чанки будут пропущены)."
            ),
        ),
        IntInput(
            name="batch_size",
            display_name="Batch size",
            value=64,
            info=(
                "Сколько чанков за раз отправлять в embedding-модель и Qdrant. "
                "16–32 надёжнее на слабых машинах, 64–128 — оптимум."
            ),
        ),
        BoolInput(
            name="recreate_collection",
            display_name="Recreate collection",
            value=False,
            info="⚠️ Удаляет коллекцию и создаёт заново. Только при смене схемы.",
        ),
    ]

    outputs = [
        Output(name="status", display_name="Ingest Status", method="ingest"),
    ]

    # ------------------------------------------------------------ прогресс ---

    def _send_progress(self, stage, done=None, total=None, message=None, force=False):
        """Отправка прогресса. Не бросает: приёмник может быть недоступен."""
        url = str(getattr(self, "progress_url", "") or "").strip()
        interval = int(getattr(self, "progress_interval", 2) or 0)
        if not url or interval <= 0:
            return

        now = time.monotonic()
        if not force and (now - getattr(self, "_progress_last_sent", 0.0)) < interval:
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
        except Exception as exc:  # noqa: BLE001
            self.log(f"Прогресс не отправлен ({stage}): {exc}")

    def _estimate_eta(self, stage, done, total, now):
        """Остаток по средней скорости этапа (см. парсер — та же логика)."""
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

    # ------------------------------------------------------------- эмбеддинги ---

    def _embed_one(self, text: str):
        """Один текст → вектор или None. Первую ошибку запоминаем целиком."""
        model = self.embedding_model
        try:
            if hasattr(model, "embed_query"):
                return model.embed_query(text)
            return model.embed_documents([text])[0]
        except Exception as exc:  # noqa: BLE001
            if getattr(self, "_embed_error", None) is None:
                self._embed_error = f"{type(exc).__name__}: {exc}"
            return None

    def _embed_with_shrink(self, text: str, limit: int, stats: dict):
        """Пробует всё меньшие куски текста, пока модель не примет.

        Фиксированного лимита в символах недостаточно: соотношение
        «символы / токены» зависит от текста. Реальный юридический текст
        даёт ~2.5 символа на токен, а плотный (коды ОКПД, повторы, цифры) —
        ~1.7. Поэтому 1800 символов одного текста проходят, а другого — нет.
        Уменьшаем вдвое: 1800 → 900 → 450 → 200.
        """
        current = limit or len(text)
        while current >= 200:
            piece = text[:current]
            if len(piece) < len(text):
                vector = self._embed_one(piece)
                if vector is not None:
                    stats["truncated"] += 1
                    return vector
            current //= 2
        return None

    def _embed_texts(self, texts: list[str], stats: dict) -> list:
        """Эмбеддинги батча с деградацией по одному.

        Раньше ошибка батча означала потерю всех его чанков: один parent
        длиннее ``max_model_len`` ронял запрос на 64 текста, и в Qdrant не
        попадали 63 исправных. Теперь:

        1. пробуем батч целиком (быстро);
        2. не вышло — по одному;
        3. не прошёл отдельный текст — режем его и пробуем снова, уменьшая
           кусок вдвое. Вектор считается по началу, а в payload остаётся
           полный текст.

        Возвращает список векторов, где ``None`` = текст не осилила модель
        даже частями.
        """
        try:
            vectors = self.embedding_model.embed_documents(texts)
            if len(vectors) == len(texts):
                return vectors
            self.log(
                f"Модель вернула {len(vectors)} векторов на {len(texts)} текстов — "
                f"перехожу на поштучную обработку"
            )
        except Exception as exc:  # noqa: BLE001
            self.log(
                f"Батч из {len(texts)} текстов не прошёл ({type(exc).__name__}): {exc}. "
                f"Пробую по одному, чтобы не потерять остальные."
            )
            stats["batch_failures"] += 1

        result = []
        limit = int(getattr(self, "max_embed_chars", 0) or 0)

        for text in texts:
            vector = self._embed_one(text)
            if vector is None:
                vector = self._embed_with_shrink(text, limit, stats)
            if vector is None:
                stats["embed_failed"] += 1
            result.append(vector)

        return result

    # ------------------------------------------------------------ энкодеры ---

    @classmethod
    def _get_sparse_encoder(cls):
        if cls._sparse_encoder is None:
            try:
                from fastembed import SparseTextEmbedding
            except ImportError:
                raise ImportError(
                    "Не установлен fastembed. Добавьте в Dockerfile: "
                    "RUN /app/.venv/bin/python -m pip install --no-cache-dir fastembed"
                )
            cls._sparse_encoder = SparseTextEmbedding(model_name="Qdrant/bm25")
        return cls._sparse_encoder

    @staticmethod
    def _json_safe(value):
        """Приводит значение к JSON-сериализуемому виду (Qdrant строг к типам)."""
        if value is None or isinstance(value, (bool, int, str)):
            return value
        if isinstance(value, float):
            if math.isnan(value) or math.isinf(value):
                return None
            return value
        if isinstance(value, (list, tuple)):
            return [QdrantHybridIngest._json_safe(v) for v in value]
        if isinstance(value, dict):
            return {str(k): QdrantHybridIngest._json_safe(v) for k, v in value.items()}
        try:
            import pandas as pd

            if pd.isna(value):
                return None
        except Exception:
            pass
        return str(value)

    @staticmethod
    def _make_point_id(filename: str, chunk_index, text: str) -> str:
        """Детерминированный UUID — идемпотентность при повторном ингесте."""
        seed = f"{filename}::{chunk_index}::{text[:300]}"
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))

    # ----------------------------------------------------------- коллекция ---

    def _ensure_collection(self, client, dense_size: int):
        exists = False
        try:
            client.get_collection(self.collection_name)
            exists = True
        except Exception:
            exists = False

        if exists and self.recreate_collection:
            self.log(f"Удаляю коллекцию '{self.collection_name}'")
            client.delete_collection(self.collection_name)
            exists = False

        if not exists:
            from qdrant_client import models

            self.log(
                f"Создаю коллекцию '{self.collection_name}' "
                f"(dense='{self.dense_vector_name}' size={dense_size}, "
                f"sparse='{self.sparse_vector_name}')"
            )
            client.create_collection(
                collection_name=self.collection_name,
                vectors_config={
                    self.dense_vector_name: models.VectorParams(
                        size=dense_size,
                        distance=models.Distance.COSINE,
                    ),
                },
                sparse_vectors_config={
                    self.sparse_vector_name: models.SparseVectorParams(
                        modifier=models.Modifier.IDF,
                    ),
                },
            )
        else:
            info = client.get_collection(self.collection_name)
            vectors = info.config.params.vectors or {}
            sparse = info.config.params.sparse_vectors or {}
            if self.dense_vector_name not in vectors:
                raise ValueError(
                    f"Коллекция '{self.collection_name}' не содержит вектор "
                    f"'{self.dense_vector_name}' (есть: {list(vectors.keys())}). "
                    f"Включите 'Recreate collection' = true, чтобы пересоздать."
                )
            if self.sparse_vector_name not in sparse:
                raise ValueError(
                    f"Коллекция '{self.collection_name}' не содержит sparse-вектор "
                    f"'{self.sparse_vector_name}'. Включите 'Recreate collection' = true."
                )

    @staticmethod
    def _meta_of(chunk) -> dict:
        """Payload-метаданные чанка: Data.data или dict["data"]."""
        if isinstance(chunk, dict):
            value = chunk.get("data")
        else:
            value = getattr(chunk, "data", None)
        return value or {}

    def _sparse_texts(self, texts: list[str], encoder, stats: dict) -> list:
        """BM25-векторы; при сбое батча — по одному."""
        try:
            vectors = list(encoder.embed(texts))
            if len(vectors) == len(texts):
                return vectors
            self.log(
                f"BM25 вернул {len(vectors)} векторов на {len(texts)} текстов — "
                f"обрабатываю по одному"
            )
        except Exception as exc:  # noqa: BLE001
            self.log(
                f"BM25 не осилил батч из {len(texts)} ({type(exc).__name__}): {exc}. "
                f"Пробую по одному."
            )

        result = []
        for text in texts:
            try:
                result.append(list(encoder.embed([text]))[0])
            except Exception:  # noqa: BLE001
                stats["sparse_failed"] += 1
                result.append(None)
        return result

    def _upsert(self, client, points: list, stats: dict) -> int:
        """Запись точек. Возвращает число записанных.

        Если батч не прошёл целиком — пробуем по одной точке: Qdrant может
        отвергнуть конкретный payload, и терять из-за него весь батч нельзя.
        """
        stats["_last_upsert_skipped"] = 0
        try:
            client.upsert(
                collection_name=self.collection_name, points=points, wait=True
            )
            self.log(f"Upsert {len(points)} точек")
            return len(points)
        except Exception as exc:  # noqa: BLE001
            self.log(
                f"Батч из {len(points)} точек не записался ({type(exc).__name__}): {exc}. "
                f"Пробую по одной."
            )

        written = 0
        for point in points:
            try:
                client.upsert(
                    collection_name=self.collection_name, points=[point], wait=True
                )
                written += 1
            except Exception as exc:  # noqa: BLE001
                stats["upsert_failed"] += 1
                stats["_last_upsert_skipped"] += 1
                if stats["upsert_failed"] <= 3:
                    self.log(f"Точка {point.id} не записалась: {exc}")

        return written

    # -------------------------------------------------------------- ингест ---

    def ingest(self) -> Data:
        from qdrant_client import QdrantClient, models

        chunks = self.chunks
        if chunks is None:
            chunks = []
        elif not isinstance(chunks, list):
            chunks = [chunks]

        if not chunks:
            self._send_progress(
                "done", done=0, total=0, message="нечего загружать", force=True
            )
            return Data(text="Нет чанков для ингеста", data={"upserted": 0, "skipped": 0})

        total = len(chunks)
        self._send_progress(
            "embedding", done=0, total=total, message="старт эмбеддингов", force=True
        )

        # Определяем размерность dense по пробному запросу
        probe = self.embedding_model.embed_query("probe")
        dense_size = len(probe)

        client = QdrantClient(url=self.qdrant_url, timeout=120)
        self._ensure_collection(client, dense_size)

        sparse_encoder = self._get_sparse_encoder()

        upserted = 0
        skipped = 0
        batch_size = max(1, self.batch_size)
        self._embed_error = None

        # Разбивка пропусков по причинам. Без неё «пропущено 704» ничего не
        # говорит: пустой текст, длинный чанк и ошибка записи лечатся
        # по-разному.
        stats = {
            "empty": 0,
            "embed_failed": 0,
            "sparse_failed": 0,
            "upsert_failed": 0,
            "truncated": 0,
            "batch_failures": 0,
        }

        for start in range(0, total, batch_size):
            batch = chunks[start:start + batch_size]
            texts, metas = [], []
            for chunk in batch:
                text = getattr(chunk, "text", None)
                if text is None and isinstance(chunk, dict):
                    text = chunk.get("text")
                text = str(text).strip() if text else ""
                if not text:
                    stats["empty"] += 1
                    skipped += 1
                    continue
                texts.append(text)
                metas.append(self._meta_of(chunk))

            if not texts:
                continue

            # Отчитываемся по эмбеддингам: после парсинга это основная стадия.
            self._send_progress(
                "embedding",
                done=start,
                total=total,
                message=f"эмбеддинги: {start} / {total}",
            )

            dense_vecs = self._embed_texts(texts, stats)

            sparse_vecs = self._sparse_texts(texts, sparse_encoder, stats)

            points = []
            for text, dense, sparse_emb, meta in zip(
                texts, dense_vecs, sparse_vecs, metas
            ):
                # None = именно этот текст не осилила модель. Раньше такой
                # чанк уносил с собой весь батч.
                if dense is None or sparse_emb is None:
                    skipped += 1
                    continue

                payload = {"text": text}
                for key, value in meta.items():
                    payload[str(key)] = self._json_safe(value)

                filename = str(meta.get("filename") or "unknown")
                chunk_index = meta.get("chunk_index", start)
                point_id = self._make_point_id(filename, chunk_index, text)

                points.append(models.PointStruct(
                    id=point_id,
                    vector={
                        self.dense_vector_name: dense,
                        self.sparse_vector_name: models.SparseVector(
                            indices=sparse_emb.indices.tolist(),
                            values=sparse_emb.values.tolist(),
                        ),
                    },
                    payload=payload,
                ))

            if not points:
                continue

            self._send_progress(
                "upserting",
                done=start,
                total=total,
                message=f"запись в Qdrant: {start} / {total}",
            )

            upserted += self._upsert(client, points, stats)
            skipped += stats["_last_upsert_skipped"]

            self._send_progress(
                "upserting",
                done=min(total, start + len(batch)),
                total=total,
                message=f"записано {upserted} из {total}",
            )

        self._send_progress(
            "done",
            done=upserted,
            total=total,
            message=f"готово: {upserted} точек",
            force=True,
        )

        # Итог должен объяснять пропуски, а не просто показывать число.
        reasons = []
        if stats["empty"]:
            reasons.append(f"пустой текст: {stats['empty']}")
        if stats["embed_failed"]:
            reasons.append(f"модель не осилила текст: {stats['embed_failed']}")
        if stats["sparse_failed"]:
            reasons.append(f"BM25 не осилил текст: {stats['sparse_failed']}")
        if stats["upsert_failed"]:
            reasons.append(f"Qdrant отверг точку: {stats['upsert_failed']}")
        if stats["truncated"]:
            reasons.append(f"усечено до {self.max_embed_chars} символов: {stats['truncated']}")

        summary = (
            f"Ингест завершён: {upserted} точек в '{self.collection_name}', "
            f"пропущено {skipped} (всего {total})"
        )
        if reasons:
            summary += ". Причины: " + "; ".join(reasons)
        if stats["embed_failed"] or stats["upsert_failed"]:
            summary += (
                ". Есть потери — повторите загрузку: идемпотентные point id "
                "перезапишут уже загруженное и добавят недостающее"
            )
        if getattr(self, "_embed_error", None):
            summary += f". Первая ошибка модели: {self._embed_error}"

        if skipped:
            self.log(summary)

        return Data(
            text=summary,
            data={
                "upserted": upserted,
                "skipped": skipped,
                "total": total,
                "skipped_breakdown": {
                    key: value
                    for key, value in stats.items()
                    if not key.startswith("_") and value
                },
                "embed_error": getattr(self, "_embed_error", None),
                "collection": self.collection_name,
                "dense_vector": self.dense_vector_name,
                "sparse_vector": self.sparse_vector_name,
            },
        )




