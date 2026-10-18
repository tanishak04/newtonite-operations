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

@pytest.mark.asyncio
async def test_concurrent_multi_worker_claim_safety():
    """
    Test requirement:
    Jobs are claimed safely when multiple worker instances operate concurrently.
    Two workers polling the same pending job in parallel: exactly one worker succeeds
    in claiming and executing, preventing duplicate dispatches.
    """
    import asyncio
    job_id = str(uuid.uuid4())

    async with AsyncSessionLocal() as setup_session:
        job = JobOutbox(
            id=job_id,
            job_type="NOTIFY_ASSIGNEE",
            payload={"work_item_id": "item-concurrency-outbox", "assignee_id": "bob"},
            status=OutboxStatus.PENDING,
            attempts=0,
            max_attempts=3
        )
        setup_session.add(job)
        await setup_session.commit()

    # Simulate two independent worker instances polling the same job concurrently
    async def run_worker_instance():
        async with AsyncSessionLocal() as session:
            return await process_outbox_batch(limit=5, db=session, job_ids=[job_id])

    results = await asyncio.gather(run_worker_instance(), run_worker_instance())

    # Exactly one worker processed the job (sum of processed counts is 1)
    assert sum(results) == 1
    assert 1 in results
    assert 0 in results

    # Verify final state in database
    async with AsyncSessionLocal() as verify_session:
        j = (await verify_session.execute(select(JobOutbox).where(JobOutbox.id == job_id))).scalars().first()
        assert j.status == OutboxStatus.COMPLETED
        assert j.attempts == 1

@pytest.mark.asyncio
async def test_outbox_worker_crash_recovery_abandoned_processing():
    """
    Test requirement:
    Crash recovery: A job left in PROCESSING state after a worker crash / restart
    is safely reclaimed and completed after exceeding the stale processing timeout.
    """
    from datetime import datetime, timedelta, timezone

    job_id = str(uuid.uuid4())
    t_past = datetime.now(timezone.utc) - timedelta(seconds=600)

    # Insert a job stuck in PROCESSING from 600s ago (simulating a dead crashed worker)
    async with AsyncSessionLocal() as setup_session:
        job = JobOutbox(
            id=job_id,
            job_type="NOTIFY_ASSIGNEE",
            payload={"work_item_id": "item-crashed-recovery", "assignee_id": "charlie"},
            status=OutboxStatus.PROCESSING,
            attempts=1,
            max_attempts=3,
            created_at=t_past,
            updated_at=t_past
        )
        setup_session.add(job)
        await setup_session.commit()

    # Worker runs with stale_timeout_seconds=300 (job has been abandoned for 600s)
    async with AsyncSessionLocal() as worker_session:
        count = await process_outbox_batch(limit=5, db=worker_session, job_ids=[job_id], stale_timeout_seconds=300.0)
        assert count == 1

        # Verify the abandoned job was recovered and completed
        j = (await worker_session.execute(select(JobOutbox).where(JobOutbox.id == job_id))).scalars().first()
        assert j.status == OutboxStatus.COMPLETED
        assert j.attempts == 2

@pytest.mark.asyncio
async def test_cursor_pagination_deterministic_tie_breaking():
    """
    Test requirement:
    Cursor pagination has deterministic ordering and handles ties correctly.
    Multiple work items with identical created_at timestamps are paginated
    using composite (created_at, id) keyset cursor without skipping or duplicating items.
    """
    from datetime import datetime, timezone
    from app.models import WorkItem, WorkItemType, Priority, Status

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        token = await get_token(ac, "alice@newtonite.io", "LeadPass123!")
        headers = {"Authorization": f"Bearer {token}"}
        me_res = await ac.get("/api/v1/auth/me", headers=headers)
        team_id = me_res.json()["teams"][0]["team_id"]
        user_id = me_res.json()["id"]

        # Insert 3 work items with the EXACT SAME created_at timestamp
        identical_ts = datetime(2025, 6, 15, 10, 30, 0, tzinfo=timezone.utc)
        test_ids = [str(uuid.uuid4()) for _ in range(3)]
        test_ids.sort(reverse=True)  # Sort descending so we know expected order

        async with AsyncSessionLocal() as db:
            for idx, item_id in enumerate(test_ids):
                item = WorkItem(
                    id=item_id,
                    team_id=team_id,
                    title=f"Tie-breaking test item {idx}",
                    type=WorkItemType.TASK,
                    priority=Priority.MEDIUM,
                    status=Status.OPEN,
                    created_by_id=user_id,
                    created_at=identical_ts,
                    updated_at=identical_ts,
                    version=1
                )
                db.add(item)
            await db.commit()

        # Page 1 with limit=1
        res1 = await ac.get(f"/api/v1/items?team_id={team_id}&limit=1&status=OPEN", headers=headers)
        assert res1.status_code == 200
        data1 = res1.json()
        assert len(data1["items"]) == 1
        first_cursor = data1["next_cursor"]
        assert first_cursor is not None
        assert "|" in first_cursor

        # Page 2 with limit=1 using composite cursor
        res2 = await ac.get(f"/api/v1/items?team_id={team_id}&limit=1&status=OPEN&cursor={first_cursor}", headers=headers)
        assert res2.status_code == 200
        data2 = res2.json()
        assert len(data2["items"]) == 1
        assert data2["items"][0]["id"] != data1["items"][0]["id"]
