import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app

@pytest.mark.asyncio
async def test_auth_login_and_me():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # 1. Login with seeded Alice Lead
        login_res = await ac.post("/api/v1/auth/login", json={
            "email": "alice@newtonite.io",
            "password": "LeadPass123!"
        })
        assert login_res.status_code == 200
        token_data = login_res.json()
        assert "access_token" in token_data
        token = token_data["access_token"]

        # 2. Access /auth/me with Bearer token
        me_res = await ac.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"}
        )
        assert me_res.status_code == 200
        user_info = me_res.json()
        assert user_info["email"] == "alice@newtonite.io"
        assert len(user_info["teams"]) >= 1
        assert user_info["teams"][0]["role"] == "LEAD"

@pytest.mark.asyncio
async def test_auth_invalid_credentials():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        res = await ac.post("/api/v1/auth/login", json={
            "email": "alice@newtonite.io",
            "password": "WrongPassword!"
        })
        assert res.status_code == 401
