# Engineering Decisions & Architecture Trade-offs

This document details the architectural decisions, trade-offs, and design rationale behind the **Newtonite Operations Under Pressure** platform.

---

### Decision 1: Concurrency Control Strategy (Atomic Claim & Optimistic Locking)
- **Context:** Operations teams handle high-stress, rapidly changing work items where multiple coordinators or team members may view, claim, or update the same item simultaneously.
- **Options Considered:**
  1. *Pessimistic Row Locking (`SELECT FOR UPDATE`)*: Locks the row until transaction commits.
  2. *Optimistic Concurrency Control (Version Column)*: Validates `version` on `UPDATE`, failing if mismatched.
  3. *Conditional Atomic Updates*: Enforces atomic database predicates like `assignee_id IS NULL`.
- **Choice & Implementation:**
  - For **Claiming**: We implemented a conditional atomic query:
    `UPDATE work_items SET assignee_id = :user_id, version = version + 1 WHERE id = :id AND assignee_id IS NULL`.
    If 0 rows are affected, it indicates an identical parallel claim succeeded; the losing caller receives an immediate `409 Conflict`.
  - For **General Updates**: We implemented optimistic locking using an integer `version` field. Clients must submit the version they read (`UPDATE ... WHERE id = :id AND version = :version`). A mismatch returns `409 Conflict` along with the latest server state so the client can reconcile.
- **Trade-off:** Optimistic locking eliminates database connection lock contention and deadlocks under scale, at the slight expense of requiring clients to handle replay/reconciliation on conflicts.

---

### Decision 2: Data Model & History Integrity (Single-Transaction Audit Trail)
- **Context:** An operations platform requires strict accountability. History can never drift from the actual state of the entity.
- **Options Considered:**
  1. *Event Sourcing*: Reconstructing state by replaying an append-only event stream.
  2. *Dual-write*: Updating the work item in the database, then writing an audit event in a secondary log or message queue.
  3. *Single-Transaction Append-Only Audit Log*: Relational entity state alongside an `activity_events` audit table committed in the exact same ACID transaction.
- **Choice & Implementation:** Single-Transaction Append-Only Audit Log. Every status transition, claim, field update, and comment is inserted into `activity_events` within the same database transaction boundary as the work item mutation.
- **Trade-off:** Avoids the operational complexity and query overhead of full event sourcing while guaranteeing 100% transactional consistency with zero risk of partial failures where an update succeeds but history is lost.

---

### Decision 3: Reliable Asynchronous Processing (Transactional Outbox Pattern & Multi-Worker Safety)
- **Context:** State changes (e.g. assigning a high-priority incident) require downstream actions (notifications, webhooks) without blocking API response times or suffering from dual-write inconsistencies.
- **Options Considered:**
  1. *Direct HTTP/Queue Dispatch inside API handler*: Calls external service or queue directly before returning.
  2. *External Message Broker (Kafka / RabbitMQ / Redis)*: Pushing events to an external queue.
  3. *Transactional Outbox Table with Bounded Backoff & CAS Claiming*: Storing pending jobs in `job_outbox` in the database transaction, processed by an async worker loop with atomic compare-and-swap claiming, exponential backoff, and crash recovery.
- **Choice & Implementation:** Transactional Outbox Table. When an item is claimed or created, a `job_outbox` record is inserted with `status = PENDING`. A dedicated worker (`app/worker.py`) polls and executes jobs with:
  - **Multi-Worker CAS Claiming:** Atomically claims jobs via `UPDATE job_outbox SET status = 'PROCESSING', attempts = attempts + 1 WHERE id = :id AND status IN ('PENDING', 'PROCESSING') AND attempts = :attempts`. If two workers poll simultaneously, exactly one succeeds; the other gracefully skips.
  - **Worker Crash Recovery:** Abandoned jobs stuck in `PROCESSING` longer than `stale_timeout_seconds` (e.g., due to worker power loss or container SIGKILL) are safely reclaimed and retried.
  - **Bounded exponential backoff:** retry delays grow as $T = \min(60\text{s}, 2^{\text{attempts}})$, preventing downstream thundering herd problems.
  - **Hard attempt caps:** jobs that fail consecutively reach a permanent terminal `FAILED` state after `max_attempts` (default 5).
  - **Side-Effect Delivery Boundary:** The outbox provides durable "at-least-once" delivery with persistent database status checking. Downstream systems must support idempotent consumer keys or deduplication tokens for end-to-end effectively exactly-once semantics.
- **Trade-off:** Guarantees durable "at-least-once" delivery and safe multi-worker execution without requiring external broker infrastructure (Redis/Kafka) in the core reviewer setup.

---

### Decision 4: Pagination & Scale Strategy (Composite Keyset Cursor with Tie-Breaking)
- **Context:** The system must comfortably handle tens of thousands of work items and high concurrent write volumes without missing records or degrading query response times.
- **Options Considered:**
  1. *Offset Pagination (`OFFSET 1000 LIMIT 25`)*: Traditional page-based approach.
  2. *Single-column Cursor (`WHERE created_at < cursor`)*: Loses records when multiple items share identical timestamps.
  3. *Composite Keyset Cursor (`WHERE (created_at < cursor_dt) OR (created_at == cursor_dt AND id < cursor_id)`)*: Deterministic keyset-based pagination.
- **Choice & Implementation:** Composite Keyset Cursor Pagination ordered by `(created_at DESC, id DESC)` with cursor encoding `timestamp|id`. Composite indexes `(team_id, status)`, `(team_id, priority)`, and `(team_id, created_at)` were configured.
- **Trade-off:** Keyset pagination ensures stable $O(1)$ index seek performance and prevents record skips or duplicates across page boundaries even when bulk ingestion produces identical creation timestamps.

---

### Decision 5: Team-Scoped Role-Based Access Control (Server-Enforced RBAC & Terminal State Immunity)
- **Context:** Work items often belong to sensitive domains (e.g., Compliance, Payments). Team members must not be able to view, claim, or resolve items outside their department.
- **Options Considered:**
  1. *Frontend-only filtering*: Hiding unauthorized buttons and lists in the UI.
  2. *Global Roles*: Admin vs User globally across all records.
  3. *Team-Scoped Granular RBAC*: Scoped permissions per `(user, team_id, role)` enforced at the API layer.
- **Choice & Implementation:** Team-Scoped Granular RBAC (`VIEWER`, `MEMBER`, `LEAD`, `ADMIN`):
  - Every endpoint invokes `check_team_permission` before reading or mutating data (`403 Forbidden` on cross-team access).
  - Approving items (`AWAITING_APPROVAL -> RESOLVED`) strictly requires `LEAD` role or higher.
  - `VIEWER` role is strictly read-only: forbidden from creating items, claiming, updating, transitioning, and posting comments.
  - Assignee validation enforces that assignees belong to the work item's team on creation (`400 Bad Request` on cross-team assignment).
  - Terminal state `CLOSED` is strictly immutable: further transitions, claims, and field edits are rejected with `400 Bad Request`.
- **Trade-off:** Requires explicit membership management, but provides ironclad multitenant security and prevents unauthorized actions even if API endpoints are called directly.

---

### Decision 6: Schema Migration Strategy (Alembic Async Migrations vs Declarative DDL)
- **Context:** Schema evolution must be repeatable, version-controlled, and consistent across both local development (SQLite `aiosqlite`) and containerized production (PostgreSQL `asyncpg`).
- **Options Considered:**
  1. *Ad-hoc Declarative DDL (`create_all`)*: Quick to start, but fails to track schema version history or support forward/backward schema evolutions in production.
  2. *Alembic Asynchronous Migrations*: Official SQLAlchemy migration framework configured with async engine support and auto-version tracking.
- **Choice & Implementation:** Alembic Asynchronous Migrations. We configured `migrations/env.py` with dynamic database URL resolution from `settings.DATABASE_URL` and bound it to `Base.metadata`. Seed scripts and container startup execute `alembic upgrade head` before ingesting data, unifying the schema path across all environments.
- **Trade-off:** Slightly more initial boilerplate than `create_all`, but guarantees production-grade schema predictability, zero schema drift, and support for automated CI migration checks (`alembic check`).

---

### Decision 7: API Idempotency & Key Ownership Scoping
- **Context:** Network retries, mobile disconnects, and double-clicking submission buttons can cause duplicate transactions and corrupted operational records.
- **Options Considered:**
  1. *Client-side button disabling only*: Unreliable under network timeouts or direct API calls.
  2. *In-memory deduplication cache*: Lost on server restarts and fails across multiple API instances.
  3. *Database-Backed Idempotency Keys with Request Hashing & Ownership Scoping*: Storing key, payload hash, status code, and response body in an ACID table.
- **Choice & Implementation:** Database-Backed Idempotency Key table (`idempotency_keys`):
  - Repeated identical requests return the cached response with `X-Idempotent-Replay: true`.
  - Reusing a key with a modified payload is rejected with `400 Bad Request`.
  - Keys are scoped to the authenticated owner; unauthorized cross-user key reuse is rejected with `403 Forbidden`.
  - Concurrent duplicate race conditions are caught at database commit, safely rolling back and returning the winning sibling's cached response.
- **Trade-off:** Requires a database write alongside business record insertion, but eliminates duplicate records with zero chance of inconsistent phantom items.

---

### What We Chose Not to Build (Deliberate Scope Boundaries)
1. **SSO / SAML / Email Verification:** A seeded user table with pre-configured passwords allows reviewers to verify all 4 role permission boundaries in seconds without signing up.
2. **WebSocket Infrastructure:** HTTP cursor polling was chosen over WebSockets to avoid maintaining sticky socket connections and stateful proxy infrastructure, while TanStack/vanilla fetch patterns provide identical user responsiveness.
3. **File Attachments & Blobs:** S3/GCS bucket integrations were omitted to maintain a 100% self-contained codebase that starts with a single command.

---

### What I Would Build Next Week
1. **Real-time Server-Sent Events (SSE):** Push outbox updates and status changes directly to active operator dashboards without client polling.
2. **Lead Reassignment & Delegation:** Add dedicated manager workflows for cross-assigning items between team members with mandatory audit remarks.
3. **CDC Outbox Dispatcher:** Connect Debezium or PostgreSQL logical replication to feed jobs directly into Kafka or Redis BullMQ for sub-millisecond async notifications.
4. **Diagnostic Attachments:** Add S3/GCS presigned upload URLs for attaching logs and incident screenshots directly to work item audit trails.
5. **Role Delegation & Multi-team Leads:** Allow managers to temporarily delegate lead approval authority during shift handovers.

---

### What Would Change at 10x / 100x Scale?
1. **Read Replicas:** Route cursor queries and read-heavy dashboard views to PostgreSQL read replicas with connection pooling (e.g., PgBouncer).
2. **Table Partitioning:** Partition `activity_events` and `work_items` by `created_at` or `team_id` range to maintain small, hot B-tree index footprints.
3. **Dedicated Queue & CDC:** Transition the outbox worker from polling to PostgreSQL Change Data Capture (CDC via Debezium) feeding Kafka or BullMQ for high-throughput sub-millisecond job distribution.
