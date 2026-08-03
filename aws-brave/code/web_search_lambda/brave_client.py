"""Brave Web Search client — stdlib only (urllib), so it runs in the Lambda
runtime with no extra dependencies. All network I/O is injectable via `opener`
so the logic is unit-testable without hitting the network."""
import json
import urllib.error
import urllib.parse
import urllib.request

BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
MAX_COUNT = 20
MAX_DESCRIPTION_CHARS = 500
DEFAULT_TIMEOUT = 10.0


class BraveSearchError(Exception):
    """Raised for invalid inputs before an API call is attempted."""


def _clamp_count(count):
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = 5
    return max(1, min(count, MAX_COUNT))


def build_request(query, api_key, count=5, freshness=None, country=None, search_lang=None):
    if not query or not str(query).strip():
        raise BraveSearchError("query must be a non-empty string")
    if not api_key:
        raise BraveSearchError("api_key is required")
    params = {"q": str(query).strip(), "count": _clamp_count(count)}
    if freshness:
        params["freshness"] = freshness
    if country:
        params["country"] = country
    if search_lang:
        params["search_lang"] = search_lang
    url = BRAVE_ENDPOINT + "?" + urllib.parse.urlencode(params)
    return urllib.request.Request(
        url,
        headers={"Accept": "application/json", "X-Subscription-Token": api_key},
        method="GET",
    )


def normalize_response(payload, max_results=5):
    results = []
    web = (payload or {}).get("web") or {}
    for item in (web.get("results") or [])[:max_results]:
        desc = item.get("description") or ""
        if len(desc) > MAX_DESCRIPTION_CHARS:
            desc = desc[:MAX_DESCRIPTION_CHARS].rstrip() + "\u2026"
        results.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "description": desc,
        })
    more = bool(((payload or {}).get("query") or {}).get("more_results_available"))
    return {"results": results, "more_results_available": more}


def search(query, api_key, count=5, freshness=None, country=None,
           search_lang=None, timeout=DEFAULT_TIMEOUT, opener=None):
    """Run a Brave web search; return a normalized dict or an error envelope."""
    if opener is None:
        def opener(request, timeout):
            return urllib.request.urlopen(request, timeout=timeout)
    count = _clamp_count(count)
    try:
        request = build_request(query, api_key, count, freshness, country, search_lang)
    except BraveSearchError as exc:
        return {"status": "error", "message": str(exc)}
    try:
        with opener(request, timeout) as resp:
            raw = resp.read()
        payload = json.loads(raw)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
            err = (json.loads(body).get("error") or {})
            detail = err.get("detail") or err.get("code") or body[:200]
        except Exception:
            detail = ""
        message = f"Brave API returned HTTP {exc.code}"
        if detail:
            message += f": {detail}"
        return {"status": "error", "message": message}
    except urllib.error.URLError as exc:
        return {"status": "error", "message": f"Brave API request failed: {exc.reason}"}
    except (ValueError, json.JSONDecodeError):
        return {"status": "error", "message": "Brave API returned invalid JSON"}
    normalized = normalize_response(payload, count)
    return {
        "status": "success",
        "query": str(query).strip(),
        "results": normalized["results"],
        "more_results_available": normalized["more_results_available"],
    }
