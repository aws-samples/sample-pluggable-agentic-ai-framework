"""Pure helpers for the Research Agent's web_search tool. No AWS imports, so
this module is unit-testable and shared by research_agent_a2a.py."""
import json
import os

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "tool_schemas.json")
GATEWAY_TOOL_NAME = "brave-web-search___web_search"
MAX_COUNT = 20


def build_search_args(query, count=5, freshness=None):
    """Build args for the Gateway tool. Returns (args, error_message)."""
    if not query or not str(query).strip():
        return None, "query must be a non-empty string"
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = 5
    count = max(1, min(count, MAX_COUNT))
    args = {"query": str(query).strip(), "count": count}
    if freshness:
        args["freshness"] = freshness
    return args, None


def load_tool_schemas(path=SCHEMA_PATH):
    with open(path) as fh:
        return json.load(fh)
