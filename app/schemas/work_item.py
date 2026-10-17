from typing import Optional, List, Dict, Any
from datetime import datetime
from pydantic import BaseModel, Field, ConfigDict
from app.models import WorkItemType, Priority, Status, RoleEnum

class UserSummary(BaseModel):
    id: str
    name: str
    email: str

    model_config = ConfigDict(from_attributes=True)

class TeamSummary(BaseModel):
    id: str
    name: str
    slug: str

    model_config = ConfigDict(from_attributes=True)

class WorkItemCreate(BaseModel):
    team_id: str
    title: str = Field(..., min_length=3, max_length=255)
    description: Optional[str] = None
    type: WorkItemType = WorkItemType.TASK
    priority: Priority = Priority.MEDIUM
    due_date: Optional[datetime] = None
    assignee_id: Optional[str] = None

class WorkItemUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=3, max_length=255)
    description: Optional[str] = None
    priority: Optional[Priority] = None
    due_date: Optional[datetime] = None
    # Concurrency control: Client MUST send version for updates
    version: int

class WorkItemTransition(BaseModel):
    target_status: Status
    comment: Optional[str] = None

class WorkItemComment(BaseModel):
    comment: str = Field(..., min_length=1)

class ActivityEventOut(BaseModel):
    id: str
    work_item_id: str
    actor_id: Optional[str] = None
    actor_name: Optional[str] = None
    action_type: str
    old_value: Optional[str] = None
    new_value: Optional[str] = None
    comment_text: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

class WorkItemOut(BaseModel):
    id: str
    team_id: str
    title: str
    description: Optional[str] = None
    type: WorkItemType
    priority: Priority
    status: Status
    assignee_id: Optional[str] = None
    assignee: Optional[UserSummary] = None
    created_by_id: str
    creator: Optional[UserSummary] = None
    due_date: Optional[datetime] = None
    version: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

class WorkItemDetailOut(WorkItemOut):
    activities: List[ActivityEventOut] = []

class PaginatedWorkItemsOut(BaseModel):
    items: List[WorkItemOut]
    next_cursor: Optional[str] = None
    has_more: bool
    total_matching: Optional[int] = None
