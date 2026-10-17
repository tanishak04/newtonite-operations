from typing import Optional, List
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.core.security import decode_access_token
from app.models import User, Membership, Team, RoleEnum
from app.schemas.auth import UserOut, TeamRoleInfo

security_bearer = HTTPBearer(auto_error=False)

ROLE_HIERARCHY = {
    RoleEnum.VIEWER: 1,
    RoleEnum.MEMBER: 2,
    RoleEnum.LEAD: 3,
    RoleEnum.ADMIN: 4,
}

async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security_bearer),
    db: AsyncSession = Depends(get_db)
) -> User:
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authorization token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    payload = decode_access_token(credentials.credentials)
    if not payload or "sub" not in payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id = payload["sub"]
    stmt = (
        select(User)
        .where(User.id == user_id)
        .options(selectinload(User.memberships).selectinload(Membership.team))
    )
    result = await db.execute(stmt)
    user = result.scalars().first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User associated with token no longer exists",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user

def build_user_out(user: User) -> UserOut:
    teams_info = [
        TeamRoleInfo(
            team_id=m.team.id,
            team_name=m.team.name,
            team_slug=m.team.slug,
            role=m.role
        )
        for m in user.memberships if m.team
    ]
    return UserOut(
        id=user.id,
        email=user.email,
        name=user.name,
        teams=teams_info
    )

def check_team_permission(user: User, team_id: str, min_role: RoleEnum = RoleEnum.VIEWER) -> Membership:
    """
    Enforces server-side authorization scoped to the item's team.
    Raises 403 Forbidden if user is not in the team or lacks the required role level.
    """
    membership = next((m for m in user.memberships if m.team_id == team_id), None)
    if not membership:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied: you are not a member of team '{team_id}'"
        )
    
    if ROLE_HIERARCHY.get(membership.role, 0) < ROLE_HIERARCHY.get(min_role, 0):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Insufficient permissions: requires {min_role.value} role in team, you have {membership.role.value}"
        )
    return membership
