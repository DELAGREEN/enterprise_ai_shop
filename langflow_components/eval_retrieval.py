"""Оценка релевантности поиска по ЕОСЗ — без запуска Langflow.

Гоняет **настоящий** компонент ``EoszRetriever`` (тот же код, что в flow),
подменяя только внешнее окружение: модель эмбеддингов берётся по HTTP у
vllm-embedding, ``log`` заменяется на список. Так проверяется именно то, что
увидит LLM, а не переписанная «похожая» логика.

Запуск — **внутри контейнера langflow** (компоненту нужен пакет ``lfx``):

    docker cp langflow_components/eosz_retriever.py  langflow:/tmp/comp/
    docker cp langflow_components/eval_retrieval.py  langflow:/tmp/comp/
    docker exec -w /tmp/comp langflow /app/.venv/bin/python eval_retrieval.py

Что считаем:

* **hit@1 / hit@k** — попал ли ожидаемый фрагмент в первую / первые k позиций;
* **MRR** — 1/позиция первого правильного попадания (усреднённо);
* по типам вопросов: точный номер пункта, смысловой запрос, таблица.

Эталонные вопросы строятся на реальных данных коллекции: номера пунктов,
статей и приложений взяты из самого документа, поэтому ожидание проверяемо
машинно, а не «на глаз».
"""

from __future__ import annotations

import json
import sys
import urllib.request

from eosz_retriever import REF_SCOPE_APPENDIX, EoszRetriever

EMBED_URL = "http://vllm-embedding:8080/v1/embeddings"
EMBED_MODEL = "Qwen/Qwen3-Embedding-0.6B"

# Вопросы и машинно проверяемое ожидание.
#   clause   — номер пункта в payload (проверяет точный поиск по ключу)
#   article  — номер статьи
#   appendix — номер приложения
#   keyword  — подстрока в тексте (для смысловых запросов без структуры)
#   any      — хотя бы одно из перечисленных условий
QUESTIONS = [
    {
        "q": "Что устанавливает пункт 5.2.2?",
        "kind": "exact", "clause": "5.2.2",
    },
    {
        "q": "О чём говорит статья 3.4?",
        "kind": "exact", "article": "3.4",
    },
    {
        "q": "п. 8.2.1.1",
        "kind": "exact", "clause": "8.2.1.1",
    },
    {
        "q": "Перечень продукции, при закупке которой проводится аукцион",
        "kind": "semantic", "appendix": "9",
    },
    {
        "q": "Порядок проведения неконкурентных закупок у единственного поставщика",
        "kind": "semantic", "appendix": "12",
    },
    {
        "q": "Допустимые требования к участникам закупки в документации",
        "kind": "semantic", "appendix": "10",
    },
    {
        "q": "Требования к предприятиям-подрядчикам",
        "kind": "table", "appendix": "10",
    },
    {
        "q": "Обеспечение исполнения договора",
        "kind": "semantic", "keyword": "обеспечени",
    },
    {
        "q": "Обжалование результатов закупки",
        "kind": "semantic", "keyword": "обжалован",
    },
    {
        "q": "Порядок формирования начальной максимальной цены договора",
        "kind": "semantic", "keyword": "начальн",
    },
    # Вопросы из реального чата: именно на них пользователь проверял
    # систему, поэтому они важнее придуманных. Ожидание — дословная норма
    # из документа.
    {
        "q": "В течении какого срока осуществляется возврат обеспечения заявки "
             "на участие в закупке победителю состоявшейся закупки?",
        "kind": "semantic", "keyword": "5 (пяти) рабочих дней",
    },
    {
        "q": "Не позднее какого времени организатор закупки по решению заказчика "
             "второй группы вправе отменить запрос предложений?",
        "kind": "exact", "clause": "6.2.2.5",
    },
    {
        "q": "Какое достаточное количество источников ценовой информации "
             "для проведения мелких закупок",
        "kind": "semantic", "keyword": "источников ценовой информации",
    },
]


class _Embedding:
    """Модель эмбеддингов через OpenAI-совместимый API vLLM."""

    def embed_query(self, text: str) -> list[float]:
        body = json.dumps({"model": EMBED_MODEL, "input": [text]}).encode()
        req = urllib.request.Request(
            EMBED_URL, data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer x"},
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)["data"][0]["embedding"]

    def embed_documents(self, texts):
        return [self.embed_query(t) for t in texts]


class _Query:
    def __init__(self, text: str):
        self.text = text


def build_retriever(query: str, top_k: int = 8) -> EoszRetriever:
    """Собирает компонент с дефолтами flow и подменённым окружением."""
    obj = object.__new__(EoszRetriever)
    settings = dict(
        qdrant_url="http://qdrant:6333",
        collection_name="ЕОСЗ",
        dense_vector_name="text-dense",
        sparse_vector_name="text-sparse",
        doc_id="eosz",
        doc_version="",
        top_k=top_k,
        prefetch_limit=30,
        expand_parents=4,
        use_exact_lookup=True,
        use_ref_expansion=True,
        ref_scope=REF_SCOPE_APPENDIX,
        max_ref_hops=3,
        min_score=0.0,
        include_citation=True,
    )
    for key, value in settings.items():
        setattr(obj, key, value)

    obj.query = _Query(query)
    obj.embedding_model = _Embedding()
    # lfx-овский log() требует внутреннего состояния компонента — заменяем.
    obj.log = lambda *a, **k: None
    return obj


def matches(doc, expect: dict) -> bool:
    """Проверяет, тот ли это фрагмент. Условия объединяются по ИЛИ."""
    data = doc.data or {}
    if "clause" in expect:
        value = str(data.get("clause_number") or "")
        want = str(expect["clause"])
        if value and (value == want or value.endswith("." + want)):
            return True
    if "article" in expect:
        value = str(data.get("article_no") or "")
        want = str(expect["article"])
        if value and (value == want or value.startswith(want + ".")):
            return True
    if "appendix" in expect:
        if str(data.get("appendix_no") or "") == str(expect["appendix"]):
            return True
    if "keyword" in expect:
        if expect["keyword"].lower() in (doc.text or "").lower():
            return True
    return False


def evaluate(top_k: int = 8) -> list[dict]:
    rows = []
    for item in QUESTIONS:
        retriever = build_retriever(item["q"], top_k=top_k)
        docs = retriever.retrieve()

        rank = None
        for position, doc in enumerate(docs, start=1):
            if matches(doc, item):
                rank = position
                break

        rows.append({
            "q": item["q"],
            "kind": item["kind"],
            "rank": rank,
            "found": len(docs),
            "dups": sum(1 for doc in docs if duplicate_headers(doc) > 1),
            "top": (docs[0].data or {}).get("citation") if docs else None,
        })
    return rows


def show_question(index: int, top_k: int = 8) -> None:
    """Печатает, что реально уйдёт в LLM по одному вопросу."""
    item = QUESTIONS[index]
    retriever = build_retriever(item["q"], top_k=top_k)
    docs = retriever.retrieve()

    print(f"ВОПРОС: {item['q']}")
    print("ожидание:", {k: v for k, v in item.items() if k not in ("q", "kind")})
    print(f"найдено фрагментов: {len(docs)}\n")
    for position, doc in enumerate(docs, start=1):
        data = doc.data or {}
        mark = "OK " if matches(doc, item) else "   "
        print(f"{mark}[{position}] score={data.get('score'):.4f} "
              f"источник={data.get('source')} parent={data.get('expanded_to_parent')}")
        print(f"     {data.get('citation')}")
        print(f"     {(doc.text or '')[:300].replace(chr(10), ' ')}…\n")


def duplicate_headers(doc) -> int:
    """Сколько раз в тексте повторился заголовок-хлебная крошка.

    Парсер пишет breadcrumb в начало каждого чанка, а ретривер раньше
    добавлял сверху ещё и citation. В контексте заголовок задваивался —
    лишние токены и путаница для модели. Нормальный чанк содержит
    «(ред. » ровно один раз.
    """
    return (doc.text or "").count("(ред. ")


def report(rows: list[dict], top_k: int) -> None:
    width = max(len(r["q"]) for r in rows)
    print(f"{'вопрос':<{width}}  тип       ранг  top-1")
    print("-" * (width + 44))
    for r in rows:
        rank = str(r["rank"]) if r["rank"] else "—"
        print(f"{r['q']:<{width}}  {r['kind']:<9} {rank:>4}  {r['top'] or '—'}")

    def stats(subset: list[dict]) -> tuple[float, float, float]:
        if not subset:
            return 0.0, 0.0, 0.0
        hit1 = sum(1 for r in subset if r["rank"] == 1) / len(subset)
        hitk = sum(1 for r in subset if r["rank"]) / len(subset)
        mrr = sum(1.0 / r["rank"] for r in subset if r["rank"]) / len(subset)
        return hit1, hitk, mrr

    print()
    print(f"{'срез':<12} {'hit@1':>7} {'hit@' + str(top_k):>7} {'MRR':>7}   n")
    for name, subset in [("все", rows)] + [
        (kind, [r for r in rows if r["kind"] == kind])
        for kind in ("exact", "semantic", "table")
    ]:
        hit1, hitk, mrr = stats(subset)
        print(f"{name:<12} {hit1:>6.0%} {hitk:>7.0%} {mrr:>7.2f}  {len(subset):>3}")

    total_docs = sum(r["found"] for r in rows)
    total_dups = sum(r["dups"] for r in rows)
    print(f"\nзадвоенный заголовок в выдаче: {total_dups} из {total_docs} фрагментов")


def stability(runs: int, top_k: int = 8) -> None:
    """Повторяет прогон и показывает разброс hit@1.

    Кандидаты с одинаковым RRF-счётом (частый случай: 0.5 у первых двух)
    упорядочиваются произвольно, поэтому «первый» результат может меняться
    от запуска к запуску. Для ответа это обычно не критично (в LLM уходят
    все top_k), но метрика hit@1 без учёта разброса врёт.
    """
    values = []
    for _ in range(runs):
        rows = evaluate(top_k=top_k)
        values.append(sum(1 for r in rows if r["rank"] == 1) / len(rows))

    low, high = min(values), max(values)
    print(f"hit@1 за {runs} прогонов: min {low:.0%}, max {high:.0%}, "
          f"среднее {sum(values) / len(values):.0%}")
    if high > low:
        print("вывод: порядок в верхушке нестабилен — равные RRF-счёта "
              "разрешаются произвольно")


def main() -> None:
    top_k = 8
    args = sys.argv[1:]

    if "--top-k" in args:
        top_k = int(args[args.index("--top-k") + 1])
    if "--show" in args:
        show_question(int(args[args.index("--show") + 1]), top_k=top_k)
        return
    if "--runs" in args:
        stability(int(args[args.index("--runs") + 1]), top_k=top_k)
        return

    report(evaluate(top_k=top_k), top_k)


if __name__ == "__main__":
    main()

