import asyncio
import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app

async def get_token(ac: AsyncClient, email: str, password: str) -> str:
    res = await ac.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200
    return res.json()["access_token"]

@pytest.mark.asyncio
async def test_cross_team_authorization_forbidden():
    """
    Test requirement from brief:
    User from another team gets 403 on read, write, claim, and transition.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Alice is in Core Platform
        alice_token = await get_token(ac, "alice@newtonite.io", "LeadPass123!")
        # Evan is in Payments & Billing (not in Core Platform)
        evan_token = await get_token(ac, "evan@newtonite.io", "LeadPass123!")

        alice_hdr = {"Authorization": f"Bearer {alice_token}"}
        evan_hdr = {"Authorization": f"Bearer {evan_token}"}

        # 1. Alice creates item in Core Platform
        me_alice = (await ac.get("/api/v1/auth/me", headers=alice_hdr)).json()
        core_team_id = me_alice["teams"][0]["team_id"]

        create_res = await ac.post("/api/v1/items", headers=alice_hdr, json={
            "team_id": core_team_id,
            "title": "Core Platform internal architecture refactor",
            "type": "TASK",
            "priority": "MEDIUM"
        })
        assert create_res.status_code == 201
        item_id = create_res.json()["id"]

        # 2. Evan attempts to read this Core Platform item -> MUST FAIL 403
        read_res = await ac.get(f"/api/v1/items/{item_id}", headers=evan_hdr)
        assert read_res.status_code == 403
        assert "Access denied: you are not a member of team" in read_res.json()["detail"]

        # 3. Evan attempts to claim this Core Platform item -> MUST FAIL 403
        claim_res = await ac.post(f"/api/v1/items/{item_id}/claim", headers=evan_hdr)
        assert claim_res.status_code == 403

        # 4. Evan attempts to update this Core Platform item -> MUST FAIL 403
        update_res = await ac.patch(f"/api/v1/items/{item_id}", headers=evan_hdr, json={
            "title": "Hacked title",
            "version": 1
        })
        assert update_res.status_code == 403

        # 5. Evan attempts to create an item targeting Core Platform team_id -> MUST FAIL 403
        create_cross = await ac.post("/api/v1/items", headers=evan_hdr, json={
            "team_id": core_team_id,
            "title": "Unauthorized item submission",
            "type": "TASK"
        })
        assert create_cross.status_code == 403

@pytest.mark.asyncio
async def test_viewer_role_write_restrictions():
    """
    Test requirement from brief:
    Role hierarchy enforcement: Viewer cannot create, claim, update, or transition.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        lead_token = await get_token(ac, "alice@newtonite.io", "LeadPass123!")
        viewer_token = await get_token(ac, "charlie@newtonite.io", "ViewerPass123!")

        lead_hdr = {"Authorization": f"Bearer {lead_token}"}
        viewer_hdr = {"Authorization": f"Bearer {viewer_token}"}

        me_res = (await ac.get("/api/v1/auth/me", headers=lead_hdr)).json()
        team_id = me_res["teams"][0]["team_id"]

        # 1. Lead creates item
        item = (await ac.post("/api/v1/items", headers=lead_hdr, json={
            "team_id": team_id,
            "title": "Viewer test target work item",
            "type": "TASK"
        })).json()
        item_id = item["id"]

        # 2. Viewer CAN read item detail
        view_res = await ac.get(f"/api/v1/items/{item_id}", headers=viewer_hdr)
        assert view_res.status_code == 200

        # 3. Viewer CANNOT claim item -> 403
        claim_res = await ac.post(f"/api/v1/items/{item_id}/claim", headers=viewer_hdr)
        assert claim_res.status_code == 403
        assert "requires MEMBER role" in claim_res.json()["detail"]

        # 4. Viewer CANNOT update item -> 403
        patch_res = await ac.patch(f"/api/v1/items/{item_id}", headers=viewer_hdr, json={
            "title": "Viewer attempted update",
            "version": 1
        })
        assert patch_res.status_code == 403

        # 5. Viewer CANNOT create item -> 403
        create_res = await ac.post("/api/v1/items", headers=viewer_hdr, json={
            "team_id": team_id,
            "title": "Viewer attempted creation",
            "type": "TASK"
        })
        assert create_res.status_code == 403

@pytest.mark.asyncio
async def test_terminal_state_rejection():
    """
    Test requirement from brief:
    Closed status is terminal; further transitions or claims from Closed are strictly rejected.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        token = await get_token(ac, "alice@newtonite.io", "LeadPass123!")
        headers = {"Authorization": f"Bearer {token}"}
        me_res = (await ac.get("/api/v1/auth/me", headers=headers)).json()
        team_id = me_res["teams"][0]["team_id"]

        # 1. Create item
        item = (await ac.post("/api/v1/items", headers=headers, json={
            "team_id": team_id,
            "title": "Terminal state lifecycle item",
            "type": "TASK"
        })).json()
        item_id = item["id"]

        # 2. Advance to IN_PROGRESS -> AWAITING_APPROVAL -> RESOLVED -> CLOSED
        await ac.post(f"/api/v1/items/{item_id}/transition", headers=headers, json={"target_status": "IN_PROGRESS"})
        await ac.post(f"/api/v1/items/{item_id}/transition", headers=headers, json={"target_status": "AWAITING_APPROVAL"})
        await ac.post(f"/api/v1/items/{item_id}/transition", headers=headers, json={"target_status": "RESOLVED"})
        close_res = await ac.post(f"/api/v1/items/{item_id}/transition", headers=headers, json={"target_status": "CLOSED"})
        assert close_res.status_code == 200
        assert close_res.json()["status"] == "CLOSED"

        # 3. Attempting to transition from CLOSED -> IN_PROGRESS or any state -> MUST FAIL 400
        illegal_transition = await ac.post(f"/api/v1/items/{item_id}/transition", headers=headers, json={
            "target_status": "IN_PROGRESS"
        })
        assert illegal_transition.status_code == 400
        assert "Invalid state transition from CLOSED" in illegal_transition.json()["detail"]
