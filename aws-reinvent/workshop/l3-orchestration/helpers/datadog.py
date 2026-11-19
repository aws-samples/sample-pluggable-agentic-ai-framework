"""Datadog observability provider for the L3 orchestration notebooks.

Thin wrapper over the CloudWatch provider (``cloudwatch.py``). A workshop
participant who selects **Datadog** imports it as ``from helpers import datadog``
and calls ``datadog.enable(...)`` exactly the way the CloudWatch path calls
``cloudwatch.enable(...)`` — the signature is identical, so swapping providers is
a one-line import change.

Split of responsibilities (the settled reInvent design):

- **Logs** always go to CloudWatch, so this delegates log delivery straight to
  ``cloudwatch.enable_log_delivery`` — no delivery logic is reimplemented here.
- **Traces** already reach Datadog through the deploy-time OTEL-direct exporters
  (specialists via ``agents/dd_otel.py`` with ``configure(disable_otel=True)``;
  the Harness via its OTLP env vars). So this provider intentionally does NOT
  call ``enable_tracing`` / register a native CloudWatch TRACES source.
"""

from . import cloudwatch

# enable() is implemented (it delegates to the CloudWatch log-delivery helper).
IMPLEMENTED = True


def enable(logs_client, kind, resource_arn, resource_id, account_id, log_types=None):
    """Wire logs to CloudWatch and skip native tracing (traces go to Datadog).

    Signature matches ``cloudwatch.enable`` so a provider swap is a one-line
    import change.
    """
    cfg = cloudwatch.KINDS[kind]
    cloudwatch.enable_log_delivery(logs_client, resource_arn, resource_id,
                                   cfg["prefix"], cfg["log_group_base"],
                                   log_types or cfg["log_types"], account_id)
    # Datadog mode wires LOGS to CloudWatch only; it does NOT create a native
    # CloudWatch TRACES delivery. Where traces actually go depends on the resource:
    #   - order/refund runtimes: exported to Datadog by the app-owned OTEL exporter
    #     (agents/dd_otel.py), configured at deploy time (not here).
    #   - Harness: exported to Datadog via its OTEL env vars (set in nb5).
    #   - memory / gateway: no app exporter, so their spans are NOT exported anywhere.
    # So this function deliberately does not call enable_tracing(); it makes no claim
    # about Datadog delivery for this specific resource.
    print(f"{kind}: logs -> CloudWatch; no native CloudWatch TRACES delivery created (Datadog mode)")


# Alias kept parallel to the CloudWatch module (cloudwatch.enable_observability).
enable_observability = enable
