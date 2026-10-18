import asyncio
import uuid
from datetime import datetime, timedelta, timezone
import random
from sqlalchemy.future import select
from app.core.database import engine, Base, AsyncSessionLocal
from app.core.security import get_password_hash
from app.models import (
    User, Team, Membership, RoleEnum,
    WorkItem, WorkItemType, Priority, Status,
    ActivityEvent, JobOutbox, OutboxStatus
)

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

def run_migrations():
    from alembic.config import Config
    from alembic import command
    alembic_cfg = Config("alembic.ini")
    command.upgrade(alembic_cfg, "head")

async def init_db():
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, run_migrations)

async def seed_data(item_count: int = 1500):
    await init_db()
    async with AsyncSessionLocal() as session:
        # Check if already seeded
        result = await session.execute(select(User).limit(1))
        if result.scalars().first():
            print("Database already contains data, skipping seed.")
            return

        print("Seeding Users, Teams, Memberships, and Work Items...")

        # 1. Teams
        teams_data = [
            ("Core Platform", "core-platform"),
            ("Payments & Billing", "payments-billing"),
            ("Compliance & Risk", "compliance-risk"),
            ("Customer Operations", "customer-ops"),
        ]
        teams = []
        for name, slug in teams_data:
            t = Team(id=str(uuid.uuid4()), name=name, slug=slug, created_at=utc_now())
            session.add(t)
            teams.append(t)
        await session.flush()

        # 2. Users (Seeded credentials per role)
        # Format: (name, email, default_password)
        users_info = [
            ("Alice Lead", "alice@newtonite.io", "LeadPass123!"),
            ("Bob Member", "bob@newtonite.io", "MemberPass123!"),
            ("Charlie Viewer", "charlie@newtonite.io", "ViewerPass123!"),
            ("Diana Admin", "diana@newtonite.io", "AdminPass123!"),
            ("Evan Payments Lead", "evan@newtonite.io", "LeadPass123!"),
            ("Fiona Payments Member", "fiona@newtonite.io", "MemberPass123!"),
            ("George Compliance Lead", "george@newtonite.io", "LeadPass123!"),
            ("Hannah Compliance Member", "hannah@newtonite.io", "MemberPass123!"),
            ("Ian Ops Lead", "ian@newtonite.io", "LeadPass123!"),
            ("Julia Ops Member", "julia@newtonite.io", "MemberPass123!"),
        ]

        users = []
        for name, email, pwd in users_info:
            u = User(
                id=str(uuid.uuid4()),
                name=name,
                email=email,
                password_hash=get_password_hash(pwd),
                created_at=utc_now()
            )
            session.add(u)
            users.append(u)
        await session.flush()

        # 3. Memberships across teams
        # Core Platform: Alice (Lead), Bob (Member), Charlie (Viewer), Diana (Admin)
        core_team = teams[0]
        pay_team = teams[1]
        comp_team = teams[2]
        ops_team = teams[3]

        memberships = [
            # Core
            Membership(id=str(uuid.uuid4()), user_id=users[0].id, team_id=core_team.id, role=RoleEnum.LEAD),
            Membership(id=str(uuid.uuid4()), user_id=users[1].id, team_id=core_team.id, role=RoleEnum.MEMBER),
            Membership(id=str(uuid.uuid4()), user_id=users[2].id, team_id=core_team.id, role=RoleEnum.VIEWER),
            Membership(id=str(uuid.uuid4()), user_id=users[3].id, team_id=core_team.id, role=RoleEnum.ADMIN),

            # Payments
            Membership(id=str(uuid.uuid4()), user_id=users[4].id, team_id=pay_team.id, role=RoleEnum.LEAD),
            Membership(id=str(uuid.uuid4()), user_id=users[5].id, team_id=pay_team.id, role=RoleEnum.MEMBER),

            # Compliance
            Membership(id=str(uuid.uuid4()), user_id=users[6].id, team_id=comp_team.id, role=RoleEnum.LEAD),
            Membership(id=str(uuid.uuid4()), user_id=users[7].id, team_id=comp_team.id, role=RoleEnum.MEMBER),

            # Customer Ops
            Membership(id=str(uuid.uuid4()), user_id=users[8].id, team_id=ops_team.id, role=RoleEnum.LEAD),
            Membership(id=str(uuid.uuid4()), user_id=users[9].id, team_id=ops_team.id, role=RoleEnum.MEMBER),
        ]
        session.add_all(memberships)
        await session.flush()

        # 4. Work Items (realistic scale: ~1500 items to test cursor pagination and indexing)
        types = list(WorkItemType)
        priorities = list(Priority)
        statuses = list(Status)

        title_prefixes = [
            "High latency observed in", "Reconciliation mismatch on", "Customer ticket escalated:",
            "Security vulnerability scan flag in", "Stripe webhook failure:", "AML audit check failed:",
            "Database connection pool exhausted on", "SSL certificate renewal required for",
            "Urgent user account unlock request:", "Tax calculation divergence on invoice"
        ]

        work_items_batch = []
        activity_batch = []
        outbox_batch = []

        now = utc_now()

        for i in range(1, item_count + 1):
            team = teams[i % len(teams)]
            # Select possible assignees from that team or unassigned
            team_users = [u for u in users if any(m.user_id == u.id and m.team_id == team.id for m in memberships)]
            creator = random.choice(team_users)
            assignee = random.choice([None] + team_users)

            w_type = random.choice(types)
            w_prio = random.choice(priorities)
            w_status = random.choice(statuses)

            item_id = str(uuid.uuid4())
            created_at = now - timedelta(days=random.randint(0, 30), minutes=random.randint(0, 1440))
            
            wi = WorkItem(
                id=item_id,
                team_id=team.id,
                title=f"{random.choice(title_prefixes)} #{1000 + i}",
                description=f"Detailed investigation required for operations incident report #{i}. Priority evaluated under team policy.",
                type=w_type,
                priority=w_prio,
                status=w_status,
                assignee_id=assignee.id if assignee else None,
                created_by_id=creator.id,
                due_date=created_at + timedelta(days=random.randint(1, 14)),
                version=1,
                created_at=created_at,
                updated_at=created_at
            )
            work_items_batch.append(wi)

            # Audit event for creation
            ae = ActivityEvent(
                id=str(uuid.uuid4()),
                work_item_id=item_id,
                actor_id=creator.id,
                action_type="CREATED",
                old_value=None,
                new_value=w_status.value,
                comment_text="Work item created via automated pipeline / ingestion.",
                created_at=created_at
            )
            activity_batch.append(ae)

            # If assigned, record assignment event + outbox notification
            if assignee:
                ae_assign = ActivityEvent(
                    id=str(uuid.uuid4()),
                    work_item_id=item_id,
                    actor_id=creator.id,
                    action_type="CLAIMED",
                    old_value=None,
                    new_value=assignee.name,
                    comment_text=f"Assigned to {assignee.name}",
                    created_at=created_at + timedelta(seconds=10)
                )
                activity_batch.append(ae_assign)

                ob = JobOutbox(
                    id=str(uuid.uuid4()),
                    job_type="NOTIFY_ASSIGNEE",
                    payload={"work_item_id": item_id, "assignee_email": assignee.email, "title": wi.title},
                    status=OutboxStatus.COMPLETED if i > 20 else OutboxStatus.PENDING,
                    attempts=1 if i > 20 else 0,
                    created_at=created_at + timedelta(seconds=10),
                    updated_at=created_at + timedelta(seconds=10)
                )
                outbox_batch.append(ob)

        session.add_all(work_items_batch)
        session.add_all(activity_batch)
        session.add_all(outbox_batch)
        await session.commit()
        print(f"Successfully seeded {len(teams)} teams, {len(users)} users, {len(memberships)} memberships, and {len(work_items_batch)} work items!")

if __name__ == "__main__":
    asyncio.run(seed_data())
