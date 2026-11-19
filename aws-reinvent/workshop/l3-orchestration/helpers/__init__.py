"""Observability providers for the L3 orchestration notebooks.

This package is the L3 code that *enables* log/trace delivery for deployed agents;
the ``l6-observability`` workshop layer is where you *view and query* that telemetry.

Each provider is its own module with a matching ``enable(...)`` entry point:

    from helpers import cloudwatch   # implemented
    from helpers import datadog      # placeholder, not implemented yet

    cloudwatch.enable(logs_client, "runtime", ARN, resource_id, ACCOUNT_ID)

For backward compatibility, the CloudWatch helpers are also re-exported at the
package level so existing notebook cells keep working unchanged:

    from helpers import enable_observability, disable_log_delivery
"""

from . import cloudwatch
from . import datadog

# Backward-compatible top-level API — points at the CloudWatch provider, which is
# what the agent notebooks (2_specialist_agents, 3_orchestrator_agent) import.
from .cloudwatch import (
    enable_observability,
    enable_log_delivery,
    enable_tracing,
    disable_log_delivery,
    KINDS,
)

__all__ = [
    "cloudwatch",
    "datadog",
    "enable_observability",
    "enable_log_delivery",
    "enable_tracing",
    "disable_log_delivery",
    "KINDS",
]
