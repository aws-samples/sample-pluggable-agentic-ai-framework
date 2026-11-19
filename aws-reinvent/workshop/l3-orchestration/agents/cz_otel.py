"""CloudZero OTEL-direct tracing for the reInvent specialist agents.

Shared by ``agents/order_agent_a2a.py``, ``agents/refund_agent_a2a.py`` and
``agents/research_agent_a2a.py``. Imported with the same package-path-then-bare
discipline as ``dd_otel.py``.

CloudZero receives ONLY LiteLLM's ``litellm_request`` spans (one per model call,
with token and cost attributes, no message content). LiteLLM's OTEL integration
is given a PRIVATE TracerProvider that is never set as the global one, so the
Strands / A2A / runtime spans keep going to whatever global provider the runtime
has (the managed ADOT -> CloudWatch, which stays on in CloudZero mode).

Config comes from dedicated ``CZ_*`` env vars set by
``helpers.observability.cloudzero_otel_env`` at launch, never from the generic
``OTEL_*`` vars (ADOT reads those). The gate is ``CZ_OTEL_DIRECT=true``.

Failure policy: a CloudZero misconfiguration never stops the agent from serving.
Errors are logged (without the API key) and the agent continues untraced in
CloudZero; CloudWatch tracing is unaffected.
"""
import logging
import os

logger = logging.getLogger(__name__)

CZ_DEFAULT_ENDPOINT = "https://telemetry.cloudzero.com/v2/telemetry/litellm/v1/traces"


def install_cloudzero_observability() -> None:
    """Send LiteLLM's model-call spans to CloudZero. No-op unless ``CZ_OTEL_DIRECT=true``.

    Never raises: on any problem it logs an error and leaves CloudZero tracing off.
    """
    if os.environ.get("CZ_OTEL_DIRECT") != "true":
        return
    if os.environ.get("DD_OTEL_DIRECT") == "true":
        logger.error("CloudZero tracing disabled: CZ_OTEL_DIRECT and DD_OTEL_DIRECT are "
                     "both true; pick one provider.")
        return
    key = os.environ.get("CZ_API_KEY", "")
    problem = _input_problem(key, os.environ.get("CZ_ATTENDEE_ID", ""))
    if problem:
        logger.error("CloudZero tracing disabled: %s; the agent continues untraced in "
                     "CloudZero. Fix the value in Partner Selection and redeploy.", problem)
        return
    try:
        _install(key)
    except Exception as e:  # noqa: BLE001 - never stop the agent over tracing
        msg = str(e).replace(key, "***") if key else str(e)
        logger.error("CloudZero tracing disabled: setup failed (%s: %s); the agent "
                     "continues untraced in CloudZero.", type(e).__name__, msg)


def _input_problem(key: str, attendee_id: str) -> str:
    """Describe what is wrong with the key/username, or return "". Never echoes the key."""
    if not key:
        return "CZ_API_KEY is empty"
    if not (key.isascii() and key.isprintable()) or " " in key or "," in key:
        return "CZ_API_KEY contains whitespace, a control character, a non-ASCII character or ','"
    if not attendee_id:
        return "CZ_ATTENDEE_ID (CloudZero username) is empty"
    if not attendee_id.isprintable() or "," in attendee_id or "%" in attendee_id:
        return "CZ_ATTENDEE_ID (CloudZero username) contains a control character, ',' or '%'"
    return ""


def _install(key: str) -> None:
    import litellm
    import requests
    from litellm.integrations.opentelemetry import OpenTelemetry, OpenTelemetryConfig
    from opentelemetry.exporter.otlp.proto.http import Compression
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import ALWAYS_ON

    if any(isinstance(cb, OpenTelemetry) for cb in litellm.callbacks):
        return  # already installed (module imported under both names)

    endpoint = os.environ.get("CZ_OTEL_ENDPOINT") or CZ_DEFAULT_ENDPOINT
    service_name = os.environ.get("CZ_SERVICE_NAME", "")
    attendee_id = os.environ.get("CZ_ATTENDEE_ID", "")

    # Every exporter/provider argument is explicit so nothing is picked up from the
    # OTEL_* env that ADOT owns (endpoint, headers, compression, sampler, credential
    # session). Resource(...) rather than Resource.create(...) so ADOT's
    # OTEL_RESOURCE_ATTRIBUTES / OTEL_SERVICE_NAME do not leak into CloudZero dimensions.
    # LiteLLM's own resource is not used either: it adds model_id=<service name>,
    # which CloudZero would show as a misleading dimension, and
    # deployment.environment=production (ignored by CloudZero).
    exporter = OTLPSpanExporter(
        endpoint=endpoint,
        headers={"Authorization": key},
        compression=Compression.NoCompression,
        session=requests.Session(),
    )
    provider = TracerProvider(
        resource=Resource({"service.name": service_name, "attendee_id": attendee_id}),
        sampler=ALWAYS_ON,
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))

    # Passing tracer_provider makes LiteLLM use it as-is and never call
    # trace.set_tracer_provider. ignore_context_propagation makes LiteLLM always
    # create its own litellm_request span instead of writing its attributes onto
    # the active Strands span (no USE_OTEL_LITELLM_REQUEST_SPAN needed). The span
    # still takes the active Strands span (ADOT) as parent via the current context,
    # so it shares the CloudWatch trace id; that parent is never sent to CloudZero,
    # whose recognizer anchors on the span name. ALWAYS_ON (not ParentBased) so the
    # runtime's sampling decision never drops a CloudZero span.
    config = OpenTelemetryConfig(
        exporter=exporter,
        service_name=service_name,
        ignore_context_propagation=True,
    )
    otel = OpenTelemetry(config=config, tracer_provider=provider)

    # No prompts/completions on the span. generation_name is never set (LiteLLM
    # would rename the litellm_request span with it).
    litellm.turn_off_message_logging = True
    litellm.callbacks.append(otel)
    logger.info("CloudZero tracing enabled: LiteLLM model-call spans -> %s "
                "(service.name=%s, attendee_id=%s)", endpoint, service_name, attendee_id)
