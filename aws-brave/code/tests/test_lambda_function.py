import json

import lambda_function as lf


class _FakeSM:
    def __init__(self, secret_string):
        self._s = secret_string
        self.calls = 0

    def get_secret_value(self, SecretId):
        self.calls += 1
        return {"SecretString": self._s}


def setup_function(_):
    lf._CACHE.clear()


def test_get_api_key_parses_json_secret():
    assert lf._get_api_key(_FakeSM(json.dumps({"api_key": "BSK-123"}))) == "BSK-123"


def test_get_api_key_accepts_plain_string_secret():
    assert lf._get_api_key(_FakeSM("BSK-plain")) == "BSK-plain"


def test_get_api_key_falls_back_to_sole_json_value():
    # Secret stored as a single-key JSON object with a non-standard key name.
    assert lf._get_api_key(_FakeSM(json.dumps({"BraveSearchAPI": "BSK-sole"}))) == "BSK-sole"


def test_get_api_key_is_cached():
    assert lf._get_api_key(_FakeSM(json.dumps({"api_key": "FIRST"}))) == "FIRST"
    assert lf._get_api_key(_FakeSM(json.dumps({"api_key": "SECOND"}))) == "FIRST"


def test_handler_missing_query_returns_error():
    out = lf.lambda_handler({"arguments": {}}, sm_client=_FakeSM("K"))
    assert out["status"] == "error"
    assert "query" in out["message"]


def test_handler_happy_path(monkeypatch):
    captured = {}

    def fake_search(query, api_key, count=5, freshness=None):
        captured.update(query=query, api_key=api_key, count=count)
        return {"status": "success", "results": [], "query": query}

    monkeypatch.setattr(lf, "search", fake_search)
    out = lf.lambda_handler(
        {"query": "aws news", "count": 3},
        sm_client=_FakeSM(json.dumps({"api_key": "KEY"})),
    )
    assert out["status"] == "success"
    assert captured["query"] == "aws news"
    assert captured["api_key"] == "KEY"
    assert captured["count"] == 3
