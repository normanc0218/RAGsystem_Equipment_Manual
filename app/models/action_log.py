from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String

from app.database import Base


class ActionLog(Base):
    __tablename__ = "action_logs"

    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)
    user = Column(String, nullable=False)       # Slack user_id
    action = Column(String, nullable=False)     # archive | label | keep
    email_id = Column(String, nullable=False)
    email_subject = Column(String, nullable=True)
    label = Column(String, nullable=True)
    status = Column(String, nullable=False)     # success | dry_run | failed
    undo_status = Column(String, nullable=True)  # undone | undo_failed | None
    undone_at = Column(DateTime, nullable=True)
