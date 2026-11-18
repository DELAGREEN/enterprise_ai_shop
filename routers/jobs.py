import io
import json
import logging
import uuid
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from langflow_client import LangflowClient, extract_error_text, extract_output_text
from models import Chat, ChatMessage, LLMRequestLog, ProcessingJob
from session import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

EMPTY_RESULT_HINT = (
    "Flow завершился без результата. Проверьте, что входной компонент "
    "(Chat Input) подключён к цепочке или удалён из flow."
)

# Голые сообщения httpx/qdrant-client мало о чём говорят пользователю.
_ERROR_HINTS = {
    "timed out": (
        "Таймаут при обращении к внешнему сервису (Qdrant или модель "
        "эмбеддингов). Увеличьте timeout в компоненте загрузки."
    ),
    "all connection attempts failed": (
        "Не удалось подключиться к Langflow — сервис недоступен."
    ),
}


def _friendly_error(text: str) -> str:
    """Подставляет понятное описание вместо технического «timed out»."""
    stripped = (text or "").strip()
    hint = _ERROR_HINTS.get(stripped.lower())
    if hint:
        return f"{hint} (детали: {stripped})"
    return stripped


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

    return changed


async def save_job_history(db: AsyncSession, job: ProcessingJob) -> bool:
    """Переносит результат файловой задачи в историю чата.

    Файлы обрабатываются в фоне, поэтому ответ пишется в ``chat_messages``
    не сразу, а когда задача дошла до финального статуса. ``history_saved``
    защищает от повторной записи (поллер и опрос из UI могут сработать
    почти одновременно).

    Возвращает True, если что-то записали и нужен commit.
    """
    if not job.chat_id or job.history_saved:
        return False
    if job.status not in ("completed", "failed", "timeout"):
        return False

    chat = await db.get(Chat, job.chat_id)
    job.history_saved = True
    if chat is None:
        logger.warning("Чат %s для job %s не найден", job.chat_id, job.id)
        return True

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
    files: list[UploadFile] = File(...),
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not user:
        raise HTTPException(401, "Not authenticated")

    # Чат, из которого отправили файлы (None — служебная страница ingestion).
    # Проверяем владельца, иначе можно было бы писать в чужую историю.
    chat: Chat | None = None
    if session_id:
        chat_res = await db.execute(
            select(Chat).where(
                Chat.id == session_id,
                Chat.flow_id == flow_id,
                Chat.user_id == user["username"],
            )
        )
        chat = chat_res.scalar_one_or_none()
        if chat is None:
            raise HTTPException(status_code=404, detail="Чат не найден")

    client = LangflowClient()

    # 1. Загружаем файлы в хранилище flow
    uploaded = []
    for f in files:
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
    input_value = (message or "").strip() or (
        f"Проанализируй файлы: {', '.join(up['name'] for up in uploaded)}"
    )

    inputs = {
        "input_value": input_value,
        "file_paths": [up["path"] for up in uploaded],
    }

    # 3. Запускаем в фоне
    resp = await client.run_flow_background(flow_id, inputs, session_id=session_id)

    if resp.get("error"):
        logger.error("Langflow error: %s", resp.get("detail"))
        raise HTTPException(
            status_code=resp.get("status_code") or 502,
            detail=resp.get("detail") or resp.get("message"),
        )

    langflow_job_id = resp.get("job_id")
    if not langflow_job_id:
        logger.error("Langflow не вернул job_id: %s", resp)
        raise HTTPException(status_code=502, detail="Langflow не вернул job_id")

    # 4. Сохраняем задачу в БД и сразу — сообщение пользователя в историю чата
    job = await _create_job(
        db, user["username"], flow_id, "ingestion",
        input_files=uploaded,
        input_text=input_value,
        chat_id=chat.id if chat else None,
    )
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
        "created_at": job.created_at.isoformat(),
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


@router.get("/jobs")
async def list_jobs(
    flow_id: str | None = None,
    limit: int = 50,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin only")

    q = select(ProcessingJob).order_by(ProcessingJob.created_at.desc()).limit(limit)
    if flow_id:
        q = q.where(ProcessingJob.flow_id == flow_id)
    res = await db.execute(q)
    jobs = list(res.scalars().all())

    return [
        {
            "job_id": j.id,
            "flow_id": j.flow_id,
            "user_id": j.user_id,
            "status": j.status,
            "created_at": j.created_at.isoformat(),
            "finished_at": j.finished_at.isoformat() if j.finished_at else None,
        }
        for j in jobs
    ]


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