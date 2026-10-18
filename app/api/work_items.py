import uuid
from datetime import datetime, timezone
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query, Header, status
from sqlalchemy.future import select
from sqlalchemy import func, or_, and_, update
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user, check_team_permission
from app.core.workflow import validate_transition
from app.models import (
    User, Team, Membership, RoleEnum,
    WorkItem, WorkItemType, Priority, Status,
    ActivityEvent, JobOutbox, OutboxStatus, IdempotencyKey
)
from app.schemas.work_item import (
    WorkItemCreate, WorkItemUpdate, WorkItemTransition, WorkItemComment,
    WorkItemOut, WorkItemDetailOut, PaginatedWorkItemsOut, ActivityEventOut
)

router = APIRouter(prefix="/items", tags=["work-items"])

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

from app.core.idempotency import hash_request_payload, check_idempotency, store_idempotency_result

@router.post("", response_model=WorkItemOut, status_code=status.HTTP_201_CREATED)
async def create_work_item(
    item_in: WorkItemCreate,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    # Enforce team authorization (Member or higher can create items)
    check_team_permission(current_user, item_in.team_id, min_role=RoleEnum.MEMBER)

    route = "/api/v1/items"
    req_hash = ""
    if idempotency_key:
        req_hash = hash_request_payload(route, "POST", item_in.model_dump(mode="json"))
        cached_response = await check_idempotency(db, idempotency_key, current_user.id, route, req_hash)
        if cached_response:
            return cached_response

    item_id = str(uuid.uuid4())
    now = utc_now()

    work_item = WorkItem(
        id=item_id,
        team_id=item_in.team_id,
        title=item_in.title,
        description=item_in.description,
        type=item_in.type,
        priority=item_in.priority,
        status=Status.OPEN,
        assignee_id=item_in.assignee_id,
        created_by_id=current_user.id,
        due_date=item_in.due_date,
        version=1,
        created_at=now,
        updated_at=now
    )
    db.add(work_item)

    # In the SAME database transaction: Append audit event
    audit_event = ActivityEvent(
        id=str(uuid.uuid4()),
        work_item_id=item_id,
        actor_id=current_user.id,
        action_type="CREATED",
        old_value=None,
        new_value=Status.OPEN.value,
        comment_text="Work item created",
        created_at=now
    )
    db.add(audit_event)

    # If assigned at creation, create assignment event + outbox job
    if item_in.assignee_id:
        assign_event = ActivityEvent(
            id=str(uuid.uuid4()),
            work_item_id=item_id,
            actor_id=current_user.id,
            action_type="ASSIGNED",
            old_value=None,
            new_value=item_in.assignee_id,
            comment_text="Assigned at creation",
            created_at=now
        )
        db.add(assign_event)

        outbox_job = JobOutbox(
            id=str(uuid.uuid4()),
            job_type="NOTIFY_ASSIGNEE",
            payload={"work_item_id": item_id, "assignee_id": item_in.assignee_id},
            status=OutboxStatus.PENDING,
            created_at=now,
            updated_at=now
        )
        db.add(outbox_job)

    # If assignee specified, verify assignee is an active member of this team
    if item_in.assignee_id:
        assignee_membership = await db.execute(
            select(Membership).where(
                Membership.user_id == item_in.assignee_id,
                Membership.team_id == item_in.team_id
            )
        )
        if not assignee_membership.scalars().first():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Assignee '{item_in.assignee_id}' is not a member of team '{item_in.team_id}'"
            )

    # In the SAME transaction: If Idempotency-Key provided, store key with response
    if idempotency_key:
        response_dict = {
            "id": item_id,
            "team_id": work_item.team_id,
            "title": work_item.title,
            "description": work_item.description,
            "type": work_item.type.value,
            "priority": work_item.priority.value,
            "status": work_item.status.value,
            "assignee_id": work_item.assignee_id,
            "assignee": None,
            "created_by_id": work_item.created_by_id,
            "creator": {"id": current_user.id, "name": current_user.name, "email": current_user.email},
            "due_date": work_item.due_date.isoformat() if work_item.due_date else None,
            "version": work_item.version,
            "created_at": work_item.created_at.isoformat(),
            "updated_at": work_item.updated_at.isoformat()
        }
        await store_idempotency_result(
            db=db,
            key=idempotency_key,
            user_id=current_user.id,
            route=route,
            request_hash=req_hash,
            status_code=status.HTTP_201_CREATED,
            response_body=response_dict
        )

    from fastapi.responses import JSONResponse
    creator_user_id = str(current_user.id)
    try:
        await db.commit()
    except Exception as exc:
        await db.rollback()
        # Handle concurrent race condition where two requests with same key commit concurrently
        if idempotency_key:
            stmt = select(IdempotencyKey).where(IdempotencyKey.key == idempotency_key)
            res = await db.execute(stmt)
            existing_record = res.scalars().first()
            if existing_record:
                if existing_record.user_id != creator_user_id:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Access denied: Idempotency key is owned by another user."
                    )
                if existing_record.request_hash == req_hash:
                    return JSONResponse(
                        status_code=existing_record.status_code,
                        content=existing_record.response_body,
                        headers={"X-Idempotent-Replay": "true"}
                    )
                else:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Idempotency Key reuse error: Key was previously used with a different request payload."
                    )
        raise exc

    # Re-fetch with relationships loaded
    stmt = (
        select(WorkItem)
        .where(WorkItem.id == item_id)
        .options(selectinload(WorkItem.assignee), selectinload(WorkItem.creator))
    )
    res = await db.execute(stmt)
    return res.scalars().first()

@router.get("", response_model=PaginatedWorkItemsOut)
async def list_work_items(
    team_id: Optional[str] = Query(None, description="Filter by team"),
    status: Optional[Status] = Query(None, description="Filter by status"),
    priority: Optional[Priority] = Query(None, description="Filter by priority"),
    assignee_id: Optional[str] = Query(None, description="Filter by assignee"),
    unassigned_urgent: Optional[bool] = Query(None, description="Unassigned urgent filter"),
    awaiting_approval: Optional[bool] = Query(None, description="Items awaiting lead approval"),
    assigned_to_me: Optional[bool] = Query(None, description="Items assigned to current user"),
    q: Optional[str] = Query(None, description="Full-text / search term"),
    cursor: Optional[str] = Query(None, description="Cursor for pagination (ISO timestamp or id)"),
    limit: int = Query(25, ge=1, le=100, description="Items per page"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    # User's accessible team IDs
    user_team_ids = [m.team_id for m in current_user.memberships]
    if not user_team_ids:
        return PaginatedWorkItemsOut(items=[], next_cursor=None, has_more=False, total_matching=0)

    query = select(WorkItem).options(selectinload(WorkItem.assignee), selectinload(WorkItem.creator))

    # Scope query strictly to user's authorized teams
    if team_id:
        check_team_permission(current_user, team_id, min_role=RoleEnum.VIEWER)
        query = query.where(WorkItem.team_id == team_id)
    else:
        query = query.where(WorkItem.team_id.in_(user_team_ids))

    # Apply filters
    if status:
        query = query.where(WorkItem.status == status)
    if priority:
        query = query.where(WorkItem.priority == priority)
    if assignee_id:
        query = query.where(WorkItem.assignee_id == assignee_id)
    if assigned_to_me:
        query = query.where(WorkItem.assignee_id == current_user.id)
    if unassigned_urgent:
        query = query.where(and_(WorkItem.assignee_id.is_(None), WorkItem.priority == Priority.URGENT))
    if awaiting_approval:
        query = query.where(WorkItem.status == Status.AWAITING_APPROVAL)

    # Search filter (title or description)
    if q:
        search_filter = or_(
            WorkItem.title.ilike(f"%{q}%"),
            WorkItem.description.ilike(f"%{q}%")
        )
        query = query.where(search_filter)

    # Cursor-based keyset pagination (ordered by created_at DESC, id DESC) with deterministic tie-breaking
    if cursor:
        try:
            if "|" in cursor:
                cursor_ts_str, cursor_id = cursor.split("|", 1)
                cursor_dt = datetime.fromisoformat(cursor_ts_str)
                query = query.where(
                    or_(
                        WorkItem.created_at < cursor_dt,
                        and_(WorkItem.created_at == cursor_dt, WorkItem.id < cursor_id)
                    )
                )
            else:
                cursor_dt = datetime.fromisoformat(cursor)
                query = query.where(WorkItem.created_at < cursor_dt)
        except Exception:
            pass

    query = query.order_by(WorkItem.created_at.desc(), WorkItem.id.desc()).limit(limit + 1)
    result = await db.execute(query)
    rows = result.scalars().all()

    has_more = len(rows) > limit
    items_to_return = rows[:limit]

    next_cursor = None
    if has_more and items_to_return:
        last_item = items_to_return[-1]
        next_cursor = f"{last_item.created_at.isoformat()}|{last_item.id}"

    return PaginatedWorkItemsOut(
        items=items_to_return,
        next_cursor=next_cursor,
        has_more=has_more
    )

@router.get("/{item_id}", response_model=WorkItemDetailOut)
async def get_work_item_detail(
    item_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    stmt = (
        select(WorkItem)
        .where(WorkItem.id == item_id)
        .options(
            selectinload(WorkItem.assignee),
            selectinload(WorkItem.creator),
            selectinload(WorkItem.activity_events).selectinload(ActivityEvent.actor)
        )
    )
    res = await db.execute(stmt)
    item = res.scalars().first()
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Work item not found")

    # Authorize against item's team
    check_team_permission(current_user, item.team_id, min_role=RoleEnum.VIEWER)

    activities_out = [
        ActivityEventOut(
            id=a.id,
            work_item_id=a.work_item_id,
            actor_id=a.actor_id,
            actor_name=a.actor.name if a.actor else "System",
            action_type=a.action_type,
            old_value=a.old_value,
            new_value=a.new_value,
            comment_text=a.comment_text,
            created_at=a.created_at
        )
        for a in item.activity_events
    ]

    return WorkItemDetailOut(
        id=item.id,
        team_id=item.team_id,
        title=item.title,
        description=item.description,
        type=item.type,
        priority=item.priority,
        status=item.status,
        assignee_id=item.assignee_id,
        assignee=item.assignee,
        created_by_id=item.created_by_id,
        creator=item.creator,
        due_date=item.due_date,
        version=item.version,
        created_at=item.created_at,
        updated_at=item.updated_at,
        activities=activities_out
    )

@router.post("/{item_id}/transition", response_model=WorkItemOut)
async def transition_work_item_status(
    item_id: str,
    transition_in: WorkItemTransition,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(WorkItem).where(WorkItem.id == item_id)
    res = await db.execute(stmt)
    item = res.scalars().first()
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Work item not found")

    # Retrieve user's role in this item's team
    membership = check_team_permission(current_user, item.team_id, min_role=RoleEnum.MEMBER)

    # Validate state machine and Lead role requirement for approvals
    is_valid, err_msg = validate_transition(
        current_status=item.status,
        target_status=transition_in.target_status,
        user_role=membership.role
    )
    if not is_valid:
        if "Insufficient permissions" in err_msg:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=err_msg)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=err_msg)

    old_status = item.status
    now = utc_now()

    # Update item status and bump version
    item.status = transition_in.target_status
    item.version += 1
    item.updated_at = now

    # Write audit event in SAME transaction
    audit_event = ActivityEvent(
        id=str(uuid.uuid4()),
        work_item_id=item.id,
        actor_id=current_user.id,
        action_type="STATUS_CHANGED",
        old_value=old_status.value,
        new_value=transition_in.target_status.value,
        comment_text=transition_in.comment or f"Status changed to {transition_in.target_status.value}",
        created_at=now
    )
    db.add(audit_event)

    await db.commit()

    # Return refreshed item
    stmt = (
        select(WorkItem)
        .where(WorkItem.id == item_id)
        .options(selectinload(WorkItem.assignee), selectinload(WorkItem.creator))
    )
    res = await db.execute(stmt)
    return res.scalars().first()

@router.post("/{item_id}/claim", response_model=WorkItemOut)
async def claim_work_item(
    item_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    # Verify work item exists and check user's team permissions
    stmt = select(WorkItem).where(WorkItem.id == item_id)
    res = await db.execute(stmt)
    item = res.scalars().first()
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Work item not found")

    check_team_permission(current_user, item.team_id, min_role=RoleEnum.MEMBER)

    # Closed status is terminal; claiming is disallowed
    if item.status == Status.CLOSED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot claim a closed work item. Closed status is terminal."
        )

    # CRITICAL BEHAVIOUR 1: ATOMIC CLAIM UNDER CONCURRENCY
    # Conditional atomic update: Only update if assignee_id IS NULL
    # This prevents race conditions where two users claim simultaneously.
    now = utc_now()
    claim_stmt = (
        update(WorkItem)
        .where(and_(WorkItem.id == item_id, WorkItem.assignee_id.is_(None)))
        .values(
            assignee_id=current_user.id,
            version=WorkItem.version + 1,
            updated_at=now
        )
    )
    result = await db.execute(claim_stmt)
    if result.rowcount == 0:
        # 0 rows updated means another concurrent request already claimed this item!
        # Re-fetch latest to give helpful feedback
        refreshed = (await db.execute(select(WorkItem).where(WorkItem.id == item_id).options(selectinload(WorkItem.assignee)))).scalars().first()
        assignee_name = refreshed.assignee.name if refreshed and refreshed.assignee else "another user"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Conflict: This work item has already been claimed by {assignee_name}"
        )

    # In the SAME database transaction: Append audit event and create outbox job
    audit_event = ActivityEvent(
        id=str(uuid.uuid4()),
        work_item_id=item_id,
        actor_id=current_user.id,
        action_type="CLAIMED",
        old_value=None,
        new_value=current_user.name,
        comment_text=f"Claimed by {current_user.name}",
        created_at=now
    )
    db.add(audit_event)

    outbox_job = JobOutbox(
        id=str(uuid.uuid4()),
        job_type="NOTIFY_ASSIGNEE",
        payload={"work_item_id": item_id, "assignee_id": current_user.id, "assignee_name": current_user.name},
        status=OutboxStatus.PENDING,
        created_at=now,
        updated_at=now
    )
    db.add(outbox_job)

    await db.commit()

    # Re-fetch with relationships loaded
    stmt = (
        select(WorkItem)
        .where(WorkItem.id == item_id)
        .options(selectinload(WorkItem.assignee), selectinload(WorkItem.creator))
    )
    res = await db.execute(stmt)
    return res.scalars().first()

@router.patch("/{item_id}", response_model=WorkItemOut)
async def update_work_item(
    item_id: str,
    update_in: WorkItemUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(WorkItem).where(WorkItem.id == item_id)
    res = await db.execute(stmt)
    item = res.scalars().first()
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Work item not found")

    check_team_permission(current_user, item.team_id, min_role=RoleEnum.MEMBER)

    # Closed status is terminal; updates are disallowed
    if item.status == Status.CLOSED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot modify a closed work item. Closed status is terminal."
        )

    # CRITICAL BEHAVIOUR 2: OPTIMISTIC LOCKING
    # Update only if WHERE version = update_in.version.
    # If version changed in the database, reject stale edit with 409 Conflict.
    values_to_update = {
        "version": WorkItem.version + 1,
        "updated_at": utc_now()
    }
    changes = []
    if update_in.title is not None and update_in.title != item.title:
        values_to_update["title"] = update_in.title
        changes.append(f"Title changed from '{item.title}' to '{update_in.title}'")
    if update_in.description is not None and update_in.description != item.description:
        values_to_update["description"] = update_in.description
        changes.append("Description updated")
    if update_in.priority is not None and update_in.priority != item.priority:
        values_to_update["priority"] = update_in.priority
        changes.append(f"Priority changed from {item.priority.value} to {update_in.priority.value}")
    if update_in.due_date is not None and update_in.due_date != item.due_date:
        values_to_update["due_date"] = update_in.due_date
        changes.append("Due date updated")

    now = utc_now()
    opt_stmt = (
        update(WorkItem)
        .where(and_(WorkItem.id == item_id, WorkItem.version == update_in.version))
        .values(**values_to_update)
    )
    result = await db.execute(opt_stmt)
    if result.rowcount == 0:
        # Re-fetch latest version to send back to client
        latest = (await db.execute(select(WorkItem).where(WorkItem.id == item_id))).scalars().first()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Stale edit rejected: This item was modified by another user.",
                "current_version": latest.version if latest else None,
                "submitted_version": update_in.version
            }
        )

    # Log audit event in SAME transaction
    for ch in changes:
        audit_event = ActivityEvent(
            id=str(uuid.uuid4()),
            work_item_id=item_id,
            actor_id=current_user.id,
            action_type="UPDATED",
            old_value=None,
            new_value=None,
            comment_text=ch,
            created_at=now
        )
        db.add(audit_event)

    await db.commit()

    stmt = (
        select(WorkItem)
        .where(WorkItem.id == item_id)
        .options(selectinload(WorkItem.assignee), selectinload(WorkItem.creator))
    )
    res = await db.execute(stmt)
    return res.scalars().first()

@router.post("/{item_id}/comments", response_model=ActivityEventOut)
async def add_comment(
    item_id: str,
    comment_in: WorkItemComment,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(WorkItem).where(WorkItem.id == item_id)
    res = await db.execute(stmt)
    item = res.scalars().first()
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Work item not found")

    check_team_permission(current_user, item.team_id, min_role=RoleEnum.MEMBER)

    now = utc_now()
    event = ActivityEvent(
        id=str(uuid.uuid4()),
        work_item_id=item.id,
        actor_id=current_user.id,
        action_type="COMMENTED",
        old_value=None,
        new_value=None,
        comment_text=comment_in.comment,
        created_at=now
    )
    db.add(event)
    await db.commit()

    return ActivityEventOut(
        id=event.id,
        work_item_id=event.work_item_id,
        actor_id=event.actor_id,
        actor_name=current_user.name,
        action_type=event.action_type,
        old_value=None,
        new_value=None,
        comment_text=event.comment_text,
        created_at=event.created_at
    )

@router.get("/{item_id}/activity", response_model=List[ActivityEventOut])
async def get_work_item_activity(
    item_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(WorkItem).where(WorkItem.id == item_id)
    res = await db.execute(stmt)
    item = res.scalars().first()
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Work item not found")

    check_team_permission(current_user, item.team_id, min_role=RoleEnum.VIEWER)

    events_stmt = (
        select(ActivityEvent)
        .where(ActivityEvent.work_item_id == item_id)
        .options(selectinload(ActivityEvent.actor))
        .order_by(ActivityEvent.created_at.desc())
    )
    events_res = await db.execute(events_stmt)
    events = events_res.scalars().all()

    return [
        ActivityEventOut(
            id=e.id,
            work_item_id=e.work_item_id,
            actor_id=e.actor_id,
            actor_name=e.actor.name if e.actor else "System",
            action_type=e.action_type,
            old_value=e.old_value,
            new_value=e.new_value,
            comment_text=e.comment_text,
            created_at=e.created_at
        )
        for e in events
    ]

@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_work_item(
    item_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(WorkItem).where(WorkItem.id == item_id)
    res = await db.execute(stmt)
    item = res.scalars().first()
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Work item not found")

    # Deleting work items is restricted to team LEAD or ADMIN
    check_team_permission(current_user, item.team_id, min_role=RoleEnum.LEAD)

    await db.delete(item)
    await db.commit()
    return None
