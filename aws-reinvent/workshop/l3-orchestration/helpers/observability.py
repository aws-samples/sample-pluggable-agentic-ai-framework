"""
L3 — Observability provider branching (CloudWatch native | Datadog OTEL-direct).

Mirrors the reInvent `helpers/*.py` pattern (see l1-data-knowledge/helpers):
the provider branching lives here so the deploy notebooks (2_specialist_agents,
3_orchestrator_agent) stay thin.

reInvent is **OTEL-direct**: when the observability provider is "datadog" the
L3 specialists (order/refund Runtimes) export their spans through an
**app-owned OTLP exporter** built in agents/*_a2a.py — the managed ADOT
auto-instrumentation is disabled on those Runtimes via
``configure(disable_otel=True)`` so the app can own the trace pipeline — while
the orchestrator Harness still exports straight to Datadog's OTLP traces intake
via the environment variables this helper emits. No ddtrace, no trace-agent
sidecar, no AI Guard — that sidecar path is the separate aws-datadog track and
must not be imported here.

Logs ALWAYS stay on CloudWatch in both modes — only spans/traces branch.

SSM schema (written by the L0 selector, read here):
    {prefix}/config/observability/provider   # "cloudwatch" | "datadog" (String)
    {prefix}/config/observability/api_key     # Datadog ingest key       (SecureString)
    {prefix}/config/observability/site        # Datadog site             (String)

Public functions:
    get_observability_provider(ssm, prefix=...) -> str
    datadog_otel_env(ssm, service_name, prefix=...) -> dict[str, str]
    mask_secret(value) -> str
"""
from __future__ import annotations

SSM_PREFIX_DEFAULT = "/anycompany/agentcore"

# Allowlist of valid Datadog site identifiers. The site is interpolated into the
# OTLP traces endpoint (https://trace.agent.<site>/v1/traces) right next to the
# ingest key, so an arbitrary value would let a tampered SSM parameter route the
# dd-api-key to an attacker-controlled host. Validate at this boundary because
# SSM can be written directly, bypassing the L0 UI. Keep in sync with the L0
# selector (l0-partner-selection/snippets.py DATADOG_SITES).
DATADOG_SITES = frozenset({
    "datadoghq.com",        # US1
    "us3.datadoghq.com",    # US3
    "us5.datadoghq.com",    # US5
    "datadoghq.eu",         # EU1
    "ap1.datadoghq.com",    # AP1
    "ddog-gov.com",         # US1-FED
})


def get_observability_provider(ssm, prefix: str = SSM_PREFIX_DEFAULT) -> str:
    """Return the chosen observability provider, defaulting to 'cloudwatch'.

    Reads {prefix}/config/observability/provider (written by the L0 selector).
    If the parameter is absent or empty, the default is CloudWatch native
    tracing, which keeps the existing reInvent behaviour unchanged.
    """
    name = f"{prefix}/config/observability/provider"
    try:
        value = ssm.get_parameter(Name=name)["Parameter"]["Value"].strip().lower()
    except ssm.exceptions.ParameterNotFound:
        return "cloudwatch"
    return value or "cloudwatch"


def mask_secret(value: str) -> str:
    """Mask a secret for safe printing (first 4 + last 4 chars). Never print
    a raw Datadog key."""
    if not value:
        return "<empty>"
    if len(value) <= 8:
        return "\u2026"
    return f"{value[:4]}\u2026{value[-4:]}"


def datadog_otel_env(ssm, service_name: str, prefix: str = SSM_PREFIX_DEFAULT) -> dict:
    """Build the OTEL-direct env vars that point a component's traces at Datadog.

    Reads config/observability/{api_key,site}. api_key is a SecureString, so we
    decrypt it. These env vars are set on the Runtime (specialists, via
    ``launch(env_vars=...)``) and on the Harness (``environmentVariables=...``).

    How each component uses this dict differs:

    - **Specialists (order/refund Runtimes):** the managed ADOT is disabled via
      ``configure(disable_otel=True)``, and the agent builds its OWN OTLP
      exporter (see agents/*_a2a.py). It reads the endpoint + dd-api-key header
      from the ``OTEL_EXPORTER_OTLP_TRACES_*`` vars below, and reads
      ``DD_OTEL_DIRECT`` as the gate that tells it to install that exporter.
    - **Harness:** still exports via these env vars directly (managed OTEL stays
      on); it ignores the passive ``DD_OTEL_DIRECT`` marker.

    Session linking note: ``gen_ai.conversation.id`` (= the AgentCore runtime
    session id) is per-request and is NOT set here. The specialists stamp it in
    their agent code (see agents/*_a2a.py); the Harness relies on the managed
    runtime populating it from ``runtimeSessionId``. This dict carries only the
    static resource-level switch ``dd_llmobs_enabled=true`` which turns on the
    GenAI -> Datadog Agent Observability conversion for the whole trace.

    The managed ADOT auto-instrumentation is turned off on the specialist
    Runtimes by ``configure(disable_otel=True)`` in the deploy notebooks (not by
    any env var here); that is the single, verified mechanism that frees the
    trace pipeline for the app-owned exporter. ``DD_OTEL_DIRECT`` is safe to emit
    from this shared helper because it is a passive marker the Harness (nb5,
    which calls this same helper) ignores.
    """
    api_key = ssm.get_parameter(
        Name=f"{prefix}/config/observability/api_key", WithDecryption=True
    )["Parameter"]["Value"]
    site = ssm.get_parameter(
        Name=f"{prefix}/config/observability/site"
    )["Parameter"]["Value"].strip()
    # Reject an unrecognised site before it reaches the endpoint the ingest key
    # is sent to (a tampered SSM value must not redirect the key off Datadog).
    if site not in DATADOG_SITES:
        raise ValueError(
            f"Unsupported Datadog site {site!r}. Expected one of: "
            f"{', '.join(sorted(DATADOG_SITES))}."
        )
    return {
        # Datadog's OTLP traces intake rejects gRPC — protobuf over HTTP only.
        "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": f"https://trace.agent.{site}/v1/traces",
        # dd-api-key authenticates the ingest; keep it masked when printing.
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS": f"dd-api-key={api_key}",
        # GenAI semantic conventions 1.37+ — required for the LLM-Obs conversion
        # and emitted by strands-agents once this opt-in is set.
        "OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_latest_experimental",
        "OTEL_SERVICE_NAME": service_name,
        # dd_llmobs_enabled=true is the OTEL-direct switch that turns GenAI spans
        # into Datadog Agent Observability — no SDK, no sidecar, no AI Guard.
        "OTEL_RESOURCE_ATTRIBUTES": "deployment.environment=demo,dd_llmobs_enabled=true",
        # Gate the specialist agents read to decide whether to install their
        # app-owned OTLP exporter. A dedicated flag (not an ADOT-managed
        # attribute) so no ADOT-disable path can silently strip it. The Harness
        # ignores it.
        "DD_OTEL_DIRECT": "true",
        # Validated site, carried so the specialist exporter can rebuild the
        # traces endpoint if OTEL_EXPORTER_OTLP_TRACES_ENDPOINT is ever stripped
        # (fail-safe fallback — see agents/dd_otel.py).
        "DD_SITE": site,
        # Turn OFF the a2a-python-sdk OTel instrumentation so the specialist
        # runtimes stop emitting the a2a transport/workflow spans
        # (on_message_send, _setup_message_execution, _run_event_stream). Per
        # Datadog's guidance these carry no LLM content, clutter the Agent-Obs
        # trace, and the LLM-Obs conversion keeps them inconsistently (the cause
        # of specialists appearing as disconnected Agent-Obs traces). The strands
        # agent/LLM spans still export and still adopt the propagated remote
        # parent via SessionHeaderASGIMiddleware, so the trace stays connected.
        # Harness-safe: the managed Harness runs no a2a-sdk, so this is inert
        # there (a passive marker it ignores, like DD_OTEL_DIRECT).
        "OTEL_INSTRUMENTATION_A2A_SDK_ENABLED": "false",
    }
