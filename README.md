# Newtonite — Operations Under Pressure Platform

An internal operations web application built to create, assign, track, and resolve high-stakes operational work items across teams with strict concurrency control, transactional audit logging, and team-scoped authorization.

---

## Architecture Diagram

```mermaid
flowchart TD
    Client["Browser Client / Operations UI"] -->|REST API with Idempotency-Key & Version| API["FastAPI Application"]
    API -->|Authenticate Token| Auth["Team-Scoped Authorization Middleware"]
    Auth --> Service["WorkItem Domain Service & State Machine"]
    Service -->|Atomic Transaction| DB[("PostgreSQL / SQLite Database")]

    subgraph Transaction Boundary
        DB --- WI["work_items (version, status, assignee)"]
        DB --- AE["activity_events (append-only audit trail)"]
        DB --- OB["job_outbox (transactional background jobs)"]
        DB --- IK["idempotency_keys (cached responses)"]
    end

    Worker["Async Outbox Worker Loop"] -->|Poll & Claim Jobs| OB
    Worker -->|Execute Idempotent Task| Handler["Notification & Webhook Dispatcher"]
```

---

## Prerequisites
- **Option A (Docker - Recommended):** Docker and Docker Compose
- **Option B (Local Python):** Python 3.11+ (tested on Python 3.13)

---

## One-Command Run Setup

### Using Docker Compose (Recommended)
```bash
# Clone the repository
git clone https://github.com/tanishak04/newtonite-ops-2300971530130.git
cd newtonite-ops-2300971530130

# Start Postgres, FastAPI app, and transactional Outbox Worker
docker compose up --build
```
The application will be live at: **http://localhost:8000**

To inspect background outbox worker logs in real time:
```bash
docker compose logs -f worker
```

---

### Running Locally (Without Docker)
```bash
# 1. Create and activate virtual environment
python -m venv .venv
# On Windows:
.\.venv\Scripts\activate
# On Linux/macOS:
# source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run database migrations & seed demo dataset
alembic upgrade head
python -m app.scripts.seed

# 4. Start the application
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```
Open **http://localhost:8000** in your browser.

To run the background outbox worker in a separate terminal:
```bash
python -m app.worker
```

---

## Database Migrations (Alembic)

The project uses **Alembic** configured for asynchronous SQLAlchemy. Migrations apply identically across PostgreSQL (`asyncpg`) and local SQLite (`aiosqlite`).

- Apply latest migrations: `alembic upgrade head`
- Check current revision: `alembic current`
- Verify model-to-migration consistency: `alembic check`

---

## Demo Credentials Table

| User Email | Default Password | Team | Role | Capabilities |
| :--- | :--- | :--- | :--- | :--- |
| **alice@newtonite.io** | `LeadPass123!` | Core Platform | **LEAD** | Full CRUD, Claim, Approve, Close |
| **bob@newtonite.io** | `MemberPass123!` | Core Platform | **MEMBER** | Create, Claim, Update, Request Approval |
| **charlie@newtonite.io**| `ViewerPass123!` | Core Platform | **VIEWER** | Read-only item & timeline access |
| **diana@newtonite.io**  | `AdminPass123!`  | Core Platform | **ADMIN**  | Full administrative management |
| **evan@newtonite.io**   | `LeadPass123!` | Payments & Billing | **LEAD** | Lead in Payments (Forbidden in Core) |
| **fiona@newtonite.io**  | `MemberPass123!` | Payments & Billing | **MEMBER** | Member in Payments |
| **george@newtonite.io** | `LeadPass123!` | Compliance & Risk | **LEAD** | Lead in Compliance |

> **Pro Tip:** In the web UI, use the top-right **"Switch User"** dropdown to switch between Alice, Bob, Charlie, and Evan with one click.

---

## How to Run Automated Tests

The test suite runs with **100% test isolation**. A session fixture in `tests/conftest.py` automatically provisions an ephemeral disposable test database, runs migrations and test seeds, and cleans up on teardown. The developer database is never touched or modified.

```bash
pytest -v
```

### Critical Behaviours Tested (19/19 Passing):
1. `tests/test_concurrency.py`: Parallel atomic claim race conditions (`200` vs `409 Conflict`) and stale update rejection with optimistic locking.
2. `tests/test_idempotency.py`: Replayed `Idempotency-Key` network retries returning cached responses without duplicate rows, payload mismatch rejection (`400 Bad Request`), and strict single-transaction audit history.
3. `tests/test_edge_cases.py`: Cross-team data access isolation (`403 Forbidden`), Viewer write denial, and terminal status lifecycle protection (`CLOSED`).
4. `tests/test_outbox_and_pagination.py`: Server-side cursor pagination across 1,000+ items, bounded exponential retry backoff, attempt caps with terminal failure, and duplicate processing idempotency.
5. `tests/test_work_items.py`: Workflow state machine with Lead approval constraints, dedicated activity timeline endpoint, and item deletion authorization.
6. `tests/test_auth.py`: Token authentication and team role memberships.

---

## Known Limitations
- Real-time client updates currently use refetch/polling rather than WebSockets.
- Email delivery in the background outbox worker is simulated through structured logging and an in-memory dispatch register.
- SSO / SAML integration is omitted in favor of lightweight token auth.

---

## What I Would Build Next Week
1. **WebSocket / SSE Stream:** Replace HTTP polling with real-time Server-Sent Events push for collaborative board updates.
2. **Lead Reassignment Workflow:** Add explicit manager reassignment endpoints with justification comments and audit trails.
3. **Distributed Message Queue:** Transition outbox polling to Debezium Change Data Capture (CDC) streaming into RabbitMQ/BullMQ.
4. **File Attachments:** Add presigned S3/GCS URLs for incident diagnostic logs and screenshot uploads.
5. **SSO / Enterprise Auth:** Implement Google Workspace and Okta SAML 2.0 single sign-on.

See [ENGINEERING_DECISIONS.md](ENGINEERING_DECISIONS.md) for full context on architecture, design trade-offs, and scaling strategy.
