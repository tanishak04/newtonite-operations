import hashlib
import json
from typing import Optional, Dict, Any, Tuple
from datetime import datetime, timezone
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Request, Response, HTTPException, status
from fastapi.responses import JSONResponse
from app.models import IdempotencyKey

def hash_request_payload(route: str, method: str, body: Dict[str, Any]) -> str:
    serialized = json.dumps({"route": route, "method": method, "body": body}, sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

async def check_idempotency(
    db: AsyncSession,
    key: str,
    user_id: str,
    route: str,
    request_hash: str
) -> Optional[JSONResponse]:
    """
    Checks if an idempotency key exists for this user and route.
    If exists:
      - If request_hash matches, returns the cached JSONResponse.
      - If request_hash differs, raises 400 Bad Request (key reused with different parameters).
    """
    stmt = select(IdempotencyKey).where(IdempotencyKey.key == key)
    res = await db.execute(stmt)
    record = res.scalars().first()

    if record:
        if record.user_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: Idempotency key is owned by another user."
            )
        if record.request_hash != request_hash:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Idempotency Key reuse error: Key was previously used with a different request payload."
            )
        # Return cached response
        return JSONResponse(
            status_code=record.status_code,
            content=record.response_body,
            headers={"X-Idempotent-Replay": "true"}
        )
    return None

async def store_idempotency_result(
    db: AsyncSession,
    key: str,
    user_id: str,
    route: str,
    request_hash: str,
    status_code: int,
    response_body: Dict[str, Any]
):
    now = datetime.now(timezone.utc)
    record = IdempotencyKey(
        key=key,
        user_id=user_id,
        route=route,
        request_hash=request_hash,
        status_code=status_code,
        response_body=response_body,
        created_at=now
    )
    db.add(record)
