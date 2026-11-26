from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
)

from database import Base


class FlowPublication(Base):
    __tablename__ = "flow_publications"

    flow_id = Column(String, primary_key=True, index=True)
    is_published = Column(Boolean, default=False, nullable=False)
    published_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    supports_files = Column(Boolean, default=False, nullable=False)

    # Переопределения (если NULL — берём значения из Langflow)
    override_name = Column(String(200), nullable=True)
    override_description = Column(Text, nullable=True)

    # Иконка агента из библиотеки (icon_library.id).
    # NULL — без иконки, это значение по умолчанию.
    icon_id = Column(String, nullable=True, index=True)


class IconLibrary(Base):
    """Библиотека иконок агентов, загружаемых из админки.

    Иконки хранятся в БД: не теряются при пересборке контейнера и не требуют
    доступа к файловой системе. Отдаются по адресу ``/icons/{id}``.
    """

    __tablename__ = "icon_library"

    id = Column(String(32), primary_key=True)        # uuid4 hex
    name = Column(String(200), nullable=False)       # подпись в админке
    mime = Column(String(100), nullable=False)
    size = Column(Integer, nullable=False, default=0)
    data = Column(LargeBinary, nullable=False)
    uploaded_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class AppSetting(Base):
    """Настройки интерфейса, которые меняются из админки.

    Хранится «ключ → значение». Приоритет: значение из этой таблицы выше,
    чем переменная окружения (см. ``branding.py``).
    """

    __tablename__ = "app_settings"

    key = Column(String(64), primary_key=True)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AppAsset(Base):
    """Загруженные из админки картинки (например, эмблема сайта).

    Лежат в БД, чтобы не терялись при пересборке контейнера и не требовали
    доступа к файловой системе.
    """

    __tablename__ = "app_assets"

    key = Column(String(64), primary_key=True)      # "logo"
    mime = Column(String(100), nullable=False)
    size = Column(Integer, nullable=False, default=0)
    data = Column(LargeBinary, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Chat(Base):
    __tablename__ = "chats"

    id = Column(String, primary_key=True)                # uuid4 hex
    flow_id = Column(String, nullable=False, index=True)
    user_id = Column(String, nullable=False, index=True) # username
    title = Column(String, nullable=False, default="Новый чат")
    # Показывать ли блок «мышления модели» в этом чате (по умолчанию выключено)
    show_thinking = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    # ---------- Мягкое удаление ----------
    # Пользователь «удалил» чат: из UI и API он исчезает, но переписка
    # остаётся в БД для аудита и разбора инцидентов.
    deleted_at = Column(DateTime, nullable=True, index=True)
    deleted_by = Column(String, nullable=True)      # username того, кто удалил

    # Память разговора в Langflow (session_id == chats.id) вычищена.
    # NULL — ещё не вычищена, поллер доберёт.
    langflow_purged_at = Column(DateTime, nullable=True)
    purge_attempts = Column(Integer, default=0, nullable=False)
    purge_error = Column(Text, nullable=True)       # причина последней неудачи


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    chat_id = Column(
        String,
        ForeignKey("chats.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role = Column(String, nullable=False)   # "user" | "assistant"
    content = Column(Text, nullable=False)
    thinking = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class LLMRequestLog(Base):
    """
    Append-only журнал всех LLM-запросов всех пользователей.
    НЕ удаляется при удалении чата. Доступен только администратору.
    """
    __tablename__ = "llm_request_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, nullable=False, index=True)
    flow_id = Column(String, nullable=False, index=True)
    chat_id = Column(String, nullable=True)          # может быть NULL, если чат удалён
    chat_title = Column(String, nullable=True)        # снапшот названия на момент запроса
    question = Column(Text, nullable=False)
    answer = Column(Text, nullable=True)
    thinking = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class AgentFavorite(Base):
    __tablename__ = "agent_favorites"

    user_id = Column(String, primary_key=True, index=True)
    flow_id = Column(String, primary_key=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


# ---------- RBAC: пользователи, группы, привязки ----------

class User(Base):
    __tablename__ = "users"

    username = Column(String, primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_login = Column(DateTime, default=datetime.utcnow, nullable=False)
    is_disabled = Column(Boolean, default=False, nullable=False)
    # Выбранная тема интерфейса ("light" | "dark"), NULL — не выбирал
    theme = Column(String(10), nullable=True)


class Group(Base):
    __tablename__ = "groups"

    id = Column(String, primary_key=True)
    name = Column(String, nullable=False, unique=True)
    description = Column(Text, nullable=True)
    is_system = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class UserGroup(Base):
    __tablename__ = "user_groups"

    username = Column(
        String, ForeignKey("users.username", ondelete="CASCADE"), primary_key=True
    )
    group_id = Column(
        String, ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True
    )


class AgentGroup(Base):
    __tablename__ = "agent_groups"

    flow_id = Column(String, primary_key=True)
    group_id = Column(
        String, ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True
    )


class Integration(Base):
    """
    Универсальная интеграция: embed + API + прямой доступ.
    Заменяет EmbedIntegration.
    """
    __tablename__ = "integrations"

    id = Column(String, primary_key=True)               # публичный uuid hex
    name = Column(String, nullable=False)         
    flow_id = Column(String, nullable=False, index=True)

    # Секрет для HMAC (embed) — всегда есть
    secret = Column(String, nullable=False)

    # API-ключ — опционально. Хранится хэш + префикс для показа
    api_key_hash = Column(String, nullable=True, unique=True, index=True)
    api_key_prefix = Column(String(16), nullable=True)   # "sk-abc12..." для UI

    # Разрешённые способы аутентификации (JSON-массив строк)
    # Возможные значения: "api_key", "ldap", "kerberos", "internal"
    auth_methods = Column(Text, nullable=False, default='["api_key"]')

    # Разрешённые origins (для embed и CORS)
    allowed_origins = Column(Text, nullable=True)

    # Метаданные
    is_active = Column(Boolean, default=True, nullable=False)
    created_by = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_used_at = Column(DateTime, nullable=True)


class EmbedNonce(Base):
    """Использованные nonce — защита от replay-атак."""
    __tablename__ = "embed_nonces"

    nonce = Column(String, primary_key=True)
    integration_id = Column(String, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

class ProcessingJob(Base):
    __tablename__ = "processing_jobs"

    id = Column(String, primary_key=True)               # uuid hex
    flow_id = Column(String, nullable=False, index=True)
    user_id = Column(String, nullable=False, index=True)
    chat_id = Column(String, nullable=True, index=True)  # чат, из которого загрузили файл
    job_type = Column(String, nullable=False)           # "chat" | "ingestion"
    status = Column(String, nullable=False, default="queued", index=True)
    # queued | running | completed | failed | timeout
    langflow_job_id = Column(String, nullable=True, index=True)  # job_id от Langflow
    input_files = Column(Text, nullable=True)           # JSON: [{"name":..., "path":...}]
    input_text = Column(Text, nullable=True)            # для чата
    result = Column(Text, nullable=True)                # ответ / результат
    error = Column(Text, nullable=True)
    # Прогресс обработки: компонент Langflow присылает его по HTTP, пока
    # идёт разбор и загрузка. Без этого задача висит в "running" и
    # непонятно, жива она или нет.
    progress_stage = Column(String, nullable=True)      # parsing | chunking | embedding | upserting | done
    progress_done = Column(Integer, nullable=True)
    progress_total = Column(Integer, nullable=True)
    progress_message = Column(Text, nullable=True)
    # Оценка «сколько осталось». Храним в секундах и пересчитываем в
    # абсолютное время при отдаче: так цифра не устаревает между опросами.
    progress_eta_seconds = Column(Integer, nullable=True)
    progress_updated_at = Column(DateTime, nullable=True)
    # Результат уже перенесён в историю чата (защита от повторной записи)
    history_saved = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)