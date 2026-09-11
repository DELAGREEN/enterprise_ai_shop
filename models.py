from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
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

    # Переопределения (если NULL — берём значения из Langflow)
    override_name = Column(String(200), nullable=True)
    override_description = Column(Text, nullable=True)


class Chat(Base):
    __tablename__ = "chats"

    id = Column(String, primary_key=True)                # uuid4 hex
    flow_id = Column(String, nullable=False, index=True)
    user_id = Column(String, nullable=False, index=True) # username
    title = Column(String, nullable=False, default="Новый чат")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


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