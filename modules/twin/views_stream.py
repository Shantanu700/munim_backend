import json

from django.http import HttpResponseNotFound, StreamingHttpResponse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.renderers import BaseRenderer, JSONRenderer

from base.exceptions import PermissionException
from base.permissions import APIAuthenticationPermission, IsMerchantPermission
from base.views import BaseAV
from modules.twin.choices import IngestJobStatus
from modules.twin.models import IngestJob
from modules.twin.pubsub import get_async_client, tenant_channel

HEARTBEAT_S = 15
TERMINAL = {IngestJobStatus.SUCCEEDED, IngestJobStatus.FAILED}


class EventStreamRenderer(BaseRenderer):
    """Advertises text/event-stream so DRF's content negotiation admits an EventSource.

    Returning a raw StreamingHttpResponse skips the renderer on the way OUT, but
    negotiation happens on the way IN: APIView.initial() picks a renderer from the
    Accept header before get() is ever called, and a browser's EventSource asks for
    exactly "text/event-stream" -- which neither JSONRenderer nor BrowsableAPIRenderer
    offers, so the request 406s. curl hides this: its default Accept is */*, which
    matches JSONRenderer, so the endpoint tests clean from a shell and fails only in a
    real browser.

    render() is never reached by the stream itself. It exists for this view's error
    Responses (401/403/404), which still have to come back as readable JSON.
    """

    media_type = "text/event-stream"
    format = "event-stream"
    charset = "utf-8"

    def render(self, data, accepted_media_type=None, renderer_context=None):
        if data is None:
            return ""
        if isinstance(data, (bytes, str)):
            return data
        return json.dumps(data)


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def _snapshot(job: IngestJob) -> dict:
    return {
        "job": str(job.uuid),
        "status": job.get_job_status_display().lower(),
        "processed": job.processed,
        "total": job.total_urls,
        "created": job.created_count,
        "updated": job.updated_count,
        "skipped": job.skipped_count,
    }


def _terminal_event(job: IngestJob) -> bytes:
    if job.job_status == IngestJobStatus.FAILED:
        return _sse("error", {"message": job.error_message})
    return _sse("done", _snapshot(job))


class IngestStreamView(BaseAV):
    """SSE progress stream for one ingestion job.

    Locked to the user who started the job (job.created_by): anyone else,
    including other users of the same tenant, gets a 403. An unauthenticated
    caller gets a 401 (via APIAuthenticationPermission, same as every other
    authenticated endpoint in this API), and a caller who owns no store at all
    gets a 403 before get() runs (via IsMerchantPermission). The created_by
    check below is the narrower rule the permission cannot express, since it
    needs the job row.

    A plain DRF APIView on purpose, not a viewset: returning a raw
    StreamingHttpResponse from get() bypasses DRF's Response/renderer
    machinery entirely (finalize_response only special-cases Response
    instances), so this gets normal session-auth handling and, importantly
    for the frontend, a real OpenAPI schema entry -- a bare django.views.View
    would be invisible to drf-spectacular's generator, which only enumerates
    APIView subclasses.
    """

    authentication = True
    permission_classes = [APIAuthenticationPermission, IsMerchantPermission]

    renderer_classes = [JSONRenderer, EventStreamRenderer]

    @extend_schema(
        description=(
            "Server-Sent Events stream of live progress for one ingestion job, "
            "open until the job finishes. Requires `EventSource(url, "
            "{withCredentials: true})` so the session cookie is sent.\n\n"
            "Named events:\n"
            "- `progress` -- `{job, status, processed, total, created, updated, skipped}`, "
            "sent immediately on connect and again whenever a counter changes.\n"
            "- `product` -- `{uuid, sku, title, price_paise, currency, image_url}`, sent "
            "whenever a product is newly ingested. Same shape as a row of `results` from "
            "`GET /ingest/products`, so a client renders either without telling them apart; "
            "`uuid` addresses the row for `GET /ingest/products/<uuid>` and its edit/delete.\n"
            "- `done` -- final `progress`-shaped payload, sent once and the stream closes.\n"
            "- `error` -- `{message}`, sent once (job failed) and the stream closes."
        ),
        parameters=[
            OpenApiParameter(
                name="job_uuid", type=OpenApiTypes.UUID, location=OpenApiParameter.PATH,
                description="The `job` uuid returned by POST /ingest/.",
            ),
        ],
        responses={
            status.HTTP_200_OK: OpenApiResponse(
                response=OpenApiTypes.STR,
                description="`text/event-stream` body -- not JSON; Swagger's Try It Out cannot render it live.",
            ),
            status.HTTP_403_FORBIDDEN: OpenApiResponse(description="This ingestion job belongs to another user."),
            status.HTTP_404_NOT_FOUND: OpenApiResponse(description="No such job."),
        },
    )
    def get(self, request, job_uuid):
        job = IngestJob.objects.filter(uuid=job_uuid).select_related("tenant").first()
        if job is None:
            return HttpResponseNotFound("no such job")
        if job.created_by_id != request.user.id:
            raise PermissionException(detail="not your ingestion job", code=status.HTTP_403_FORBIDDEN)

        return StreamingHttpResponse(
            self._events(job), content_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def _events(self, job: IngestJob):
        yield _sse("progress", _snapshot(job))
        if job.job_status in TERMINAL:
            yield _terminal_event(job)
            return

        client = get_async_client()
        pubsub = client.pubsub()
        await pubsub.subscribe(tenant_channel(job.tenant_id))
        try:
            await job.arefresh_from_db()
            if job.job_status in TERMINAL:
                yield _terminal_event(job)
                return

            while True:
                message = await pubsub.get_message(timeout=HEARTBEAT_S, ignore_subscribe_messages=True)
                if message is None:
                    yield b": heartbeat\n\n"
                    continue

                payload = json.loads(message["data"])
                if payload["type"] == "product":
                    yield _sse("product", payload)
                    continue

                await job.arefresh_from_db()
                yield _sse("progress", _snapshot(job))
                if job.job_status in TERMINAL:
                    yield _terminal_event(job)
                    return
        finally:
            await pubsub.close()
            await client.aclose()
