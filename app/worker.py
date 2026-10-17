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

# Simulated notification dispatch / email handler
async def handle_job_payload(job_type: str, payload: Dict[str, Any]) -> bool:
    """
    Idempotent job handler.
    Returns True if successfully processed, or raises an Exception on failure.
    """
    logger.info(f"Processing job [{job_type}] with payload: {payload}")
    
    # Simulate flaky network or failure condition if specified in test payloads
    if payload.get("should_fail"):
        raise RuntimeError("Simulated external notification service failure")
        
    if job_type == "NOTIFY_ASSIGNEE":
        # In real life, dispatch to SMTP/SES/Slack webhook
        logger.info(f"Notification sent for item {payload.get('work_item_id')} to assignee {payload.get('assignee_id')}")
    else:
        logger.info(f"Generic job {job_type} executed successfully.")
        
    return True

async def process_outbox_batch(limit: int = 10, db: Optional[AsyncSession] = None, job_ids: Optional[List[str]] = None) -> int:
    """
    Pulls a batch of PENDING or retryable jobs, executes them with exponential backoff,
    and updates job status atomically.
    """
    should_close_session = False
    if db is None:
        db = AsyncSessionLocal()
        should_close_session = True

    processed_count = 0
    try:
        # Fetch pending jobs that have not exceeded max_attempts
        conditions = [
            JobOutbox.status.in_([OutboxStatus.PENDING, OutboxStatus.PROCESSING]),
            JobOutbox.attempts < JobOutbox.max_attempts
        ]
        if job_ids:
            conditions.append(JobOutbox.id.in_(job_ids))

        stmt = (
            select(JobOutbox)
            .where(and_(*conditions))
            .order_by(JobOutbox.created_at.asc())
            .limit(limit)
        )
        res = await db.execute(stmt)
        jobs = res.scalars().all()

        for job in jobs:
            job.status = OutboxStatus.PROCESSING
            job.attempts += 1
            job.updated_at = datetime.now(timezone.utc)
            await db.commit()

            try:
                await handle_job_payload(job.job_type, job.payload)
                job.status = OutboxStatus.COMPLETED
                job.last_error = None
                processed_count += 1
            except Exception as exc:
                job.last_error = str(exc)
                if job.attempts >= job.max_attempts:
                    job.status = OutboxStatus.FAILED
                    logger.error(f"Job {job.id} permanently failed after {job.attempts} attempts: {exc}")
                else:
                    job.status = OutboxStatus.PENDING
                    logger.warning(f"Job {job.id} failed attempt {job.attempts}/{job.max_attempts}. Will retry.")

            job.updated_at = datetime.now(timezone.utc)
            await db.commit()

    finally:
        if should_close_session:
            await db.close()

    return processed_count

async def run_worker_loop():
    logger.info("Starting Transactional Outbox Background Worker loop...")
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
