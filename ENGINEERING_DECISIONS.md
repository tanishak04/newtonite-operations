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

### Decision 3: Reliable Asynchronous Processing (Transactional Outbox Pattern)
- **Context:** State changes (e.g. assigning a high-priority incident) require downstream actions (notifications, webhooks) without blocking API response times or suffering from dual-write inconsistencies.
- **Options Considered:**
  1. *Direct HTTP/Queue Dispatch inside API handler*: Calls external service or queue directly before returning.
  2. *External Message Broker (Kafka / RabbitMQ / Redis)*: Pushing events to an external queue.
  3. *Transactional Outbox Table*: Storing pending jobs in `job_outbox` in the database transaction, processed by an async worker loop.
- **Choice & Implementation:** Transactional Outbox Table. When an item is claimed or created, a `job_outbox` record is inserted with `status = PENDING`. A dedicated worker (`app/worker.py`) polls and executes jobs with idempotent handlers, exponential retry logic, and an attempt cap before marking permanently `FAILED`.
- **Trade-off:** Guarantees "at-least-once" delivery without requiring external broker infrastructure (Redis/Kafka) in the core reviewer setup.

---

### Decision 4: Pagination & Scale Strategy (Cursor/Keyset vs Offset)
- **Context:** The system must comfortably handle tens of thousands of work items and high concurrent write volumes.
- **Options Considered:**
  1. *Offset Pagination (`OFFSET 1000 LIMIT 25`)*: Traditional page-based approach.
  2. *Cursor/Keyset Pagination (`WHERE (created_at, id) < (cursor_created_at, cursor_id)`)*: Keyset-based pagination.
- **Choice & Implementation:** Cursor/Keyset Pagination ordered by `(created_at DESC, id DESC)`. Composite indexes `(team_id, status)`, `(team_id, priority)`, and `(team_id, created_at)` were configured.
- **Trade-off:** Offset pagination degrades to $O(N)$ query scans on large tables and suffers from "page drift" when items are inserted in real time. Keyset pagination ensures $O(1)$ index seek performance and stable feeds.

---

### Decision 5: Team-Scoped Role-Based Access Control (Server-Enforced RBAC)
- **Context:** Work items often belong to sensitive domains (e.g., Compliance, Payments). Team members must not be able to view, claim, or resolve items outside their department.
- **Options Considered:**
  1. *Frontend-only filtering*: Hiding unauthorized buttons and lists in the UI.
  2. *Global Roles*: Admin vs User globally across all records.
  3. *Team-Scoped Granular RBAC*: Scoped permissions per `(user, team_id, role)` enforced at the API layer.
- **Choice & Implementation:** Team-Scoped Granular RBAC (`VIEWER`, `MEMBER`, `LEAD`, `ADMIN`). Every endpoint invokes `check_team_permission` before reading or mutating data. Approving items (`AWAITING_APPROVAL -> RESOLVED`) strictly requires `LEAD` role or higher.
- **Trade-off:** Requires explicit membership management, but provides ironclad multitenant security and prevents unauthorized actions even if API endpoints are called directly.

---

### What We Chose Not to Build (Deliberate Scope Boundaries)
1. **SSO / SAML / Email Verification:** A seeded user table with pre-configured passwords allows reviewers to verify all 4 role permission boundaries in seconds without signing up.
2. **WebSocket Infrastructure:** HTTP cursor polling was chosen over WebSockets to avoid maintaining sticky socket connections and stateful proxy infrastructure, while TanStack/vanilla fetch patterns provide identical user responsiveness.
3. **File Attachments & Blobs:** S3/GCS bucket integrations were omitted to maintain a 100% self-contained codebase that starts with a single command.

---

### What Would Change at 10x / 100x Scale?
1. **Read Replicas:** Route cursor queries and read-heavy dashboard views to PostgreSQL read replicas with connection pooling (e.g., PgBouncer).
2. **Table Partitioning:** Partition `activity_events` and `work_items` by `created_at` or `team_id` range to maintain small, hot B-tree index footprints.
3. **Dedicated Queue & CDC:** Transition the outbox worker from polling to PostgreSQL Change Data Capture (CDC via Debezium) feeding Kafka or BullMQ for high-throughput sub-millisecond job distribution.
