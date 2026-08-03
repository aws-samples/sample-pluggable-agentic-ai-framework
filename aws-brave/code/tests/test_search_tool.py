import ast
import os

import search_tool as st

_AGENT = os.path.join(os.path.dirname(__file__), "..", "research_agent", "research_agent_a2a.py")


def test_build_search_args_happy():
    args, err = st.build_search_args("latest aws news", count=5)
    assert err is None
    assert args["query"] == "latest aws news"
    assert args["count"] == 5


def test_build_search_args_rejects_empty():
    args, err = st.build_search_args("   ")
    assert args is None
    assert err


def test_build_search_args_clamps_count():
    args, _ = st.build_search_args("q", count=100)
    assert args["count"] == st.MAX_COUNT


def test_build_search_args_includes_freshness_when_set():
    args, _ = st.build_search_args("q", freshness="pw")
    assert args["freshness"] == "pw"


def test_tool_schema_is_valid_and_matches_gateway_name():
    schemas = st.load_tool_schemas()
    assert isinstance(schemas, list) and len(schemas) == 1
    schema = schemas[0]
    assert schema["name"] == "web_search"
    assert schema["inputSchema"]["required"] == ["query"]
    assert "query" in schema["inputSchema"]["properties"]
    assert st.GATEWAY_TOOL_NAME.endswith("___web_search")


def test_agent_source_compiles_and_wires_web_search():
    src = open(_AGENT).read()
    compile(src, _AGENT, "exec")  # raises SyntaxError on failure
    tree = ast.parse(src)
    func_names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "web_search" in func_names
    assert "build_search_args" in src
    assert "brave-web-search___web_search" in src or "GATEWAY_TOOL_NAME" in src
