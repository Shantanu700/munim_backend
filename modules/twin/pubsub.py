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


def publish_tenant_event(tenant_id: int, payload: dict) -> None:
    """Fan a progress event out to anyone streaming this tenant's ingestion.

    One channel per tenant, not per job: the duplicate-job rule in IngestAV
    guarantees at most one PENDING/RUNNING IngestJob per tenant at a time, so
    scoping by tenant is equivalent to scoping by job while it matters.
    """
    get_client().publish(tenant_channel(tenant_id), json.dumps(payload))
