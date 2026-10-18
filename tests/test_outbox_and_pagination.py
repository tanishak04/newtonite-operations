import uuid
import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy.future import select
from app.main import app
from app.core.database import AsyncSessionLocal
from app.models import JobOutbox, OutboxStatus
from app.worker import process_outbox_batch

async def get_token(ac: AsyncClient, email: str, password: str) -> str:
    res = await ac.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200
    return res.json()["access_token"]

@pytest.mark.asyncio
async def test_filtering_and_pagination_scale():
    """
    Test requirement from brief:
    Verify server-side filtering (priority, status, assigned_to_me, unassigned_urgent)
    and cursor pagination across seeded dataset.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        token = await get_token(ac, "alice@newtonite.io", "LeadPass123!")
        headers = {"Authorization": f"Bearer {token}"}

        # 1. Page 1: limit 5
        res_p1 = await ac.get("/api/v1/items?limit=5", headers=headers)
        assert res_p1.status_code == 200
        p1_data = res_p1.json()
        assert len(p1_data["items"]) == 5
        assert p1_data["has_more"] is True
        cursor = p1_data["next_cursor"]
        assert cursor is not None

        # 2. Page 2 using cursor
        res_p2 = await ac.get(f"/api/v1/items?limit=5&cursor={cursor}", headers=headers)
        assert res_p2.status_code == 200
        p2_data = res_p2.json()
        assert len(p2_data["items"]) == 5
        # Ensure no duplicate items between page 1 and page 2
        p1_ids = {item["id"] for item in p1_data["items"]}
        p2_ids = {item["id"] for item in p2_data["items"]}
        assert len(p1_ids.intersection(p2_ids)) == 0

        # 3. Filter by priority URGENT
        urgent_res = await ac.get("/api/v1/items?priority=URGENT&limit=10", headers=headers)
        assert urgent_res.status_code == 200
        for item in urgent_res.json()["items"]:
            assert item["priority"] == "URGENT"

        # 4. Filter by unassigned urgent
        unassigned_urgent_res = await ac.get("/api/v1/items?unassigned_urgent=true&limit=10", headers=headers)
        assert unassigned_urgent_res.status_code == 200
        for item in unassigned_urgent_res.json()["items"]:
            assert item["assignee_id"] is None
            assert item["priority"] == "URGENT"

@pytest.mark.asyncio
async def test_transactional_outbox_processing_and_retries():
    """
    Test requirement from brief:
    Transactional outbox: worker processes jobs, retries with backoff, caps attempts,
    and marks failed jobs.
    """
    async with AsyncSessionLocal() as session:
        # 1. Insert a normal successful job
        success_job_id = str(uuid.uuid4())
        job_success = JobOutbox(
            id=success_job_id,
            job_type="NOTIFY_ASSIGNEE",
            payload={"work_item_id": "test-item-1", "assignee_id": "user-1"},
            status=OutboxStatus.PENDING,
            attempts=0,
            max_attempts=3
        )
        session.add(job_success)

        # 2. Insert a failing job with max_attempts = 2
        fail_job_id = str(uuid.uuid4())
        job_fail = JobOutbox(
            id=fail_job_id,
            job_type="NOTIFY_ASSIGNEE",
            payload={"work_item_id": "test-item-2", "should_fail": True},
            status=OutboxStatus.PENDING,
            attempts=1,  # Already attempted once
            max_attempts=2
        )
        session.add(job_fail)
        await session.commit()

        # Run worker processing batch on these specific jobs
        await process_outbox_batch(limit=10, db=session, job_ids=[success_job_id, fail_job_id])

        # 3. Verify success job is marked COMPLETED
        res_succ = await session.execute(select(JobOutbox).where(JobOutbox.id == success_job_id))
        s_record = res_succ.scalars().first()
        assert s_record.status == OutboxStatus.COMPLETED
        assert s_record.attempts == 1

        # 4. Verify failing job hit max_attempts and is marked FAILED
        res_fail = await session.execute(select(JobOutbox).where(JobOutbox.id == fail_job_id))
        f_record = res_fail.scalars().first()
        assert f_record.status == OutboxStatus.FAILED
        assert f_record.attempts == 2
        assert "Simulated external notification service failure" in f_record.last_error

@pytest.mark.asyncio
async def test_outbox_bounded_retry_backoff_and_exhaustion():
    """
    Test requirement:
    Verify failure followed by bounded backoff retry, and final terminal failure.
    Uses time injection to verify backoff windows deterministically without arbitrary sleeps.
    """
    from datetime import datetime, timedelta, timezone

    async with AsyncSessionLocal() as session:
        job_id = str(uuid.uuid4())
        job = JobOutbox(
            id=job_id,
            job_type="NOTIFY_ASSIGNEE",
            payload={"work_item_id": "item-retry-test", "should_fail": True},
            status=OutboxStatus.PENDING,
            attempts=0,
            max_attempts=3
        )
        session.add(job)
        await session.commit()

        t0 = datetime.now(timezone.utc)

        # 1. Initial attempt at t0: Fails attempt 1 -> attempts=1, backoff required = 2^1 = 2 seconds
        count = await process_outbox_batch(db=session, job_ids=[job_id], current_time=t0)
        assert count == 0
        j1 = (await session.execute(select(JobOutbox).where(JobOutbox.id == job_id))).scalars().first()
        assert j1.status == OutboxStatus.PENDING
        assert j1.attempts == 1

        # 2. Premature retry attempt at t0 + 1 second (cooldown is 2s): MUST BE SKIPPED
        t_premature = t0 + timedelta(seconds=1)
        count_premature = await process_outbox_batch(db=session, job_ids=[job_id], current_time=t_premature)
        assert count_premature == 0
        j_premature = (await session.execute(select(JobOutbox).where(JobOutbox.id == job_id))).scalars().first()
        assert j_premature.attempts == 1  # Unchanged, skipped due to backoff

        # 3. Valid retry at t0 + 3 seconds: Fails attempt 2 -> attempts=2, backoff required = 2^2 = 4 seconds
        t_retry1 = t0 + timedelta(seconds=3)
        await process_outbox_batch(db=session, job_ids=[job_id], current_time=t_retry1)
        j2 = (await session.execute(select(JobOutbox).where(JobOutbox.id == job_id))).scalars().first()
        assert j2.status == OutboxStatus.PENDING
        assert j2.attempts == 2

        # 4. Final attempt at t0 + 10 seconds: Fails attempt 3 -> reaches max_attempts (3) -> terminal FAILED
        t_final = t0 + timedelta(seconds=10)
        await process_outbox_batch(db=session, job_ids=[job_id], current_time=t_final)
        j3 = (await session.execute(select(JobOutbox).where(JobOutbox.id == job_id))).scalars().first()
        assert j3.status == OutboxStatus.FAILED
        assert j3.attempts == 3

        # 5. Subsequent execution loop at t0 + 20s must permanently skip this exhausted job
        t_after = t0 + timedelta(seconds=20)
        await process_outbox_batch(db=session, job_ids=[job_id], current_time=t_after)
        j4 = (await session.execute(select(JobOutbox).where(JobOutbox.id == job_id))).scalars().first()
        assert j4.status == OutboxStatus.FAILED
        assert j4.attempts == 3

@pytest.mark.asyncio
async def test_outbox_duplicate_processing_idempotency():
    """
    Test requirement:
    Duplicate processing of a completed job does not re-execute or duplicate side-effects,
    relying strictly on persisted database state across worker restarts (when in-memory registers are empty).
    """
    from app.worker import PROCESSED_DISPATCHES, reset_dispatches, handle_job_payload
    reset_dispatches()

    job_id = str(uuid.uuid4())

    # Step 1: Initial job creation and execution
    async with AsyncSessionLocal() as session1:
        job = JobOutbox(
            id=job_id,
            job_type="NOTIFY_ASSIGNEE",
            payload={"work_item_id": "item-idempotent-dispatch", "assignee_id": "alice"},
            status=OutboxStatus.PENDING,
            attempts=0,
            max_attempts=3
        )
        session1.add(job)
        await session1.commit()

        # First execution -> succeeds and transitions status to COMPLETED in database
        count1 = await process_outbox_batch(db=session1, job_ids=[job_id])
        assert count1 == 1

        j1 = (await session1.execute(select(JobOutbox).where(JobOutbox.id == job_id))).scalars().first()
        assert j1.status == OutboxStatus.COMPLETED
        assert j1.attempts == 1

    # Step 2: SIMULATE WORKER RESTART
    # Wipe all in-memory tracking registers completely to simulate a fresh process restart
    reset_dispatches()
    assert len(PROCESSED_DISPATCHES) == 0

    # Step 3: Reprocess using a completely new database session and empty in-memory state
    async with AsyncSessionLocal() as session2:
        # A. Normal worker polling query: COMPLETED jobs are never selected for processing
        stmt_poll = select(JobOutbox).where(
            JobOutbox.status.in_([OutboxStatus.PENDING, OutboxStatus.PROCESSING]),
            JobOutbox.id == job_id
        )
        res_poll = await session2.execute(stmt_poll)
        assert res_poll.scalars().first() is None  # Excluded by persistent status filter

        # B. Even if explicitly requested by job_id, worker checks persisted status in DB and skips
        count2 = await process_outbox_batch(db=session2, job_ids=[job_id])
        assert count2 == 0  # 0 jobs processed

        # C. Verify database record remained unchanged
        j2 = (await session2.execute(select(JobOutbox).where(JobOutbox.id == job_id))).scalars().first()
        assert j2.status == OutboxStatus.COMPLETED
        assert j2.attempts == 1  # Attempts was NOT incremented

        # D. Direct handle_job_payload call with db session checks persisted status in DB
        res_direct = await handle_job_payload(j2.job_type, j2.payload, job_id=job_id, db=session2)
        assert res_direct is True
        # In-memory register was never populated because it exited early on database check
        assert job_id not in PROCESSED_DISPATCHES
