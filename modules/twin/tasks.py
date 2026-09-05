import logging

from celery import shared_task
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.text import slugify

from base.enums import Status
from modules.core.models import MerchantCategory
from modules.twin.choices import IngestJobStatus
from modules.twin.ingest import discover, fetch, normalise_source, product_from_html
from modules.twin.models import IngestJob, Product
from modules.twin.pubsub import publish_job_status

log = logging.getLogger(__name__)

CATEGORY_SEPARATORS = (">", "»", "|")


LIVE = [IngestJobStatus.PENDING, IngestJobStatus.RUNNING]


def _is_cancelled(job: IngestJob) -> bool:
    """Has the merchant stopped this run since the last URL?

    Read straight from the database rather than from `job`, which this task has
    held in memory since it started and which its own per-URL save() would only
    ever refresh with its own writes. One indexed primary-key lookup per URL,
    next to an HTTP fetch of a product page -- not a cost worth batching away,
    and checking any less often means a merchant presses Stop and watches rows
    keep arriving.
    """
    return IngestJob.objects.filter(
        pk=job.pk, job_status=IngestJobStatus.CANCELLED,
    ).exists()


@shared_task
def run_ingest_job(ingest_job_id: int):
    # Claim the job with a conditional UPDATE instead of a plain save(), so a
    # cancellation that lands between enqueue and pickup is not overwritten by
    # this task's own RUNNING write. Scoped to the live statuses rather than to
    # PENDING alone: a job stranded in RUNNING by a worker that died is still
    # re-runnable, which a PENDING-only claim would have quietly taken away.
    claimed = IngestJob.objects.filter(pk=ingest_job_id, job_status__in=LIVE).update(
        job_status=IngestJobStatus.RUNNING, started_at=timezone.now(),
    )
    job = IngestJob.objects.select_related("tenant").get(pk=ingest_job_id)
    if not claimed:
        log.info("ingest job %s was %s before the worker started it; not running",
                 job.uuid, job.get_job_status_display().lower())
        return {"cancelled": True}

    # .update() fires no post_save, so the receiver that normally feeds the SSE
    # stream never sees this transition. Announce it by hand.
    publish_job_status(job)

    try:
        source = normalise_source(job.source)
    except ValueError as exc:
        return _fail(job, f"invalid source: {exc}")

    try:
        urls, _rp = discover(source, limit=job.limit, match=job.match)
    except Exception as exc:
        log.exception("discover failed for job %s", job.uuid)
        return _fail(job, f"discovery failed: {exc}")

    if not urls:
        return _fail(job, "no URLs discovered (check the source, match filter, and that a sitemap exists)")

    job.total_urls = len(urls)
    job.save(update_fields=["total_urls"])

    created = updated = skipped = 0
    categories = 0
    seen_categories: set[str] = set()
    for url in urls:
        if _is_cancelled(job):
            # Nothing to write: the endpoint already set CANCELLED and
            # finished_at, and the counters below were saved by the previous
            # iteration, so they are a true account of what was ingested. The
            # stream has been closed since that write -- this only stops the
            # crawl, and above all stops the SUCCEEDED write at the end of this
            # function from resurrecting a run the merchant ended.
            log.info("ingest job %s cancelled after %s/%s URLs",
                     job.uuid, job.processed, job.total_urls)
            return {"cancelled": True, "created": created, "updated": updated,
                    "skipped": skipped, "categories": categories}

        try:
            html = fetch(url)
            item = product_from_html(html, url) if html else None
            if item is None:
                skipped += 1
            else:
                _, was_created = Product.unfiltered_objects.update_or_create(
                    tenant=job.tenant, url=item.url,
                    defaults={
                        "sku": item.sku, "title": item.title,
                        "price_paise": item.price_paise, "currency": item.currency,
                        "in_stock": item.in_stock, "image_url": item.image_url,
                        "raw": item.raw, "confidence": item.confidence, "source": item.source,
                        "is_visible_to_agents": True, "fetched_at": timezone.now(),
                        "status": Status.CREATED,
                    },
                )
                created += was_created
                updated += not was_created
                categories += _upsert_categories(job.tenant, item.raw, seen_categories)
                job.last_product_url = item.url
                job.last_product_title = item.title
        except Exception as exc:
            log.warning("ingest job %s: skipping %s (%s)", job.uuid, url, exc)
            skipped += 1

        job.processed += 1
        job.created_count = created
        job.updated_count = updated
        job.skipped_count = skipped
        job.save(update_fields=[
            "processed", "created_count", "updated_count", "skipped_count",
            "last_product_url", "last_product_title",
        ])

    counts = {"created": created, "updated": updated, "skipped": skipped,
              "categories": categories}
    if not _settle(job, IngestJobStatus.SUCCEEDED):
        return {"cancelled": True, **counts}
    return counts


def _category_names(raw: dict) -> list[str]:
    """The category names on one JSON-LD Product node.

    schema.org types `category` as Text | Thing | ..., and stores publish all of
    it: a bare string, a list of them, a nested {"name": ...} Thing, and
    taxonomy paths. Only a path's leaf is kept -- a dropdown listing every level
    of every path ("Apparel", "Clothing", "Shirts") is not one a merchant can use.
    """
    value = raw.get("category")
    names: list[str] = []
    for item in (value if isinstance(value, list) else [value]):
        if isinstance(item, dict):
            item = item.get("name") or item.get("title")
        if not isinstance(item, str):
            continue
        for sep in CATEGORY_SEPARATORS:
            if sep in item:
                item = item.rsplit(sep, 1)[-1]
        name = " ".join(item.split())[:255]
        if name:
            names.append(name)
    return names


def _upsert_categories(tenant, raw: dict, seen: set[str]) -> int:
    """Record one product's categories against its store. Returns rows created.

    Matched case-insensitively: "Shirts" and "shirts" are one dropdown entry,
    not two. unfiltered_objects, like the Product upsert above, because a
    soft-deleted row still occupies the (tenant, name) unique constraint -- and
    because a category the merchant deleted should stay deleted, not reappear on
    the next crawl.
    """
    created = 0
    for name in _category_names(raw):
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        if MerchantCategory.unfiltered_objects.filter(tenant=tenant, name__iexact=name).exists():
            continue
        try:
            with transaction.atomic():
                MerchantCategory.unfiltered_objects.create(
                    tenant=tenant, name=name, key=slugify(name)[:255] or None,
                )
            created += 1
        except IntegrityError:
            log.debug("category %r already exists for tenant %s", name, tenant.pk)
    return created


def _settle(job: IngestJob, to: IngestJobStatus, **fields) -> bool:
    """Write a terminal status, unless the merchant has already ended the run.

    Conditional on the job still being live so that a cancellation landing in a
    window this task cannot poll -- during discovery, or during the fetch of
    the very last URL, after the loop's final check -- is not overwritten by
    the ending this task was going to write anyway. The database arbitrates
    between the two writers; whichever gets there first wins and the loser says
    so in its return value.

    Uses UPDATE rather than save() for that conditionality, which costs the
    post_save signal, so the announcement the SSE stream is waiting on is made
    here by hand.
    """
    settled = IngestJob.objects.filter(pk=job.pk, job_status__in=LIVE).update(
        job_status=to, finished_at=timezone.now(), **fields,
    )
    job.refresh_from_db()
    if settled:
        publish_job_status(job)
    return bool(settled)


def _fail(job: IngestJob, message: str):
    if not _settle(job, IngestJobStatus.FAILED, error_message=message):
        return {"cancelled": True}
    return {"error": message}
