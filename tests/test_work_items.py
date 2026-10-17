import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app

async def get_token(ac: AsyncClient, email: str, password: str) -> str:
    res = await ac.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200
    return res.json()["access_token"]

@pytest.mark.asyncio
async def test_work_item_crud_and_cursor_pagination():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        token = await get_token(ac, "alice@newtonite.io", "LeadPass123!")
        headers = {"Authorization": f"Bearer {token}"}

        # 1. Fetch user to get valid team_id
        me_res = await ac.get("/api/v1/auth/me", headers=headers)
        team_id = me_res.json()["teams"][0]["team_id"]

        # 2. Create work item
        create_res = await ac.post("/api/v1/items", headers=headers, json={
            "team_id": team_id,
            "title": "Investigate Kafka consumer lag in payments",
            "description": "Consumer lag exceeded 50,000 messages on topic orders",
            "type": "INCIDENT",
            "priority": "HIGH"
        })
        assert create_res.status_code == 201
        created_item = create_res.json()
        assert created_item["title"] == "Investigate Kafka consumer lag in payments"
        assert created_item["status"] == "OPEN"
        assert created_item["version"] == 1
        item_id = created_item["id"]

        # 3. List items with cursor pagination & filtering
        list_res = await ac.get(f"/api/v1/items?team_id={team_id}&limit=10", headers=headers)
        assert list_res.status_code == 200
        data = list_res.json()
        assert len(data["items"]) <= 10
        assert data["has_more"] is True
        assert data["next_cursor"] is not None

        # 4. Detail view with activity events
        detail_res = await ac.get(f"/api/v1/items/{item_id}", headers=headers)
        assert detail_res.status_code == 200
        detail = detail_res.json()
        assert detail["id"] == item_id
        assert len(detail["activities"]) >= 1
        assert detail["activities"][0]["action_type"] == "CREATED"

@pytest.mark.asyncio
async def test_workflow_transitions_and_lead_approval():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        lead_token = await get_token(ac, "alice@newtonite.io", "LeadPass123!")
        member_token = await get_token(ac, "bob@newtonite.io", "MemberPass123!")
        
        lead_headers = {"Authorization": f"Bearer {lead_token}"}
        member_headers = {"Authorization": f"Bearer {member_token}"}

        me_res = await ac.get("/api/v1/auth/me", headers=lead_headers)
        team_id = me_res.json()["teams"][0]["team_id"]

        # 1. Create Open Item
        item = (await ac.post("/api/v1/items", headers=lead_headers, json={
            "team_id": team_id,
            "title": "Compliance quarterly report generation",
            "type": "COMPLIANCE",
            "priority": "MEDIUM"
        })).json()
        item_id = item["id"]

        # 2. Invalid jump (OPEN -> RESOLVED or CLOSED) should fail with 400
        invalid_jump = await ac.post(f"/api/v1/items/{item_id}/transition", headers=member_headers, json={
            "target_status": "RESOLVED"
        })
        assert invalid_jump.status_code == 400
        assert "Invalid state transition" in invalid_jump.json()["detail"]

        # 3. Valid jump: OPEN -> IN_PROGRESS
        t1 = await ac.post(f"/api/v1/items/{item_id}/transition", headers=member_headers, json={
            "target_status": "IN_PROGRESS"
        })
        assert t1.status_code == 200
        assert t1.json()["status"] == "IN_PROGRESS"

        # 4. Valid jump: IN_PROGRESS -> AWAITING_APPROVAL
        t2 = await ac.post(f"/api/v1/items/{item_id}/transition", headers=member_headers, json={
            "target_status": "AWAITING_APPROVAL"
        })
        assert t2.status_code == 200
        assert t2.json()["status"] == "AWAITING_APPROVAL"

        # 5. Member tries to approve (AWAITING_APPROVAL -> RESOLVED) -> MUST FAIL with 403 (Requires Lead)
        member_approve = await ac.post(f"/api/v1/items/{item_id}/transition", headers=member_headers, json={
            "target_status": "RESOLVED"
        })
        assert member_approve.status_code == 403
        assert "requires LEAD role" in member_approve.json()["detail"]

        # 6. Lead approves (AWAITING_APPROVAL -> RESOLVED) -> SUCCEEDS
        lead_approve = await ac.post(f"/api/v1/items/{item_id}/transition", headers=lead_headers, json={
            "target_status": "RESOLVED"
        })
        assert lead_approve.status_code == 200
        assert lead_approve.json()["status"] == "RESOLVED"

@pytest.mark.asyncio
async def test_get_activity_endpoint_and_delete():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        lead_token = await get_token(ac, "alice@newtonite.io", "LeadPass123!")
        member_token = await get_token(ac, "bob@newtonite.io", "MemberPass123!")
        lead_headers = {"Authorization": f"Bearer {lead_token}"}
        member_headers = {"Authorization": f"Bearer {member_token}"}

        me_res = await ac.get("/api/v1/auth/me", headers=lead_headers)
        team_id = me_res.json()["teams"][0]["team_id"]

        # 1. Create item
        item = (await ac.post("/api/v1/items", headers=lead_headers, json={
            "team_id": team_id,
            "title": "Item for testing activity endpoint and deletion",
            "type": "TASK"
        })).json()
        item_id = item["id"]

        # 2. Add comment
        await ac.post(f"/api/v1/items/{item_id}/comments", headers=member_headers, json={
            "comment": "Adding comment for activity log verification"
        })

        # 3. Dedicated GET /items/:id/activity
        act_res = await ac.get(f"/api/v1/items/{item_id}/activity", headers=member_headers)
        assert act_res.status_code == 200
        activities = act_res.json()
        assert len(activities) >= 2
        action_types = [a["action_type"] for a in activities]
        assert "CREATED" in action_types
        assert "COMMENTED" in action_types

        # 4. Member attempts DELETE -> 403 (Requires Lead)
        del_member = await ac.delete(f"/api/v1/items/{item_id}", headers=member_headers)
        assert del_member.status_code == 403

        # 5. Lead attempts DELETE -> 204
        del_lead = await ac.delete(f"/api/v1/items/{item_id}", headers=lead_headers)
        assert del_lead.status_code == 204

        # 6. Verify item is deleted
        get_res = await ac.get(f"/api/v1/items/{item_id}", headers=lead_headers)
        assert get_res.status_code == 404
