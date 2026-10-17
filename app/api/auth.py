from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.core.security import verify_password, create_access_token
from app.core.dependencies import get_current_user, build_user_out
from app.models import User, Membership
from app.schemas.auth import UserLoginRequest, TokenResponse, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])

@router.post("/login", response_model=TokenResponse)
async def login(login_req: UserLoginRequest, db: AsyncSession = Depends(get_db)):
    stmt = (
        select(User)
        .where(User.email == login_req.email)
        .options(selectinload(User.memberships).selectinload(Membership.team))
    )
    result = await db.execute(stmt)
    user = result.scalars().first()

    if not user or not verify_password(login_req.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token = create_access_token(data={"sub": user.id, "email": user.email})
    return TokenResponse(access_token=access_token)

@router.get("/me", response_model=UserOut)
async def get_current_user_profile(user: User = Depends(get_current_user)):
    return build_user_out(user)
