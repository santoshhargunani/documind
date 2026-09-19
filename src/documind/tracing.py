"""
tracing.py
----------
Sets up OpenTelemetry distributed tracing for DocuMind, exporting
spans to Google Cloud Trace. Import and call configure_tracing() once
at application startup (in api.py and mcp_server.py), then use the
tracer this module provides to wrap any unit of work worth measuring.

Why this exists: structlog (used throughout this project) tells us
WHAT happened at each step. Tracing tells us HOW LONG each step took,
nested in the actual call hierarchy — e.g. "the /ask request took 14s
total: 2s in the router's LLM call, 3s in retrieval (1s embedding +
2s DB query), 5s in synthesis, 4s in the critic." That breakdown is
what actually explains "why is this slow," and it's visible as a
timeline directly in the Cloud Trace console rather than something
you'd have to manually reconstruct from scattered log timestamps.
"""

# --- IMPORTS ---

from opentelemetry import trace
# The OpenTelemetry API — trace.get_tracer(...) is how any module in
# this project obtains a "tracer" object used to create spans. This
# is the vendor-neutral API; note it's separate from the SDK (below),
# which is the actual implementation that does something with the
# spans created via this API. This separation is deliberate in
# OpenTelemetry's design — application code only ever depends on the
# API, never the SDK or a specific exporter directly, so swapping
# where traces go (Cloud Trace today, something else later) never
# requires touching instrumented code, only this configuration file.

from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
# The Google Cloud-specific piece: takes completed spans and ships
# them to the Cloud Trace API. Confirmed as the current, actively
# maintained package (opentelemetry-exporter-gcp-trace) rather than
# assuming from possibly-stale training knowledge, given this
# project's repeated experience with fast-moving GCP/AI SDKs.

from opentelemetry.sdk.resources import SERVICE_NAME, Resource
# Resource: metadata describing WHAT is producing these traces (the
# service name, in our case) — this is what lets Cloud Trace's
# console group and filter traces by which service emitted them,
# useful once there are multiple services (this API, and later
# perhaps the MCP server) sending traces to the same project.

from opentelemetry.sdk.trace import TracerProvider
# The SDK's actual tracer implementation — as opposed to the API's
# abstract interface. This is what get_tracer_provider() will return
# once configured.

from opentelemetry.sdk.trace.export import BatchSpanProcessor
# BatchSpanProcessor collects finished spans and sends them to the
# exporter in batches on a background thread, rather than making a
# network call to Cloud Trace synchronously every single time a span
# ends (which would add real latency to every traced operation). This
# is the production-appropriate choice — the alternative,
# SimpleSpanProcessor, exports every span immediately and
# synchronously, which is simpler but meaningfully slower and only
# appropriate for quick local debugging, not a real deployed service.

from documind.config import get_settings


# --- CONFIGURATION FUNCTION ---

def configure_tracing(service_name: str) -> None:
    """
    Sets up the global OpenTelemetry tracer provider, configured to
    export spans to Cloud Trace for the given GCP project. Call this
    ONCE at application startup — calling it multiple times would
    create redundant, conflicting tracer providers.
    """

    settings = get_settings()

    resource = Resource.create({SERVICE_NAME: service_name})
    # SERVICE_NAME is a well-known OpenTelemetry semantic convention
    # key — using the standard constant instead of a raw string
    # ("service.name") means any OpenTelemetry-aware tool (Cloud
    # Trace's console included) recognizes and displays it correctly
    # without any custom configuration.

    provider = TracerProvider(resource=resource)
    # Creates the actual tracer provider, tagged with our service
    # identity via the resource above.

    exporter = CloudTraceSpanExporter(project_id=settings.gcp_project_id)
    # Configures WHERE spans go — this specific GCP project's Cloud
    # Trace. Authentication uses the same Application Default
    # Credentials every other GCP client in this project relies on —
    # your local gcloud login for dev, Workload Identity once running
    # in GKE — no separate tracing-specific credentials needed.

    provider.add_span_processor(BatchSpanProcessor(exporter))
    # Wires the exporter into the provider via the batching processor
    # discussed above.

    trace.set_tracer_provider(provider)
    # Registers this provider as the GLOBAL default — after this call,
    # any other module calling trace.get_tracer(__name__) anywhere in
    # the process gets a tracer backed by this exact configuration,
    # without needing to pass the provider around explicitly.


# --- SHARED TRACER INSTANCE ---

tracer = trace.get_tracer("documind")
# A single, module-level tracer instance other files import directly
# (e.g. `from documind.tracing import tracer`) rather than each
# calling get_tracer themselves. This tracer works correctly whether
# configure_tracing() has been called yet or not — OpenTelemetry's API
# is designed so that using tracing calls before configuration simply
# results in spans going nowhere (a harmless no-op), rather than
# raising an error. This matters for testability: unit tests can
# import and use functions instrumented with this tracer without
# needing Cloud Trace configured or reachable at all.
