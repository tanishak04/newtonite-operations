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
