import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from sqlalchemy.future import select
from sqlalchemy import update, and_
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import AsyncSessionLocal
from app.models import JobOutbox, OutboxStatus
from app.core.config import settings

logger = logging.getLogger("outbox_worker")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# In-memory tracking of dispatched job side-effects for strict idempotency verification
PROCESSED_DISPATCHES: set = set()

def reset_dispatches():
    PROCESSED_DISPATCHES.clear()

def compute_backoff_seconds(attempts: int, base: float = 2.0, max_backoff: float = 60.0) -> float:
    """
    Bounded exponential backoff: base ^ attempts, capped at max_backoff.
    e.g. attempt 1 -> 2s, attempt 2 -> 4s, attempt 3 -> 8s...
    """
    return min(max_backoff, base ** max(1, attempts))

def is_job_ready(job: JobOutbox, now: datetime, base_backoff: float = 2.0, max_backoff: float = 60.0) -> bool:
    """
    Determines if a pending/retryable job has cleared its backoff duration.
    Initial attempts (attempts == 0) or jobs without a recorded prior failure are immediately ready.
    """
    if job.attempts == 0 or not job.last_error:
        return True
    backoff = compute_backoff_seconds(job.attempts, base=base_backoff, max_backoff=max_backoff)
    job_updated = job.updated_at or job.created_at
    if job_updated.tzinfo is None and now.tzinfo is not None:
        job_updated = job_updated.replace(tzinfo=timezone.utc)
    elif job_updated.tzinfo is not None and now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now - job_updated).total_seconds() >= backoff

# Simulated notification dispatch / email handler
async def handle_job_payload(
    job_type: str,
    payload: Dict[str, Any],
    job_id: Optional[str] = None,
    db: Optional[AsyncSession] = None
) -> bool:
    """
    Idempotent job handler.
    Checks the persisted outbox status in the database so idempotency is durable
    across worker restarts rather than relying solely on in-memory state.
    Returns True if successfully processed, or raises an Exception on failure.
    """
    logger.info(f"Processing job [{job_type}] (id={job_id}) with payload: {payload}")
    
    # 1. Durable database-backed idempotency check: If job is already COMPLETED in DB, skip side-effect
    if job_id and db:
        stmt = select(JobOutbox.status).where(JobOutbox.id == job_id)
        res = await db.execute(stmt)
        persisted_status = res.scalar_one_or_none()
        if persisted_status == OutboxStatus.COMPLETED:
            logger.info(f"Job {job_id} is already persisted as COMPLETED in database; skipping duplicate dispatch.")
            return True

    # 2. In-memory safeguard for same-process tracking
    if job_id and job_id in PROCESSED_DISPATCHES:
        logger.info(f"Job {job_id} side-effect was already executed in current session; skipping duplicate dispatch.")
        return True

    # 3. Simulate flaky network or failure condition if specified in test payloads
    if payload.get("should_fail"):
        raise RuntimeError("Simulated external notification service failure")
        
    if job_type == "NOTIFY_ASSIGNEE":
        # In real life, dispatch to SMTP/SES/Slack webhook
        logger.info(f"Notification sent for item {payload.get('work_item_id')} to assignee {payload.get('assignee_id')}")
    else:
        logger.info(f"Generic job {job_type} executed successfully.")
        
    if job_id:
        PROCESSED_DISPATCHES.add(job_id)
    return True

async def process_outbox_batch(
    limit: int = 10,
    db: Optional[AsyncSession] = None,
    job_ids: Optional[List[str]] = None,
    current_time: Optional[datetime] = None,
    base_backoff: float = 2.0,
    max_backoff: float = 60.0,
    stale_timeout_seconds: float = 300.0
) -> int:
    """
    Pulls a batch of PENDING or retryable jobs, executes them with exponential backoff,
    and updates job status atomically using compare-and-swap claiming for multi-worker safety.
    Supports crash recovery for abandoned jobs stuck in PROCESSING longer than stale_timeout_seconds.
    Supports time injection (current_time) for deterministic, sleep-free testing.
    """
    should_close_session = False
    if db is None:
        db = AsyncSessionLocal()
        should_close_session = True

    processed_count = 0
    now = current_time or datetime.now(timezone.utc)

    try:
        # Fetch candidate jobs
        conditions = [
            JobOutbox.status.in_([OutboxStatus.PENDING, OutboxStatus.PROCESSING]),
            JobOutbox.attempts < JobOutbox.max_attempts
        ]
        if job_ids:
            conditions = [
                JobOutbox.id.in_(job_ids)
            ]

        stmt = (
            select(JobOutbox)
            .where(and_(*conditions))
            .order_by(JobOutbox.created_at.asc())
            .limit(limit)
        )
        res = await db.execute(stmt)
        jobs = res.scalars().all()

        for job in jobs:
            # Durable idempotency: If job is already COMPLETED in database, skip reprocessing
            if job.status == OutboxStatus.COMPLETED:
                logger.info(f"Job {job.id} is already COMPLETED in database. Skipping duplicate execution.")
                continue

            # If job is FAILED (exceeded attempts), do not process
            if job.status == OutboxStatus.FAILED or job.attempts >= job.max_attempts:
                logger.warning(f"Job {job.id} has reached terminal failure or max attempts. Skipping.")
                continue

            # Crash recovery check for jobs in PROCESSING state:
            # If a job is PROCESSING, only claim it if it has been abandoned longer than stale_timeout_seconds
            if job.status == OutboxStatus.PROCESSING:
                job_updated = job.updated_at or job.created_at
                if job_updated.tzinfo is None and now.tzinfo is not None:
                    job_updated = job_updated.replace(tzinfo=timezone.utc)
                elif job_updated.tzinfo is not None and now.tzinfo is None:
                    now = now.replace(tzinfo=timezone.utc)
                if (now - job_updated).total_seconds() < stale_timeout_seconds:
                    # Job is actively being processed by another worker; do not touch
                    continue
                logger.warning(f"Job {job.id} was abandoned in PROCESSING state for >{stale_timeout_seconds}s. Reclaiming for crash recovery.")

            # Bounded exponential backoff check
            if not is_job_ready(job, now, base_backoff=base_backoff, max_backoff=max_backoff):
                logger.info(f"Job {job.id} is waiting for backoff cooldown. Skipping.")
                continue

            # Multi-Worker Safety: Atomic Compare-And-Swap (CAS) Claim
            # Atomically claim the job only if status and attempts match the candidate state.
            # If two workers attempt to claim the same job concurrently, exactly one will update 1 row.
            claim_stmt = (
                update(JobOutbox)
                .where(
                    and_(
                        JobOutbox.id == job.id,
                        JobOutbox.status.in_([OutboxStatus.PENDING, OutboxStatus.PROCESSING]),
                        JobOutbox.attempts == job.attempts
                    )
                )
                .values(
                    status=OutboxStatus.PROCESSING,
                    attempts=JobOutbox.attempts + 1,
                    updated_at=now
                )
            )
            claim_res = await db.execute(claim_stmt)
            if claim_res.rowcount == 0:
                # Another concurrent worker already claimed this job; gracefully skip
                logger.info(f"Job {job.id} was already claimed by another worker. Skipping.")
                continue
            await db.commit()

            # Refresh job instance from database to load the atomically updated attempts and status
            await db.refresh(job)

            # Execute the idempotent job side effect
            try:
                await handle_job_payload(job.job_type, job.payload, job_id=job.id, db=db)
                job.status = OutboxStatus.COMPLETED
                job.last_error = None
                job.updated_at = now
                await db.commit()
                processed_count += 1
            except Exception as exc:
                job.last_error = str(exc)
                if job.attempts >= job.max_attempts:
                    job.status = OutboxStatus.FAILED
                    logger.error(f"Job {job.id} permanently failed after {job.attempts} attempts: {exc}")
                else:
                    job.status = OutboxStatus.PENDING
                    logger.warning(f"Job {job.id} failed attempt {job.attempts}/{job.max_attempts}. Will retry.")
                job.updated_at = now
                await db.commit()

    finally:
        if should_close_session:
            await db.close()

    return processed_count

async def wait_for_database_ready(max_retries: int = 30, retry_delay: float = 2.0) -> bool:
    """
    Polls database connectivity and verifies the job_outbox schema is ready.
    Safely handles Docker Compose startup timing when migrations are running in the API container.
    """
    logger.info("Verifying database connectivity and outbox schema readiness...")
    for attempt in range(1, max_retries + 1):
        try:
            async with AsyncSessionLocal() as session:
                stmt = select(JobOutbox.id).limit(1)
                await session.execute(stmt)
                logger.info("Database connection and outbox schema verified ready.")
                return True
        except Exception as exc:
            logger.warning(
                f"Database or outbox schema not ready yet (attempt {attempt}/{max_retries}): {exc}. "
                f"Retrying in {retry_delay}s..."
            )
            await asyncio.sleep(retry_delay)

    logger.error(f"Database schema was not ready after {max_retries} attempts.")
    return False

async def run_worker_loop():
    logger.info("Starting Transactional Outbox Background Worker...")
    ready = await wait_for_database_ready()
    if not ready:
        logger.error("Failed to connect to database or schema. Exiting worker.")
        return

    logger.info("Entering outbox worker polling loop...")
    while True:
        try:
            count = await process_outbox_batch(limit=20)
            if count > 0:
                logger.info(f"Processed {count} outbox jobs in batch.")
        except Exception as e:
            logger.error(f"Error in outbox worker loop: {e}")
        await asyncio.sleep(settings.OUTBOX_POLL_INTERVAL_SECONDS)

if __name__ == "__main__":
    asyncio.run(run_worker_loop())
