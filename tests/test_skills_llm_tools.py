"""Unit tests for skills registry, LLM provider layer, and agent tools."""
import asyncio

import pytest

from zax import config, llm, skills, tools


# ---------------------------------------------------------------- skills

def test_skill_registry_complete():
    assert len(skills.SKILLS) >= 16
    keys = {s["key"] for s in skills.SKILLS}
    for must in ("coder", "marketer", "researcher", "ops", "designer", "finance"):
        assert must in keys
    # every pack is well-formed
    for s in skills.SKILLS:
        assert s["name"] and s["title"] and s["persona"] and s["keywords"]


def test_skill_matching():
    assert skills.match_skill("write python code to parse json")["key"] == "coder"
    assert skills.match_skill("plan a marketing campaign launch")["key"] == "marketer"
    assert skills.match_skill("seo keyword research")["key"] == "seo"
    assert skills.match_skill("design the user interface flow")["key"] == "designer"
    assert skills.match_skill("zzzz qqqq nothing") is None


def test_by_category_groups():
    cats = skills.by_category()
    assert "Engineering" in cats and "Marketing" in cats
    assert all(isinstance(v, list) and v for v in cats.values())


# ---------------------------------------------------------------- llm

def test_extract_json_balanced():
    assert llm.extract_json('noise {"a": 1} tail') == {"a": 1}
    assert llm.extract_json('x {"a": {"b": [1,2]}} y') == {"a": {"b": [1, 2]}}
    assert llm.extract_json("no json here") is None
    assert llm.extract_json("{broken json") is None


def test_extract_json_tolerates_newlines_and_inner_braces():
    # unescaped newline inside a string (common in LLM {"final": "..."} output)
    got = llm.extract_json('{"final": "line one\nline two"}')
    assert got and got["final"] == "line one\nline two"
    # braces inside the string don't break brace-matching
    got2 = llm.extract_json('{"final": "use {} for an empty dict"}')
    assert got2 and "{}" in got2["final"]
    # fenced ```json block
    got3 = llm.extract_json('```json\n{"tool": "web_search", "args": {"query": "x"}}\n```')
    assert got3 and got3["tool"] == "web_search"


def test_provider_registry_and_resolution():
    assert "claude-cli" in llm.PROVIDERS and "deepseek" in llm.PROVIDERS and "moonshot" in llm.PROVIDERS
    # mock is forced in tests
    assert llm.resolve_provider() == "mock"
    assert llm.model_for("mock") == "zax-mock-1"


def test_mock_chat_is_deterministic():
    text1, tok1 = asyncio.run(llm.chat("system", [{"role": "user", "content": "hello"}]))
    text2, _ = asyncio.run(llm.chat("system", [{"role": "user", "content": "hello"}]))
    assert text1 == text2 and isinstance(text1, str) and tok1 == 0


def test_mock_review_returns_score_json():
    text, _ = asyncio.run(llm.chat(
        "You are performing a performance review. Respond with JSON only with a score.",
        [{"role": "user", "content": "deliverable"}]))
    parsed = llm.extract_json(text)
    assert parsed and "score" in parsed


# ---------------------------------------------------------------- tools: security

def test_workspace_jail_blocks_traversal():
    for bad in ["../../../etc/passwd", "/etc/passwd", "a/../../outside"]:
        with pytest.raises(ValueError):
            tools._safe_path(bad)
    # legitimate path is allowed
    p = tools._safe_path("notes/a.txt")
    assert str(p).endswith("workspace/notes/a.txt")


def test_ssrf_guard_blocks_private_and_metadata():
    for bad in ["http://127.0.0.1/x", "http://localhost/x", "http://169.254.169.254/meta",
                "http://10.0.0.1/x", "http://192.168.1.1/x", "ftp://example.com/x"]:
        with pytest.raises(ValueError):
            tools._assert_public_url(bad)


def test_shell_disabled_by_default():
    out = asyncio.run(tools.run("shell", {"command": "echo hi; rm -rf /"}))
    assert "disabled" in out.lower() and "hi" not in out


def test_run_code_disabled_by_default():
    out = asyncio.run(tools.run("run_code", {"code": "print('escaped')"}))
    assert "disabled" in out.lower()


def test_file_tools_roundtrip_and_list(monkeypatch):
    assert "Wrote" in tools.write_file("d/a.txt", "hello zax")
    assert tools.read_file("d/a.txt") == "hello zax"
    listing = tools.list_files(".")
    assert "d" in listing


def test_unknown_tool():
    out = asyncio.run(tools.run("nonexistent", {}))
    assert "Unknown tool" in out
