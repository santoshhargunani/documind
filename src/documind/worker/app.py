"""
app.py  --  Cloud Run worker: turns Pub/Sub messages into ingestion calls.

PLACE AT: src/documind/worker/app.py
ALSO CREATE an empty file: src/documind/worker/__init__.py
RUN (inside the container):  uvicorn documind.worker.app:app --host 0.0.0.0 --port 8080

HOW IT IS USED
  GCS upload/delete  ->  Pub/Sub topic  ->  PUSH subscription  ->  POST /pubsub/push (this file)

  Pub/Sub "push" delivers each message as an HTTPS POST. We answer with an HTTP
  status that tells Pub/Sub what to do next:
      2xx  = acknowledged, never redeliver
      other (e.g. 503, 500) = NOT acknowledged -> redeliver with backoff, and
                              after N failed deliveries move it to the dead-letter topic

AUTHENTICATION
  The Cloud Run service is deployed as PRIVATE (no public invoker). The push
  subscription attaches a Google-signed OIDC token for a dedicated service
  account that holds roles/run.invoker, and Cloud Run rejects every other caller
  BEFORE this code runs. So no auth code is needed here.

DEPLOY NOTES (for the Terraform step)
  * Cloud Run's default request timeout is 300s; set it to 600s so a slow PDF
    can finish inside the Pub/Sub ack deadline.
  * Cap max-instances and use concurrency 1-2 to protect Vertex AI / Document AI quota.
"""

# structlog: structured logging consistent with the rest of the project.
import structlog

# FastAPI: the web framework. Response lets us set the HTTP status code.
from fastapi import FastAPI, Response

# pydantic: declares the exact shape of the Pub/Sub request body and validates it.
from pydantic import BaseModel, Field

# The reusable unit of work from the ingestion package.
from documind.ingestion.pipeline import ingest_object

logger = structlog.get_logger()

# The web application object that uvicorn serves.
app = FastAPI(title="DocuMind ingestion worker")

# GCS notification event types we act on. Anything else (metadata updates,
# archive events) is acknowledged and ignored.
_HANDLED_EVENTS = {"OBJECT_FINALIZE", "OBJECT_DELETE"}


class PubSubMessage(BaseModel):
    """The `message` part of a Pub/Sub push body."""
    # base64 of the object's JSON metadata; we don't need it, the attributes suffice.
    data: str | None = None
    # GCS puts eventType, bucketId, objectId, objectGeneration, ... in here.
    attributes: dict[str, str] = Field(default_factory=dict)
    # Unique id Pub/Sub assigned; useful for correlating log lines.
    messageId: str | None = None


class PubSubEnvelope(BaseModel):
    """The full JSON body Pub/Sub POSTs to us."""
    message: PubSubMessage
    subscription: str | None = None
    # Present when the subscription has a dead-letter policy: 1 on first delivery, 2 on first retry...
    deliveryAttempt: int | None = None


@app.get("/healthz")
def healthz() -> dict:
    """Liveness probe so you (or a load balancer) can check the service is up."""
    return {"status": "ok"}


@app.post("/pubsub/push")
def handle_push(envelope: PubSubEnvelope, response: Response) -> dict:
    """Process one Pub/Sub message.

    Declared with plain `def` (not `async def`) on purpose: ingestion is blocking
    I/O (Document AI, Vertex AI, Postgres), and FastAPI runs plain functions in a
    worker thread so the server stays responsive.
    """
    attrs = envelope.message.attributes
    event_type = attrs.get("eventType")
    bucket = attrs.get("bucketId")
    object_name = attrs.get("objectId")

    log = logger.bind(
        message_id=envelope.message.messageId,
        delivery_attempt=envelope.deliveryAttempt,
        event_type=event_type,
        bucket=bucket,
        object_name=object_name,
    )

    # A message without the fields we need can never succeed -> ack it (200) so it
    # doesn't bounce around until it hits the dead-letter queue for no reason.
    if not bucket or not object_name:
        log.warning("push_ignored_malformed_message")
        return {"status": "ignored", "reason": "missing bucketId/objectId"}

    # Event types we don't care about: ack and move on.
    if event_type not in _HANDLED_EVENTS:
        log.info("push_ignored_event_type")
        return {"status": "ignored", "reason": f"event type {event_type}"}

    try:
        # Both FINALIZE (upload/overwrite) and DELETE go through the same function:
        # if the object no longer exists it removes the document from the index.
        outcome = ingest_object(bucket, object_name)
    except Exception:
        # Unexpected failure (e.g. database unreachable). Log it with the traceback,
        # then re-raise -> FastAPI answers 500 -> Pub/Sub redelivers (and eventually dead-letters).
        log.exception("push_unhandled_error")
        raise

    if outcome.should_retry:
        # Another worker owns it, or a transient error occurred: ask Pub/Sub to redeliver.
        response.status_code = 503
    # Otherwise the default 200 acknowledges the message.

    return {
        "status": outcome.status,
        "tenant_id": outcome.tenant_id,
        "document_id": outcome.document_id,
        "chunks": outcome.chunk_count,
        "detail": outcome.detail,
    }
