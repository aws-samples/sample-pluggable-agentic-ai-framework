import io
import json
import urllib.error

from brave_client import (
    build_request, normalize_response, search,
    BraveSearchError, MAX_COUNT, MAX_DESCRIPTION_CHARS, BRAVE_ENDPOINT,
)


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _opener_returning(payload):
    def _open(request, timeout):
        return _FakeResp(json.dumps(payload).encode())
    return _open


def test_build_request_sets_token_header_and_query():
    req = build_request("hello world", "KEY123", count=3)
    assert "KEY123" in dict(req.header_items()).values()
    assert "q=hello+world" in req.full_url
    assert "count=3" in req.full_url
    assert req.full_url.startswith(BRAVE_ENDPOINT)


def test_build_request_rejects_empty_query():
    try:
        build_request("   ", "KEY")
        assert False, "expected BraveSearchError"
    except BraveSearchError:
        pass


def test_build_request_requires_api_key():
    try:
        build_request("q", "")
        assert False, "expected BraveSearchError"
    except BraveSearchError:
        pass


def test_count_is_clamped_to_max():
    req = build_request("q", "KEY", count=999)
    assert f"count={MAX_COUNT}" in req.full_url


def test_normalize_truncates_description_and_limits_results():
    payload = {
        "web": {"results": [
            {"title": "T1", "url": "u1", "description": "x" * (MAX_DESCRIPTION_CHARS + 50)},
            {"title": "T2", "url": "u2", "description": "short"},
            {"title": "T3", "url": "u3", "description": "d3"},
        ]},
        "query": {"more_results_available": True},
    }
    out = normalize_response(payload, max_results=2)
    assert len(out["results"]) == 2
    assert len(out["results"][0]["description"]) <= MAX_DESCRIPTION_CHARS + 1
    assert out["more_results_available"] is True


def test_search_happy_path_returns_normalized():
    payload = {
        "web": {"results": [{"title": "AWS", "url": "https://aws.amazon.com", "description": "cloud"}]},
        "query": {"more_results_available": False},
    }
    out = search("aws", "KEY", count=5, opener=_opener_returning(payload))
    assert out["status"] == "success"
    assert out["query"] == "aws"
    assert out["results"][0]["url"] == "https://aws.amazon.com"


def test_search_http_error_returns_error_envelope():
    def _open(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", {}, None)
    out = search("aws", "KEY", opener=_open)
    assert out["status"] == "error"
    assert "429" in out["message"]


def test_search_invalid_query_returns_error_envelope():
    out = search("  ", "KEY", opener=_opener_returning({}))
    assert out["status"] == "error"
