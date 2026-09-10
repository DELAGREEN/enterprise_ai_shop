from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, String

from database import Base


class FlowPublication(Base):
    __tablename__ = "flow_publications"

    flow_id = Column(String, primary_key=True, index=True)
    is_published = Column(Boolean, default=False, nullable=False)
    published_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)