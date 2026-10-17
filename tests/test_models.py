import pytest
from sqlalchemy.future import select
from sqlalchemy import func
from app.core.database import AsyncSessionLocal
from app.models import User, Team, Membership, WorkItem, ActivityEvent, JobOutbox

@pytest.mark.asyncio
async def test_database_models_and_seed_data():
    async with AsyncSessionLocal() as session:
        # Check teams count
        teams_count = await session.scalar(select(func.count(Team.id)))
        assert teams_count == 4

        # Check users count
        users_count = await session.scalar(select(func.count(User.id)))
        assert users_count == 10

        # Check memberships count
        memberships_count = await session.scalar(select(func.count(Membership.id)))
        assert memberships_count == 10

        # Check work items count
        items_count = await session.scalar(select(func.count(WorkItem.id)))
        assert items_count >= 1000

        # Check activity events were created alongside
        activity_count = await session.scalar(select(func.count(ActivityEvent.id)))
        assert activity_count >= items_count

        # Check optimistic locking version initialized
        sample_item = (await session.execute(select(WorkItem).limit(1))).scalars().first()
        assert sample_item is not None
        assert sample_item.version == 1
        assert sample_item.title is not None
