import enum
from datetime import datetime
from typing import Optional, List
from sqlalchemy import (
    Column,
    String,
    Integer,
    Text,
    DateTime,
    ForeignKey,
    Enum,
    Index,
    JSON,
    UniqueConstraint
)
from sqlalchemy.orm import relationship
from app.core.database import Base

class RoleEnum(str, enum.Enum):
    VIEWER = "VIEWER"
    MEMBER = "MEMBER"
    LEAD = "LEAD"
    ADMIN = "ADMIN"

class WorkItemType(str, enum.Enum):
    INCIDENT = "INCIDENT"
    CUSTOMER_ISSUE = "CUSTOMER_ISSUE"
    PAYMENT = "PAYMENT"
    COMPLIANCE = "COMPLIANCE"
    TASK = "TASK"

class Priority(str, enum.Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    URGENT = "URGENT"

class Status(str, enum.Enum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"

class OutboxStatus(str, enum.Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

class User(Base):
    __tablename__ = "users"

    id = Column(String(36), primary_key=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=False)
    password_hash = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    memberships = relationship("Membership", back_populates="user", cascade="all, delete-orphan")
    assigned_items = relationship("WorkItem", foreign_keys="WorkItem.assignee_id", back_populates="assignee")
    created_items = relationship("WorkItem", foreign_keys="WorkItem.created_by_id", back_populates="creator")

class Team(Base):
    __tablename__ = "teams"

    id = Column(String(36), primary_key=True)
    name = Column(String(255), unique=True, nullable=False)
    slug = Column(String(100), unique=True, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    memberships = relationship("Membership", back_populates="team", cascade="all, delete-orphan")
    work_items = relationship("WorkItem", back_populates="team")

class Membership(Base):
    __tablename__ = "memberships"

    id = Column(String(36), primary_key=True)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    team_id = Column(String(36), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(Enum(RoleEnum), nullable=False, default=RoleEnum.MEMBER)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="memberships")
    team = relationship("Team", back_populates="memberships")

    __table_args__ = (
        UniqueConstraint("user_id", "team_id", name="uq_user_team"),
    )

class WorkItem(Base):
    __tablename__ = "work_items"

    id = Column(String(36), primary_key=True)
    team_id = Column(String(36), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    type = Column(Enum(WorkItemType), nullable=False, default=WorkItemType.TASK)
    priority = Column(Enum(Priority), nullable=False, default=Priority.MEDIUM)
    status = Column(Enum(Status), nullable=False, default=Status.OPEN)
    
    assignee_id = Column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    created_by_id = Column(String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    due_date = Column(DateTime, nullable=True)
    
    # Concurrency control: Optimistic locking
    version = Column(Integer, default=1, nullable=False)
    
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    team = relationship("Team", back_populates="work_items")
    assignee = relationship("User", foreign_keys=[assignee_id], back_populates="assigned_items")
    creator = relationship("User", foreign_keys=[created_by_id], back_populates="created_items")
    activity_events = relationship("ActivityEvent", back_populates="work_item", cascade="all, delete-orphan", order_by="ActivityEvent.created_at.desc()")

    __table_args__ = (
        # Composite indexes for high performance server-side filtering & dashboard views
        Index("ix_work_items_team_status", "team_id", "status"),
        Index("ix_work_items_team_priority", "team_id", "priority"),
        Index("ix_work_items_assignee_status", "assignee_id", "status"),
        Index("ix_work_items_team_created_at", "team_id", "created_at"),
    )

class ActivityEvent(Base):
    __tablename__ = "activity_events"

    id = Column(String(36), primary_key=True)
    work_item_id = Column(String(36), ForeignKey("work_items.id", ondelete="CASCADE"), nullable=False, index=True)
    actor_id = Column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    action_type = Column(String(50), nullable=False)  # CREATED, CLAIMED, STATUS_CHANGED, PRIORITY_CHANGED, COMMENTED, REASSIGNED
    old_value = Column(String(255), nullable=True)
    new_value = Column(String(255), nullable=True)
    comment_text = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    work_item = relationship("WorkItem", back_populates="activity_events")
    actor = relationship("User")

    __table_args__ = (
        Index("ix_activity_item_created", "work_item_id", "created_at"),
    )

class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    key = Column(String(255), primary_key=True)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    route = Column(String(255), nullable=False)
    request_hash = Column(String(64), nullable=False)
    status_code = Column(Integer, nullable=False)
    response_body = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint("key", "user_id", name="uq_idempotency_key_user"),
    )

class JobOutbox(Base):
    __tablename__ = "job_outbox"

    id = Column(String(36), primary_key=True)
    job_type = Column(String(100), nullable=False, index=True)  # e.g., NOTIFY_ASSIGNEE, AUDIT_WEBHOOK
    payload = Column(JSON, nullable=False)
    status = Column(Enum(OutboxStatus), nullable=False, default=OutboxStatus.PENDING, index=True)
    attempts = Column(Integer, default=0, nullable=False)
    max_attempts = Column(Integer, default=5, nullable=False)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("ix_job_outbox_status_attempts", "status", "attempts"),
    )
