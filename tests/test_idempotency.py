import uuid
import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy.future import select
from sqlalchemy import func
from app.main import app
from app.core.database import AsyncSessionLocal
from app.models import WorkItem, ActivityEvent, IdempotencyKey

async def get_token(ac: AsyncClient, email: str, password: str) -> str:
    res = await ac.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200
    return res.json()["access_token"]

@pytest.mark.asyncio
async def test_idempotent_work_item_creation():
    """
    Test requirement from brief:
    Same Idempotency-Key sent twice creates exactly one item and returns the same response.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        token = await get_token(ac, "alice@newtonite.io", "LeadPass123!")
        me_res = await ac.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        team_id = me_res.json()["teams"][0]["team_id"]

        unique_key = f"idempotency-test-key-{uuid.uuid4()}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": unique_key
        }

        payload = {
            "team_id": team_id,
            "title": "Idempotent payment refund process",
            "type": "PAYMENT",
            "priority": "URGENT"
        }

        # First request
        res1 = await ac.post("/api/v1/items", headers=headers, json=payload)
        assert res1.status_code == 201
        data1 = res1.json()

        # Second request with identical key and payload (network retry / double-click)
        res2 = await ac.post("/api/v1/items", headers=headers, json=payload)
        assert res2.status_code == 201
        data2 = res2.json()

        # Responses must be identical (same item ID returned)
        assert data1["id"] == data2["id"]
        assert data1["title"] == data2["title"]
        assert res2.headers.get("x-idempotent-replay") == "true"

        # Verify in database: Only ONE row was created with this ID
        async with AsyncSessionLocal() as session:
            count = await session.scalar(
                select(func.count(WorkItem.id)).where(WorkItem.id == data1["id"])
            )
            assert count == 1

@pytest.mark.asyncio
async def test_transactional_activity_history_integrity():
    """
    Test requirement from brief:
    Every state change, claim, and comment is written in the exact same database transaction,
    so history can never disagree with the item.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        token = await get_token(ac, "alice@newtonite.io", "LeadPass123!")
        headers = {"Authorization": f"Bearer {token}"}
        me_res = await ac.get("/api/v1/auth/me", headers=headers)
        team_id = me_res.json()["teams"][0]["team_id"]

        # 1. Create item
        res = await ac.post("/api/v1/items", headers=headers, json={
            "team_id": team_id,
            "title": "Transactional audit trail verification item",
            "type": "INCIDENT",
            "priority": "HIGH"
        })
        item_id = res.json()["id"]

        # 2. Add comment
        comment_res = await ac.post(f"/api/v1/items/{item_id}/comments", headers=headers, json={
            "comment": "Initial triage complete, isolating database cluster."
        })
        assert comment_res.status_code == 200

        # 3. Transition status: OPEN -> IN_PROGRESS
        t_res = await ac.post(f"/api/v1/items/{item_id}/transition", headers=headers, json={
            "target_status": "IN_PROGRESS",
            "comment": "Started working on database isolation"
        })
        assert t_res.status_code == 200

        # 4. Check detailed activity timeline
        detail = (await ac.get(f"/api/v1/items/{item_id}", headers=headers)).json()
        activities = detail["activities"]
        action_types = [a["action_type"] for a in activities]

        assert "CREATED" in action_types
        assert "COMMENTED" in action_types
        assert "STATUS_CHANGED" in action_types

@pytest.mark.asyncio
async def test_idempotency_key_payload_mismatch_returns_400():
    """
    Test requirement:
    Idempotency key reuse with a different payload returns HTTP 400 Bad Request error.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        token = await get_token(ac, "alice@newtonite.io", "LeadPass123!")
        me_res = await ac.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        team_id = me_res.json()["teams"][0]["team_id"]

        unique_key = f"idempotency-mismatch-test-{uuid.uuid4()}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": unique_key
        }

        # 1. First request with payload A -> 201 Created
        payload_a = {
            "team_id": team_id,
            "title": "First payload for idempotency key",
            "type": "TASK",
            "priority": "LOW"
        }
        res1 = await ac.post("/api/v1/items", headers=headers, json=payload_a)
        assert res1.status_code == 201

        # 2. Second request with SAME key but DIFFERENT payload B -> 400 Bad Request
        payload_b = {
            "team_id": team_id,
            "title": "Different payload trying to reuse key",
            "type": "INCIDENT",
            "priority": "URGENT"
        }
        res2 = await ac.post("/api/v1/items", headers=headers, json=payload_b)
        assert res2.status_code == 400
        data2 = res2.json()
        assert "Idempotency Key reuse error" in data2["detail"]
        assert "previously used with a different request payload" in data2["detail"]

@pytest.mark.asyncio
async def test_concurrent_duplicate_requests_with_same_key_race():
    """
    Test requirement:
    Concurrent duplicate requests with identical Idempotency-Key and payload:
    Exactly one executes the creation, the concurrent sibling safely recovers and returns
    the cached response without duplicate records or unhandled 500 errors.
    """
    import asyncio
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        token = await get_token(ac, "alice@newtonite.io", "LeadPass123!")
        headers_base = {"Authorization": f"Bearer {token}"}
        me_res = await ac.get("/api/v1/auth/me", headers=headers_base)
        team_id = me_res.json()["teams"][0]["team_id"]

        unique_key = f"concurrent-idempotency-{uuid.uuid4()}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": unique_key
        }

        payload = {
            "team_id": team_id,
            "title": "Concurrent duplicate request item",
            "type": "INCIDENT",
            "priority": "HIGH"
        }

        # Fire two concurrent POST requests with the same key and payload
        res1, res2 = await asyncio.gather(
            ac.post("/api/v1/items", headers=headers, json=payload),
            ac.post("/api/v1/items", headers=headers, json=payload)
        )

        assert res1.status_code == 201
        assert res2.status_code == 201
        d1 = res1.json()
        d2 = res2.json()

        # Both return the exact same item ID
        assert d1["id"] == d2["id"]
        assert d1["title"] == payload["title"]

        # Verify only one database record was created
        async with AsyncSessionLocal() as session:
            count = await session.scalar(
                select(func.count(WorkItem.id)).where(WorkItem.id == d1["id"])
            )
            assert count == 1

@pytest.mark.asyncio
async def test_idempotency_key_cross_user_ownership_theft_rejected():
    """
    Test requirement:
    Idempotency key ownership: User B attempting to reuse a key created by User A
    is rejected with 403 Forbidden.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        alice_token = await get_token(ac, "alice@newtonite.io", "LeadPass123!")
        bob_token = await get_token(ac, "bob@newtonite.io", "MemberPass123!")

        alice_hdr_base = {"Authorization": f"Bearer {alice_token}"}
        me_res = await ac.get("/api/v1/auth/me", headers=alice_hdr_base)
        team_id = me_res.json()["teams"][0]["team_id"]

        shared_key = f"theft-test-key-{uuid.uuid4()}"

        # 1. Alice creates item with key
        res_alice = await ac.post("/api/v1/items", headers={
            "Authorization": f"Bearer {alice_token}",
            "Idempotency-Key": shared_key
        }, json={
            "team_id": team_id,
            "title": "Alice legitimate request",
            "type": "TASK"
        })
        assert res_alice.status_code == 201

        # 2. Bob attempts to reuse Alice's key -> MUST FAIL with 403 Forbidden
        res_bob = await ac.post("/api/v1/items", headers={
            "Authorization": f"Bearer {bob_token}",
            "Idempotency-Key": shared_key
        }, json={
            "team_id": team_id,
            "title": "Bob attempting key theft",
            "type": "TASK"
        })
        assert res_bob.status_code == 403
        assert "Idempotency key is owned by another user" in res_bob.json()["detail"]
