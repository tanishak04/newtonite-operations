from typing import Optional, List
from datetime import datetime
from pydantic import BaseModel, EmailStr, ConfigDict
from app.models import RoleEnum, WorkItemType, Priority, Status

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"

class UserLoginRequest(BaseModel):
    email: EmailStr
    password: str

class TeamRoleInfo(BaseModel):
    team_id: str
    team_name: str
    team_slug: str
    role: RoleEnum

class UserOut(BaseModel):
    id: str
    email: str
    name: str
    teams: List[TeamRoleInfo]

    model_config = ConfigDict(from_attributes=True)
