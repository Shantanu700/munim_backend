import json

import redis
import redis.asyncio as redis_asyncio
from django.conf import settings


def get_client() -> redis.Redis:
    return redis.Redis.from_url(settings.REDIS_URL)


def get_async_client() -> redis_asyncio.Redis:
    return redis_asyncio.Redis.from_url(settings.REDIS_URL)


def tenant_channel(tenant_id: int) -> str:
    return f"ingest:tenant:{tenant_id}"


def publish_job_status(job) -> None:
    """Announce one ingestion job's current state to anyone streaming it.

    Lives here rather than only inside the post_save receiver because the two
    writes that end a job outside the worker -- cancellation, above all -- go
    through QuerySet.update(), which fires no signal. An open EventSource is
    blocked on exactly this publish to learn the run is over, so the caller
    that bypasses the signal has to make the announcement itself, in the shape
    the receiver would have used.
    """
    publish_tenant_event(job.tenant_id, {
        "type": "status",
        "status": job.get_job_status_display().lower(),
        "processed": job.processed,
        "total": job.total_urls,
        "created": job.created_count,
        "updated": job.updated_count,
        "skipped": job.skipped_count,
        "error": job.error_message,
    })


def publish_tenant_event(tenant_id: int, payload: dict) -> None:
    """Fan a progress event out to anyone streaming this tenant's ingestion.

    One channel per tenant, not per job: the duplicate-job rule in IngestAV
    guarantees at most one PENDING/RUNNING IngestJob per tenant at a time, so
    scoping by tenant is equivalent to scoping by job while it matters.
    """
    get_client().publish(tenant_channel(tenant_id), json.dumps(payload))
