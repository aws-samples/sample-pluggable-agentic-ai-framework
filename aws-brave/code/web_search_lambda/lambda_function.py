"""Lambda: anycompany_brave_web_search.

Invoked by the AgentCore Gateway (MCP target). Reads the Brave API key from
Secrets Manager (cached across warm invocations), runs a Brave web search via
brave_client, and returns a normalized JSON payload. boto3 is imported lazily
so unit tests can inject a fake Secrets Manager client without boto3 installed.
"""
import json
import os

from brave_client import search

SECRET_ID = os.environ.get("BRAVE_SECRET_ID", "brave/search-api-key")
_CACHE = {}


def _get_api_key(sm_client=None):
    if _CACHE.get("api_key"):
        return _CACHE["api_key"]
    if sm_client is None:
        import boto3  # lazy: keeps unit tests boto3-free
        sm_client = boto3.client("secretsmanager")
    resp = sm_client.get_secret_value(SecretId=SECRET_ID)
    raw = resp.get("SecretString") or ""
    try:
        parsed = json.loads(raw)
        api_key = parsed.get("api_key") or parsed.get("BRAVE_API_KEY") or ""
        if not api_key and isinstance(parsed, dict):
            # Fall back to the sole value of a single-key JSON secret
            # (e.g. {"BraveSearchAPI": "..."}).
            str_values = [v for v in parsed.values() if isinstance(v, str) and v.strip()]
            if len(str_values) == 1:
                api_key = str_values[0]
    except (ValueError, json.JSONDecodeError):
        api_key = raw.strip()
    _CACHE["api_key"] = api_key
    return api_key


def _extract_args(event):
    if not isinstance(event, dict):
        return {}
    for key in ("arguments", "body", "input"):
        if isinstance(event.get(key), dict):
            return event[key]
    return event


def lambda_handler(event, context=None, sm_client=None):
    args = _extract_args(event)
    query = args.get("query", "")
    count = args.get("count", 5)
    freshness = args.get("freshness")
    if not query or not str(query).strip():
        return {"status": "error", "message": "query is required"}
    api_key = _get_api_key(sm_client)
    if not api_key:
        return {"status": "error", "message": "Brave API key not available"}
    return search(query, api_key, count=count, freshness=freshness)
