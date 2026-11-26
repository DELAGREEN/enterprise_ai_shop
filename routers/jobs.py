import io
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

import config
from database import get_db
import uploads
from langflow_client import LangflowClient, extract_error_text, extract_output_text
from models import Chat, ChatMessage, LLMRequestLog, ProcessingJob
from session import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

# Стадии обработки документа. Порядок важен: по нему UI рисует шаги,
# а поллер понимает, что задача действительно движется вперёд.
PROGRESS_STAGES = ("parsing", "chunking", "embedding", "upserting", "done")
_FINAL_JOB_STATUSES = ("completed", "failed", "timeout")

EMPTY_RESULT_HINT = (
    "Flow завершился без результата. Проверьте, что входной компонент "
    "(Chat Input) подключён к цепочке или удалён из flow."
)

# Голые сообщения httpx/qdrant-client мало о чём говорят пользователю.
# Проверяем по подстроке: Langflow отдаёт разные обёртки вокруг одного и
# того же таймаута («Workflow execution timed out», «Operation timed out»).
_ERROR_HINTS = (
    (
        "workflow execution timed out",
        "Langflow прервал запуск по потолку времени. Это ДВЕ разные "
        "переменные: LANGFLOW_WORKFLOW_EXECUTION_TIMEOUT (по умолчанию "
        "всего 300 с — именно она даёт «execution ceiling») и "
        "LANGFLOW_WORKER_TIMEOUT. Поднимите обе в docker-compose и "
        "перезапустите langflow. Для очень больших PDF дополнительно "
        "гнать пакетами через page_from/page_to (по 60–100 страниц).",
    ),
    (
        "execution ceiling",
        "Сработал потолок времени Langflow (LANGFLOW_WORKFLOW_EXECUTION_TIMEOUT, "
        "по умолчанию 300 с). Поднимите его в docker-compose и перезапустите "
        "langflow; большие документы дополнительно гнать пакетами "
        "page_from/page_to.",
    ),
    (
        "job timed out",
        "Задача не успела за отведённое время. Разбейте документ на части "
        "через page_from/page_to или увеличьте LANGFLOW_WORKER_TIMEOUT и "
        "LANGFLOW_WORKFLOW_EXECUTION_TIMEOUT.",
    ),
    (
        "timed out",
        "Таймаут при обращении к внешнему сервису (Qdrant или модель "
        "эмбеддингов). Увеличьте timeout в компоненте загрузки.",
    ),
    (
        "all connection attempts failed",
        "Не удалось подключиться к Langflow — сервис недоступен.",
    ),
)


def _friendly_error(text: str) -> str:
    """Подставляет понятное описание вместо технического «timed out»."""
    stripped = (text or "").strip()
    lowered = stripped.lower()
    for needle, hint in _ERROR_HINTS:
        if needle in lowered:
            return f"{hint} (детали: {stripped})"
    return stripped


def _iso_utc(value: datetime | None) -> str | None:
    """Время из БД в ISO-строке с явной зоной UTC.

    В БД все даты — naive UTC (``datetime.utcnow``). Если отдать такую строку
    браузеру как есть (``2026-11-26T07:13:04``), то ``new Date(...)`` по
    стандарту разберёт её как ЛОКАЛЬНОЕ время. Для Москвы это сдвиг на 3 часа:
    только что обновлённый прогресс выглядел как «нет обновлений 180 мин»,
    а ETA показывался на 3 часа раньше.

    Поэтому добавляем ``+00:00``: тогда браузер сам переведёт время в зону
    пользователя, и разница между сервером и клиентом исчезнет.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _last_activity(job: ProcessingJob) -> datetime | None:
    """Когда задача последний раз подавала признаки жизни.

    Для запущенной — время последнего отчёта о прогрессе, для ещё не
    подхваченной воркером — время постановки в очередь.
    """
    return job.progress_updated_at or job.created_at


def _is_stale(job: ProcessingJob, minutes: int | None = None) -> bool:
    """Зависшая задача: активна, но давно молчит.

    Компоненты шлют прогресс каждые пару секунд, поэтому долгая тишина
    означает, что воркер умер или потерял задачу. Такие записи копятся,
    занимают очередь и мешают разобраться, что реально работает.
    """
    if job.status not in ("queued", "running"):
        return False
    minutes = config.INGEST_STALE_MINUTES if minutes is None else minutes
    last = _last_activity(job)
    if last is None:
        return True
    return (datetime.utcnow() - last) > timedelta(minutes=minutes)


def _progress_of(job: ProcessingJob) -> dict:
    """Прогресс задачи в виде, удобном для UI.

    ``percent`` считается здесь, а не в браузере: у таблицы и у карточки
    задачи одна и та же логика, и не хочется дублировать округление.

    ``eta_at`` — абсолютное время окончания, а не «осталось N секунд».
    Секунды устаревают между опросами (страница обновляется раз в 10 с), а
    время окончания остаётся верным, даже если вкладка провисела минуту.
    """
    done = job.progress_done
    total = job.progress_total
    percent = None
    if done is not None and total:
        percent = max(0, min(100, int(done * 100 / total)))

    eta_seconds = job.progress_eta_seconds
    eta_at = None
    if eta_seconds and job.progress_updated_at:
        eta_at = _iso_utc(
            job.progress_updated_at + timedelta(seconds=int(eta_seconds))
        )

    return {
        "stage": job.progress_stage,
        "done": done,
        "total": total,
        "percent": percent,
        "message": job.progress_message,
        "eta_seconds": eta_seconds,
        "eta_at": eta_at,
        "updated_at": _iso_utc(job.progress_updated_at),
    }


def apply_job_status(job: ProcessingJob, lf: dict) -> bool:
    """Синхронизирует задачу с ответом Langflow.

    Возвращает True, если поля изменились и нужен commit.

    Отдельно обрабатывается «пустое» завершение: Langflow стартует запуск
    с входного компонента, поэтому неподключённый Chat Input даёт
    status=completed без единого токена в ответе. Для загрузки файлов это
    провал, а не успех.
    """
    new_status = _map_status(lf.get("status"))
    changed = False

    if new_status != job.status:
        job.status = new_status
        changed = True

    if new_status == "completed":
        text = (extract_output_text(lf) or "").strip()
        if text:
            if job.result != text:
                job.result = text
                changed = True
        elif not job.error:
            job.status = "failed"
            job.result = None
            job.error = EMPTY_RESULT_HINT
            changed = True
    elif new_status in ("failed", "timeout"):
        error = extract_error_text(lf) or f"Flow завершился со статусом {new_status}"
        error = _friendly_error(error)
        if job.error != error:
            job.error = error
            changed = True

    if job.status in ("completed", "failed", "timeout") and job.finished_at is None:
        job.finished_at = datetime.utcnow()
        changed = True

    # Задачу добили после того, как компонент уже отчитался о разборе:
    # значит разбор прошёл, а упало на эмбеддингах/записи. Голая ошибка
    # «timed out» тут не объясняет, что документ не попал в Qdrant.
    if (
        job.status in ("failed", "timeout")
        and job.progress_stage
        and job.progress_stage not in ("done",)
    ):
        note = (
            f"Обработка прервана на стадии «{job.progress_stage}"
            f"» (успело: {job.progress_done or 0} из {job.progress_total or '?'}). "
            "Данные в Qdrant не записаны."
        )
        if note not in (job.error or ""):
            job.error = f"{job.error}\n\n{note}" if job.error else note
            changed = True

    return changed


async def _claim_history(db: AsyncSession, job: ProcessingJob) -> bool:
    """Атомарно «забирает» право записать результат в историю.

    Проверки ``job.history_saved`` в памяти недостаточно: приложение
    запущено с несколькими воркерами uvicorn, у каждого свой процесс, своя
    сессия и свой поллер задач. Все они видят ``history_saved = False``
    и все пишут сообщение — в чате появляется по 3 одинаковых ответа.

    Поэтому решение принимает БД: ``UPDATE ... WHERE history_saved = false``.
    Строка блокируется до коммита, конкурент после коммита видит уже
    ``true`` и получает ``rowcount = 0``. Запись делает только победитель.
    """
    result = await db.execute(
        update(ProcessingJob)
        .where(ProcessingJob.id == job.id)
        .where(ProcessingJob.history_saved.is_(False))
        .values(history_saved=True)
        .execution_options(synchronize_session=False)
    )
    if not result.rowcount:
        return False
    # Держим объект в памяти согласованным с БД, чтобы повторный вызов
    # в этом же процессе не пошёл в базу зря.
    job.history_saved = True
    return True


async def save_job_history(db: AsyncSession, job: ProcessingJob) -> bool:
    """Переносит результат файловой задачи в историю чата.

    Файлы обрабатываются в фоне, поэтому ответ пишется в ``chat_messages``
    не сразу, а когда задача дошла до финального статуса. Право на запись
    забирается атомарно в БД (см. ``_claim_history``): иначе поллеры разных
    воркеров пишут один и тот же результат по несколько раз.

    Возвращает True, если что-то записали и нужен commit.
    """
    if not job.chat_id or job.history_saved:
        return False
    if job.status not in ("completed", "failed", "timeout"):
        return False

    chat = await db.get(Chat, job.chat_id)
    if chat is None:
        logger.warning("Чат %s для job %s не найден", job.chat_id, job.id)
        job.history_saved = True
        return True

    if chat.deleted_at is not None:
        # Чат удалили, пока задача считалась. В историю не пишем, но память
        # в Langflow могли создать заново — поллер вычистит её повторно.
        chat.langflow_purged_at = None
        logger.info("Чат %s удалён, результат job %s не сохраняем", chat.id, job.id)
        job.history_saved = True
        return True

    if not await _claim_history(db, job):
        # Другой воркер успел первым — сообщение уже пишется там.
        logger.info("История job %s уже записывается другим процессом", job.id)
        return False

    answer = ""
    if job.status == "completed" and job.result:
        answer = job.result

    if answer:
        db.add(ChatMessage(chat_id=chat.id, role="assistant", content=answer))
    else:
        reason = job.error or f"Обработка завершилась со статусом {job.status}"
        db.add(
            ChatMessage(
                chat_id=chat.id,
                role="assistant",
                content=f"⚠️ {reason}",
            )
        )

    chat.updated_at = datetime.utcnow()

    question = job.input_text or ""
    if not question and job.input_files:
        try:
            names = ", ".join(
                str(f.get("name") or "") for f in json.loads(job.input_files)
            )
        except Exception:
            names = ""
        question = f"📎 {names}" if names else ""

    # Журнал не удаляется вместе с чатом — фиксируем запрос навсегда
    db.add(
        LLMRequestLog(
            user_id=job.user_id,
            flow_id=job.flow_id,
            chat_id=chat.id,
            chat_title=chat.title,
            question=question or "—",
            answer=answer or None,
            thinking=None,
        )
    )
    return True


def build_input_value(message: str | None, uploaded: list[dict]) -> str:
    """Текст запроса к flow.

    Пользователь может отправить только файлы, только текст или всё вместе.
    Если текста нет, но есть файлы — подставляем их список.
    Пустая строка означает «отправлять нечего».
    """
    text = (message or "").strip()
    if text:
        return text
    if uploaded:
        names = ", ".join(str(up.get("name") or "") for up in uploaded)
        return f"Проанализируй файлы: {names}".strip()
    return ""


async def _create_job(
    db: AsyncSession,
    user_id: str,
    flow_id: str,
    job_type: str,
    input_files: list | None = None,
    input_text: str | None = None,
    chat_id: str | None = None,
) -> ProcessingJob:
    job = ProcessingJob(
        id=uuid.uuid4().hex,
        flow_id=flow_id,
        user_id=user_id,
        chat_id=chat_id,
        job_type=job_type,
        status="queued",
        input_files=json.dumps(input_files, ensure_ascii=False) if input_files else None,
        input_text=input_text,
    )
    db.add(job)
    await db.flush()
    return job


@router.post("/upload-and-run")
async def upload_and_run(
    request: Request,
    flow_id: str = Form(...),
    session_id: str | None = Form(None),
    message: str | None = Form(None),
    files: list[UploadFile] | None = File(None),
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not user:
        raise HTTPException(401, "Not authenticated")

    uploaded_files = files or []
    if not uploaded_files and not (message or "").strip():
        raise HTTPException(
            status_code=400, detail="Нужен текст сообщения или файл"
        )

    # Агент читает текст из документа, поэтому принимаем только текстовые
    # форматы и PDF с текстовым слоем.
    rejected = [f.filename for f in uploaded_files if not uploads.is_allowed(f.filename)]
    if rejected:
        raise HTTPException(
            status_code=400,
            detail=uploads.rejected_message(rejected[0]),
        )

    # Чат, из которого отправили файлы (None — служебная страница ingestion).
    # Проверяем владельца, иначе можно было бы писать в чужую историю.
    chat: Chat | None = None
    if session_id:
        chat_res = await db.execute(
            select(Chat).where(
                Chat.id == session_id,
                Chat.flow_id == flow_id,
                Chat.user_id == user["username"],
                Chat.deleted_at.is_(None),
            )
        )
        chat = chat_res.scalar_one_or_none()
        if chat is None:
            raise HTTPException(status_code=404, detail="Чат не найден")

    client = LangflowClient()

    # 1. Загружаем файлы в хранилище flow
    uploaded = []
    for f in uploaded_files:
        content = await f.read()
        try:
            result = await client.upload_file(
                file=io.BytesIO(content),
                filename=f.filename or "file",
                flow_id=flow_id,
            )
        except httpx.HTTPStatusError as e:
            logger.error(
                "Langflow не принял файл %s: %s %s",
                f.filename, e.response.status_code, e.response.text,
            )
            raise HTTPException(
                status_code=502,
                detail=f"Не удалось загрузить файл {f.filename} в Langflow",
            )
        except httpx.HTTPError as e:
            logger.exception("Ошибка загрузки файла %s: %s", f.filename, e)
            raise HTTPException(
                status_code=502,
                detail=f"Langflow недоступен при загрузке файла {f.filename}",
            )

        logger.info("Upload result for %s: %s", f.filename, result)

        # Langflow возвращает путь к файлу (flow-scoped upload)
        file_path = result.get("path") or result.get("file_path")
        if not file_path:
            logger.error("Langflow не вернул путь для файла %s: %s", f.filename, result)
            raise HTTPException(
                status_code=502,
                detail=f"Langflow не вернул путь для файла {f.filename}",
            )

        uploaded.append({
            "name": f.filename,
            "path": file_path,
            "size": len(content),
        })

    # 2. Формируем inputs для flow
    # Read File компонент получает пути через поле files в WorkflowRunRequest
    input_value = build_input_value(message, uploaded)

    inputs = {"input_value": input_value}
    if uploaded:
        inputs["file_paths"] = [up["path"] for up in uploaded]

    # 3. Задача создаётся ДО запуска flow: компонент Langflow должен знать
    # job_id, чтобы присылать прогресс. Раньше job создавался после запуска,
    # и адресовать прогресс было некуда — отсюда «висит и непонятно, живой ли».
    job = await _create_job(
        db, user["username"], flow_id, "ingestion",
        input_files=uploaded,
        input_text=input_value,
        chat_id=chat.id if chat else None,
    )
    await db.commit()

    progress = {
        "job_id": job.id,
        "url": f"{config.PROGRESS_BASE_URL}/api/jobs/{job.id}/progress",
        "token": config.PROGRESS_TOKEN,
    }

    # 4. Запускаем в фоне
    resp = await client.run_flow_background(
        flow_id, inputs, session_id=session_id, progress=progress
    )

    if resp.get("error"):
        logger.error("Langflow error: %s", resp.get("detail"))
        job.status = "failed"
        job.error = str(resp.get("detail") or resp.get("message") or "Ошибка запуска flow")
        job.finished_at = datetime.utcnow()
        await db.commit()
        raise HTTPException(
            status_code=resp.get("status_code") or 502,
            detail=resp.get("detail") or resp.get("message"),
        )

    langflow_job_id = resp.get("job_id")
    if not langflow_job_id:
        logger.error("Langflow не вернул job_id: %s", resp)
        job.status = "failed"
        job.error = "Langflow не вернул job_id"
        job.finished_at = datetime.utcnow()
        await db.commit()
        raise HTTPException(status_code=502, detail="Langflow не вернул job_id")

    job.langflow_job_id = langflow_job_id

    if chat is not None:
        file_names = ", ".join(up["name"] for up in uploaded)
        user_text = (message or "").strip()
        if user_text and file_names:
            content = f"{user_text}\n\n📎 {file_names}"
        else:
            content = user_text or f"📎 {file_names}"

        # Авто-название чата по первому сообщению (как в обычной отправке)
        if chat.title == "Новый чат":
            first_line = (user_text or file_names).split("\n")[0].strip()
            chat.title = first_line[:60] or "Новый чат"

        db.add(ChatMessage(chat_id=chat.id, role="user", content=content))
        chat.updated_at = datetime.utcnow()

    await db.commit()

    return {
        "job_id": job.id,
        "langflow_job_id": langflow_job_id,
        "status": "queued",
        "files": uploaded,
        "chat_title": chat.title if chat else None,
        "progress_url": progress["url"],
    }


@router.post("/jobs/{job_id}/progress")
async def report_progress(
    job_id: str,
    request: Request,
    payload: dict = Body(default={}),
    db: AsyncSession = Depends(get_db),
):
    """Приём прогресса от компонента Langflow.

    Авторизация — по общему токену, а не по сессии: у Langflow нет cookie
    пользователя. Токен передаётся заголовком ``X-Progress-Token`` или полем
    ``token`` в теле (заголовок предпочтительнее — не оседает в логах тела).

    Вызывается часто (раз в несколько секунд), поэтому отвечает 204 и не
    трогает ничего, кроме полей прогресса.
    """
    token = (request.headers.get("x-progress-token") or "").strip()
    if not token:
        token = str(payload.get("token") or "").strip()
    if not config.PROGRESS_TOKEN or token != config.PROGRESS_TOKEN:
        raise HTTPException(401, "Неверный токен прогресса")

    job = await db.get(ProcessingJob, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")

    # Поздний прогресс от зависшего воркера: задачу уже закрыли по таймауту
    # или по ответу Langflow — перезаписывать статус нельзя.
    if job.status in _FINAL_JOB_STATUSES:
        return Response(status_code=204)

    stage = str(payload.get("stage") or "").strip()
    if stage and stage not in PROGRESS_STAGES:
        raise HTTPException(400, f"Неизвестная стадия: {stage}")

    def _as_int(value):
        if value is None or value == "":
            return None
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return None

    done = _as_int(payload.get("done"))
    total = _as_int(payload.get("total"))
    eta_seconds = _as_int(payload.get("eta_seconds"))
    message = payload.get("message")
    message = str(message).strip()[:500] if message else None

    if stage:
        job.progress_stage = stage
    if done is not None:
        job.progress_done = done
    if total is not None:
        job.progress_total = total
    if message is not None:
        job.progress_message = message

    # На финальной стадии оценка «сколько осталось» теряет смысл: задача
    # уже закончилась, и «осталось 2 минуты» в UI было бы враньём.
    if stage == "done" or eta_seconds == 0:
        job.progress_eta_seconds = 0
    elif eta_seconds is not None:
        job.progress_eta_seconds = min(eta_seconds, 24 * 3600)

    job.progress_updated_at = datetime.utcnow()

    # Компонент уже работает — значит Langflow точно запустил flow, и
    # задача больше не «в очереди».
    if job.status == "queued":
        job.status = "running"

    await db.commit()
    return Response(status_code=204)


@router.get("/jobs/active")
async def active_job(
    chat_id: str | None = None,
    flow_id: str | None = None,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Незавершённая задача пользователя — для восстановления индикатора.

    Прогресс рисуется в браузере, поэтому при перезагрузке страницы он
    пропадал: задача продолжает идти, а показать её нечем. Отдаём активную
    задачу, чтобы страница могла снова подписаться на опрос.

    ВАЖНО: маршрут объявлен ДО ``/jobs/{job_id}``. Иначе FastAPI примет
    «active» за идентификатор задачи и вернёт 404.
    """
    q = (
        select(ProcessingJob)
        .where(ProcessingJob.user_id == user["username"])
        .where(ProcessingJob.status.in_(("queued", "running")))
        .order_by(ProcessingJob.created_at.desc())
        .limit(1)
    )
    if chat_id:
        q = q.where(ProcessingJob.chat_id == chat_id)
    if flow_id:
        q = q.where(ProcessingJob.flow_id == flow_id)

    res = await db.execute(q)
    job = res.scalars().first()
    if not job:
        return {"job": None}

    return {
        "job": {
            "job_id": job.id,
            "flow_id": job.flow_id,
            "chat_id": job.chat_id,
            "status": job.status,
            "progress": _progress_of(job),
            "created_at": _iso_utc(job.created_at),
        }
    }


@router.get("/jobs/{job_id}")
async def get_job(
    job_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    job = await db.get(ProcessingJob, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.user_id != user["username"] and not user.get("is_admin"):
        raise HTTPException(403, "Access denied")

    # Обновляем статус из Langflow, пока задача активна
    if job.status in ("queued", "running") and job.langflow_job_id:
        try:
            client = LangflowClient()
            lf = await client.get_job_status(job.langflow_job_id)
            changed = apply_job_status(job, lf)
            if await save_job_history(db, job):
                changed = True
            if changed:
                await db.commit()
        except Exception as e:
            logger.warning("Не удалось обновить статус job %s: %s", job_id, e)

    # Страховка: если задача уже завершилась, а история ещё не записана
    # (например, поллер не успел или приложение перезапускалось)
    elif job.status in ("completed", "failed", "timeout"):
        try:
            if await save_job_history(db, job):
                await db.commit()
        except Exception as e:
            logger.warning("Не удалось сохранить историю job %s: %s", job_id, e)

    return {
        "job_id": job.id,
        "flow_id": job.flow_id,
        "status": job.status,
        "input_files": json.loads(job.input_files) if job.input_files else [],
        "result": job.result,
        "error": job.error,
        "chat_id": job.chat_id,
        "progress": _progress_of(job),
        "created_at": _iso_utc(job.created_at),
        "finished_at": _iso_utc(job.finished_at),
    }


@router.get("/jobs")
async def list_jobs(
    flow_id: str | None = None,
    limit: int = 50,
    status: str | None = None,
    stale_only: bool = False,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Список задач для админки.

    ``stale_only`` — только зависшие (активные, но давно молчащие). Именно
    они опасны: держат воркеры Langflow и создают видимость работы.
    """
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin only")

    q = select(ProcessingJob).order_by(ProcessingJob.created_at.desc())
    if flow_id:
        q = q.where(ProcessingJob.flow_id == flow_id)
    if status:
        q = q.where(ProcessingJob.status == status)
    if stale_only:
        # Фильтр по «тишине» считается в Python: порог зависит от текущего
        # времени, и в SQL пришлось бы дублировать ту же логику.
        q = q.where(ProcessingJob.status.in_(("queued", "running")))
    else:
        q = q.limit(limit)

    res = await db.execute(q)
    jobs = list(res.scalars().all())
    if stale_only:
        jobs = [j for j in jobs if _is_stale(j)][:limit]

    return [
        {
            "job_id": j.id,
            "flow_id": j.flow_id,
            "user_id": j.user_id,
            "status": j.status,
            "progress": _progress_of(j),
            "stale": _is_stale(j),
            "created_at": _iso_utc(j.created_at),
            "finished_at": _iso_utc(j.finished_at),
        }
        for j in jobs
    ]


async def _stop_in_langflow(job: ProcessingJob) -> dict:
    """Пытается остановить запуск в Langflow. Не бросает исключений.

    Остановка нужна ДО удаления записи: иначе воркер Langflow продолжит
    считать документ, хотя в БД задачи уже нет. Пара таких «призраков»
    занимает все воркеры, и новые загрузки встают в очередь навсегда.
    """
    if not job.langflow_job_id:
        return {"stopped": False, "reason": "no_langflow_job_id"}
    if job.status not in ("queued", "running"):
        return {"stopped": False, "reason": "already_finished"}
    try:
        return await LangflowClient().stop_job(job.langflow_job_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("stop_job для %s упал: %s", job.id, e)
        return {"stopped": False, "reason": str(e)}


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Отменяет активную задачу, оставляя запись для истории.

    В отличие от удаления, задача остаётся в таблице со статусом failed —
    в чате появится «⚠️ Отменено администратором», и будет понятно, почему
    загруженный файл не дал результата.
    """
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin only")

    job = await db.get(ProcessingJob, job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    if job.status not in ("queued", "running"):
        # Повторный клик по уже отменённой задаче — не ошибка.
        return {
            "job_id": job.id,
            "cancelled": False,
            "status": job.status,
            "reason": "already_finished",
        }

    langflow = await _stop_in_langflow(job)

    job.status = "failed"
    job.error = "Отменено администратором"
    job.finished_at = datetime.utcnow()
    job.progress_eta_seconds = 0
    await db.commit()
    await save_job_history(db, job)
    await db.commit()

    logger.info(
        "Задача %s отменена администратором %s (langflow: %s)",
        job.id, user["username"], langflow,
    )
    return {
        "job_id": job.id,
        "cancelled": True,
        "status": job.status,
        "langflow": langflow,
    }


@router.delete("/jobs/{job_id}")
async def delete_job(
    job_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Удаляет задачу из очереди.

    Активную задачу сначала останавливает в Langflow: удалить только строку
    в БД мало — воркер продолжит работу, а прогресс будет стучаться в 404.

    В историю чата ничего не пишется (в отличие от отмены): для пользователя
    это выглядит так, будто загрузки не было.
    """
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin only")

    job = await db.get(ProcessingJob, job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    langflow = await _stop_in_langflow(job)
    await db.delete(job)
    await db.commit()

    logger.info(
        "Задача %s удалена администратором %s (langflow: %s)",
        job_id, user["username"], langflow,
    )
    return {"job_id": job_id, "deleted": True, "langflow": langflow}


class PurgeStalePayload(BaseModel):
    older_than_minutes: int | None = None
    action: str = "delete"          # delete | cancel
    dry_run: bool = True


@router.post("/jobs/purge-stale")
async def purge_stale_jobs(
    payload: PurgeStalePayload | None = None,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Чистит зависшие задачи — те, что молчат дольше порога.

    ``dry_run`` по умолчанию True: сначала показываем, что будет затронуто,
    и только по подтверждению чистим. Удаление активной задачи без остановки
    в Langflow оставило бы «призрака», поэтому остановка идёт первой.
    """
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin only")

    payload = payload or PurgeStalePayload()

    if payload.action not in ("delete", "cancel"):
        raise HTTPException(400, "action должен быть delete или cancel")

    # None — «взять из конфига», а 0 или минус — ошибка. Через `or` ноль
    # молча превратился бы в дефолт, и очистка удалила бы больше, чем просили.
    minutes = payload.older_than_minutes
    if minutes is None:
        minutes = config.INGEST_STALE_MINUTES
    if minutes < 1:
        raise HTTPException(400, "older_than_minutes должен быть >= 1")

    res = await db.execute(
        select(ProcessingJob)
        .where(ProcessingJob.status.in_(("queued", "running")))
        .order_by(ProcessingJob.created_at.asc())
    )
    stale = [j for j in res.scalars().all() if _is_stale(j, minutes)]

    preview = [
        {
            "job_id": j.id,
            "user_id": j.user_id,
            "status": j.status,
            "progress": _progress_of(j),
            "created_at": _iso_utc(j.created_at),
        }
        for j in stale
    ]

    if payload.dry_run:
        return {
            "dry_run": True,
            "older_than_minutes": minutes,
            "count": len(stale),
            "jobs": preview,
        }

    stopped = 0
    deleted = 0
    for job in stale:
        langflow = await _stop_in_langflow(job)
        if langflow.get("stopped"):
            stopped += 1
        if payload.action == "cancel":
            job.status = "failed"
            job.error = "Отменено администратором (очистка зависших)"
            job.finished_at = datetime.utcnow()
            job.progress_eta_seconds = 0
            # Пользователь загружал файл и ждал результат — честнее сказать,
            # что задача снята, чем оставить чат молча пустым.
            await save_job_history(db, job)
        else:
            await db.delete(job)
            deleted += 1

    await db.commit()

    logger.info(
        "Очистка зависших: %d задач, остановлено в Langflow %d, удалено %d (admin %s)",
        len(stale), stopped, deleted, user["username"],
    )
    return {
        "dry_run": False,
        "older_than_minutes": minutes,
        "action": payload.action,
        "count": len(stale),
        "stopped_in_langflow": stopped,
        "deleted": deleted,
        "jobs": preview,
    }


def _map_status(lf_status: str | None) -> str:
    mapping = {
        "queued": "queued",
        "pending": "queued",
        "running": "running",
        "in_progress": "running",
        "completed": "completed",
        "failed": "failed",
        "cancelled": "failed",
        "canceled": "failed",
        "timeout": "timeout",
        "timed_out": "timeout",
        "suspended": "running",
    }
    return mapping.get((lf_status or "").lower(), "running")