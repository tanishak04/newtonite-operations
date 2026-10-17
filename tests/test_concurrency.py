import asyncio
import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app

async def get_token(ac: AsyncClient, email: str, password: str) -> str:
    res = await ac.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200
    return res.json()["access_token"]

@pytest.mark.asyncio
async def test_atomic_claim_concurrency_race_condition():
    """
    Test requirement from brief:
    Two users claim the same item simultaneously; exactly one succeeds,
    second receives 409 Conflict.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        alice_token = await get_token(ac, "alice@newtonite.io", "LeadPass123!")
        bob_token = await get_token(ac, "bob@newtonite.io", "MemberPass123!")
        
        alice_hdr = {"Authorization": f"Bearer {alice_token}"}
        bob_hdr = {"Authorization": f"Bearer {bob_token}"}

        # 1. Get team id and create an UNASSIGNED work item
        me_res = await ac.get("/api/v1/auth/me", headers=alice_hdr)
        team_id = me_res.json()["teams"][0]["team_id"]

        create_res = await ac.post("/api/v1/items", headers=alice_hdr, json={
            "team_id": team_id,
            "title": "Unassigned high priority bug for concurrency test",
            "priority": "HIGH"
        })
        item_id = create_res.json()["id"]

        # 2. Fire two claim requests in parallel
        results = await asyncio.gather(
            ac.post(f"/api/v1/items/{item_id}/claim", headers=alice_hdr),
            ac.post(f"/api/v1/items/{item_id}/claim", headers=bob_hdr),
            return_exceptions=False
        )

        status_codes = [r.status_code for r in results]
        # Exactly one MUST succeed with 200, and exactly one MUST be rejected with 409 Conflict
        assert 200 in status_codes
        assert 409 in status_codes
        assert sorted(status_codes) == [200, 409]

@pytest.mark.asyncio
async def test_optimistic_locking_stale_update_rejected():
    """
    Test requirement from brief:
    Update with an old version is rejected with 409 and data is unchanged.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        token = await get_token(ac, "alice@newtonite.io", "LeadPass123!")
        headers = {"Authorization": f"Bearer {token}"}

        me_res = await ac.get("/api/v1/auth/me", headers=headers)
        team_id = me_res.json()["teams"][0]["team_id"]

        # 1. Create item (starts at version 1)
        item = (await ac.post("/api/v1/items", headers=headers, json={
            "team_id": team_id,
            "title": "Optimistic Locking Test Item",
            "priority": "LOW"
        })).json()
        item_id = item["id"]
        assert item["version"] == 1

        # 2. First update with version 1 -> SUCCEEDS and increments to version 2
        update1 = await ac.patch(f"/api/v1/items/{item_id}", headers=headers, json={
            "title": "Updated Title First Pass",
            "version": 1
        })
        assert update1.status_code == 200
        assert update1.json()["version"] == 2
        assert update1.json()["title"] == "Updated Title First Pass"

        # 3. Second update reusing stale version 1 -> MUST BE REJECTED with 409 Conflict
        stale_update = await ac.patch(f"/api/v1/items/{item_id}", headers=headers, json={
            "title": "Stale Overwrite Attempt",
            "version": 1
        })
        assert stale_update.status_code == 409
        err = stale_update.json()["detail"]
        assert "Stale edit rejected" in err["message"]
        assert err["current_version"] == 2
        assert err["submitted_version"] == 1

        # 4. Verify data was NOT overwritten
        verify_res = await ac.get(f"/api/v1/items/{item_id}", headers=headers)
        assert verify_res.json()["title"] == "Updated Title First Pass"
        assert verify_res.json()["version"] == 2
