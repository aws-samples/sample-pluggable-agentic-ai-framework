"""Datadog OTEL-direct trace pipeline for the reInvent specialist agents.

Shared by ``agents/order_agent_a2a.py`` and ``agents/refund_agent_a2a.py`` so the
exporter + session-linking logic lives in ONE place (it was previously
duplicated byte-for-byte in both files). Imported with the same
package-path-then-bare discipline the aws-datadog track uses, because the
AgentCore container runs the entrypoint as the package module ``agents.<name>``
while local unit tests put ``agents/`` on ``sys.path``.

Why this exists
---------------
The reInvent specialists run on AgentCore Runtime under the managed ADOT
auto-instrumentation, which owns the OpenTelemetry trace pipeline and exports to
CloudWatch. Setting ``OTEL_EXPORTER_OTLP_*`` env vars does not redirect it. When
the deploy selects Datadog, the notebooks disable the managed ADOT wrapper via
``configure(disable_otel=True)`` and this module gives the app its OWN
``TracerProvider`` with an OTLP/HTTP exporter pointed at Datadog, plus a span
processor that stamps the AgentCore runtime session id on every span. When the
inbound A2A message carries the orchestrator's W3C ``traceparent``, specialist
spans join the orchestrator's trace; the session id groups order + refund +
orchestrator spans into ONE Datadog session either way, and is the fallback
when no traceparent is present.

The gate is the ``DD_OTEL_DIRECT=true`` env var (emitted by
``helpers.observability.datadog_otel_env`` at launch), NOT any ADOT-managed
resource attribute, so no ADOT-disable path can silently turn the fix off.

Only the standard library is imported at module load; OpenTelemetry is imported
lazily inside :func:`install_datadog_observability` so this module (and its unit
tests) import without the otel packages present.

Failure policy (Finding F1): because the managed ADOT wrapper is OFF in Datadog
mode, a swallowed exporter-setup failure would leave the Runtime serving with no
trace destination at all. So in Datadog mode this module FAILS startup (re-raises)
rather than fail open to an untraced agent.
"""
import json
import logging
import os
from contextvars import ContextVar

logger = logging.getLogger(__name__)

# The AgentCore runtime session id for the current request, copied off the
# inbound header by the ASGI middleware so span creation (same request task) can
# read it. Default empty => spans are exported but ungrouped.
SESSION_ID: ContextVar[str] = ContextVar("agentcore_runtime_session_id", default="")
# The delegated request text for the current A2A request, captured by the ASGI
# middleware so the span processor can stamp it as the entry (SERVER) span's
# input.value. Without it the a2a transport span carries no LLM I/O and renders
# as a contentless "No content" row in Datadog (the real I/O is on its strands
# child spans). Default empty => no input stamped (span simply unchanged).
REQUEST_INPUT: ContextVar[str] = ContextVar("a2a_request_input", default="")
SESSION_ID_HEADER = b"x-amzn-bedrock-agentcore-runtime-session-id"
# A2A message-metadata key the agent-invoker Lambda stamps with the Harness
# conversation/grouping id (Design B, caller-injected). The specialist PREFERS
# it over the per-call runtime session header so order + refund + orchestrator
# spans share ONE Datadog session; absent/garbled => fall back to the header
# (span simply ungrouped, never a broken request).
CONVERSATION_ID_KEY = "conversationId"


def datadog_otel_enabled() -> bool:
    """True when the deploy set ``DD_OTEL_DIRECT=true`` (the OTEL-direct gate)."""
    return os.environ.get("DD_OTEL_DIRECT") == "true"


def conversation_id_from_body(body: bytes) -> str:
    """Return the caller-injected conversation id from an A2A ``message/send`` body.

    Looks for ``conversationId`` in the message metadata (and, as a fallback, the
    params metadata). Returns ``""`` when the body is empty, not JSON, or carries
    no id — the caller then falls back to the runtime session header, so a missing
    or garbled id only leaves the span ungrouped, never breaks the request.
    """
    if not body:
        return ""
    try:
        params = json.loads(body).get("params", {}) or {}
        message = params.get("message", {}) or {}
        for meta in (message.get("metadata"), params.get("metadata")):
            if isinstance(meta, dict):
                cid = meta.get(CONVERSATION_ID_KEY)
                if isinstance(cid, str) and cid.strip():
                    return cid.strip()
    except (ValueError, AttributeError, TypeError):
        pass
    return ""


def traceparent_from_body(body: bytes) -> dict:
    """Return a W3C trace-context carrier from an A2A ``message/send`` body.

    Pulls ``traceparent`` (and ``tracestate`` when present) from the message
    metadata (and, as a fallback, the params metadata) — the same A2A body shape
    the agent-invoker Lambda stamps alongside ``conversationId``. Returns a dict
    carrier ``{"traceparent": ..., "tracestate": ...}`` suitable for
    ``TraceContextTextMapPropagator().extract(...)``, or ``{}`` when the body is
    empty, not JSON, or carries no traceparent. Mirrors
    :func:`conversation_id_from_body`: never raises on malformed input, so a
    missing or garbled value only skips remote-parent linking (spans fall back to
    session grouping), never breaks the request.
    """
    if not body:
        return {}
    try:
        params = json.loads(body).get("params", {}) or {}
        message = params.get("message", {}) or {}
        for meta in (message.get("metadata"), params.get("metadata")):
            if isinstance(meta, dict):
                tp = meta.get("traceparent")
                if isinstance(tp, str) and tp.strip():
                    carrier = {"traceparent": tp.strip()}
                    ts = meta.get("tracestate")
                    if isinstance(ts, str) and ts.strip():
                        carrier["tracestate"] = ts.strip()
                    return carrier
    except (ValueError, AttributeError, TypeError):
        pass
    return {}


def message_text_from_body(body: bytes) -> str:
    """Return the delegated request text from an A2A ``message/send`` body.

    Concatenates the text of the message ``parts`` so it can be surfaced as the
    entry (a2a SERVER) span's ``input.value``. Returns ``""`` when the body is
    empty, not JSON, or has no text parts. Never raises on malformed input — a
    missing value just leaves the span's input unset, never breaks the request.
    """
    if not body:
        return ""
    try:
        params = json.loads(body).get("params", {}) or {}
        message = params.get("message", {}) or {}
        parts = message.get("parts", []) or []
        texts = [p.get("text", "") for p in parts
                 if isinstance(p, dict) and p.get("kind") == "text"]
        return "\n".join(t for t in texts if t).strip()
    except (ValueError, AttributeError, TypeError):
        pass
    return ""


class SessionHeaderASGIMiddleware:
    """Pure ASGI middleware that resolves the session-linking id for the request
    and copies it into a ContextVar so span creation (same request task) can read it.

    It PREFERS the caller-injected A2A conversation id (message metadata, stamped
    by the agent-invoker Lambda) over the per-call runtime session header, so the
    orchestrator and its specialists group into ONE Datadog session. When the body
    carries no conversation id it falls back to the header — the span is simply
    ungrouped, never a broken request.

    A pure ASGI middleware runs in the request's own task, so the ContextVar
    propagates to the downstream agent code — unlike Starlette's
    BaseHTTPMiddleware, which runs in a separate task and would break
    ContextVar propagation.

    When the body also carries a W3C ``traceparent`` (re-injected by the
    agent-invoker Lambda), this middleware additionally extracts and attaches the
    remote OTel context before awaiting the app, so the strands A2AServer root
    span can adopt the Harness trace as its parent. That path is gated by the
    Datadog OTEL-direct flag and is fully fail-soft (see ``__call__``).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        header_sid = ""
        for k, v in scope.get("headers", []):
            if k.lower() == SESSION_ID_HEADER and v:
                header_sid = v.decode("latin-1")
                break

        # Buffer the request body so we can read the caller-injected conversation
        # id, then replay it unchanged to the downstream app.
        buffered = []
        more_body = True
        while more_body:
            event = await receive()
            buffered.append(event)
            more_body = event.get("more_body", False)
        body = b"".join(e.get("body", b"") for e in buffered)

        sid = conversation_id_from_body(body) or header_sid
        if sid:
            SESSION_ID.set(sid)

        # Capture the delegated request text so the span processor can stamp it as
        # the entry (SERVER) span's input.value. Set here, in the request's own
        # task, so the ContextVar is live when the strands A2AServer opens the span.
        req_text = message_text_from_body(body)
        if req_text:
            REQUEST_INPUT.set(req_text)

        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                # Collapse the buffered chunks into one event the app can consume.
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        # Adopt the remote parent span if the agent-invoker Lambda re-injected a
        # W3C traceparent into the A2A body. Extract + attach BEFORE awaiting the
        # downstream app (this runs in the request's own task, so the attach is the
        # ambient OTel context when the strands A2AServer opens its root span) and
        # detach in a finally. Gated by the Datadog OTEL-direct path and fully
        # fail-soft: no traceparent, a garbled one, or any otel import/extract error
        # -> no attach, fall back to session grouping, never break the request.
        otel_token = None
        otel_context = None
        if datadog_otel_enabled():
            try:
                carrier = traceparent_from_body(body)
                if carrier:
                    from opentelemetry import context as otel_context
                    from opentelemetry.trace.propagation.tracecontext import (
                        TraceContextTextMapPropagator,
                    )
                    ctx = TraceContextTextMapPropagator().extract(carrier)
                    otel_token = otel_context.attach(ctx)
            except Exception:
                otel_token = None
                otel_context = None

        try:
            await self.app(scope, replay, send)
        finally:
            if otel_token is not None and otel_context is not None:
                try:
                    otel_context.detach(otel_token)
                except Exception:
                    pass


def parse_otlp_headers(raw: str) -> dict:
    """Parse the OTLP ``*_HEADERS`` env format ``k1=v1,k2=v2`` into a dict.

    OTLP header values are percent-encoded per spec, so each value is
    urllib-unquoted. (The Datadog dd-api-key is hex with no encodable chars, so
    this is a correctness safeguard, not a behaviour change today.)
    """
    from urllib.parse import unquote
    out = {}
    for pair in raw.split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            out[k.strip()] = unquote(v.strip())
    return out


def resolve_traces_endpoint() -> str:
    """Return the OTLP traces endpoint, falling back to DD_SITE if the per-signal
    endpoint var was stripped (fail-safe for Finding F1)."""
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    if endpoint:
        return endpoint
    site = os.environ.get("DD_SITE", "").strip()
    if not site:
        raise RuntimeError(
            "Datadog mode (DD_OTEL_DIRECT=true) but neither "
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT nor DD_SITE is set; cannot build "
            "the Datadog traces endpoint."
        )
    return f"https://trace.agent.{site}/v1/traces"


def _make_session_id_span_processor():
    """Build the span processor that stamps the runtime session id on each span.

    Defined behind a factory so the OpenTelemetry import stays lazy (the module
    and its stdlib-only tests load without otel installed)."""
    from opentelemetry.sdk.trace import SpanProcessor
    from opentelemetry.trace import SpanKind

    class SessionIdSpanProcessor(SpanProcessor):
        def on_start(self, span, parent_context=None):
            sid = SESSION_ID.get()
            if sid:
                span.set_attribute("gen_ai.conversation.id", sid)  # Datadog session_id
                span.set_attribute("session.id", sid)              # OpenInference equiv
            # The A2A transport entry span (span.kind SERVER) carries no GenAI
            # semconv attributes, so Datadog cannot classify it: it defaults to a
            # generic "workflow" and raises the "missing operation name" +
            # "unrecognized instrumentation" issues, rendering a "No content" row.
            # Per the Datadog OTel LLM-Obs mapping, set gen_ai.operation.name
            # (-> classifies as an *agent* span) and gen_ai.system (-> identifies
            # the instrumentation, clears the spec-version warning), then stamp the
            # delegated request as input.value (used directly as meta.input.value
            # for agent spans). Only the SERVER entry span gets this; the strands
            # child spans are INTERNAL and already carry their own gen_ai content.
            if getattr(span, "kind", None) == SpanKind.SERVER:
                span.set_attribute("gen_ai.operation.name", "invoke_agent")
                span.set_attribute("gen_ai.system", "aws.bedrock")
                req_text = REQUEST_INPUT.get()
                if req_text:
                    span.set_attribute("input.value", req_text)

        def on_end(self, span):
            ...

        def shutdown(self):
            ...

        def force_flush(self, timeout_millis: int = 30000):
            return True

    return SessionIdSpanProcessor()


def install_datadog_observability(fastapi_app) -> None:
    """Install the app-owned Datadog OTLP exporter + session-linking processor.

    No-op unless ``DD_OTEL_DIRECT=true`` (CloudWatch mode leaves the managed ADOT
    / X-Ray path untouched). In Datadog mode a setup failure RE-RAISES so the
    container fails to start instead of silently serving untraced (Finding F1).
    """
    if not datadog_otel_enabled():
        # Warn only if the Datadog *traces* ingest header is present while the
        # gate is off (a stripped/renamed DD_OTEL_DIRECT) — not on the generic
        # OTEL_EXPORTER_OTLP* vars the managed ADOT path injects on every
        # CloudWatch startup, which would otherwise warn spuriously (Finding F9).
        if "dd-api-key=" in os.environ.get("OTEL_EXPORTER_OTLP_TRACES_HEADERS", ""):
            logger.warning(
                "Datadog OTLP traces header present but DD_OTEL_DIRECT!=true; "
                "Datadog exporter NOT installed. Check the launch env/gate."
            )
        return
    try:
        from opentelemetry import trace as _otel_trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        # --- Resource (FR4) ---
        # service.name + deployment.environment are set explicitly. dd_llmobs_enabled
        # is ALSO set here (not relied on via OTEL_RESOURCE_ATTRIBUTES) so the GenAI->
        # Datadog Agent/LLM Observability conversion + one-grouped-session behaviour
        # hold regardless of what the ADOT-disable path does to env attributes.
        # The neutral fallback is never hit on the live Datadog path because the
        # launch cell always sets OTEL_SERVICE_NAME.
        service_name = os.environ.get("OTEL_SERVICE_NAME", "anycompany-agent")
        resource = Resource.create({
            "service.name": service_name,
            "deployment.environment": "demo",
            "dd_llmobs_enabled": "true",
        })

        # --- OTLP/HTTP exporter to Datadog (FR3) ---
        endpoint = resolve_traces_endpoint()
        headers = parse_otlp_headers(os.environ.get("OTEL_EXPORTER_OTLP_TRACES_HEADERS", ""))
        exporter = OTLPSpanExporter(endpoint=endpoint, headers=headers)

        # --- ONE provider holds BOTH the exporter and the session processor (FR8/AC4) ---
        tp = _otel_trace.get_tracer_provider()
        if not isinstance(tp, TracerProvider):      # default is a Proxy provider -> create ours
            tp = TracerProvider(resource=resource)
            _otel_trace.set_tracer_provider(tp)
        else:
            # A real SDK provider already exists => configure(disable_otel=True)
            # did NOT take effect. We still attach to it to keep one provider, but
            # our Resource (service.name, dd_llmobs_enabled) is NOT applied and
            # spans dual-export under ADOT's service name. Log so a live test can
            # detect this degraded path (Finding F10).
            logger.warning(
                "An SDK TracerProvider already exists; reusing it. App Resource "
                "(service.name=%s, dd_llmobs_enabled) NOT applied — check that "
                "configure(disable_otel=True) took effect.",
                service_name,
            )
        tp.add_span_processor(BatchSpanProcessor(exporter))
        tp.add_span_processor(_make_session_id_span_processor())
        fastapi_app.add_middleware(SessionHeaderASGIMiddleware)

        # Never log the key; endpoint only.
        logger.info(
            "Datadog OTEL-direct tracing enabled (exporter -> %s, session linking on).",
            endpoint,
        )
    except Exception as e:
        # Datadog mode with managed ADOT OFF: a swallowed failure means NO trace
        # destination. Surface it and fail startup (Finding F1).
        logger.error("Datadog observability setup FAILED: %s: %s", type(e).__name__, e)
        raise
