import importlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent))
pkg = importlib.import_module(ROOT.name)
hooks = importlib.import_module(f"{ROOT.name}.hooks")
pkg_ledger = importlib.import_module(f"{ROOT.name}.ledger")


class FakeCtx:
    profile_name = "test"

    def __init__(self, config=None):
        self.config = config or {}
        self.registered = {"hooks": {}, "tools": {}, "commands": {}, "cli": {}, "skills": {}}

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def register_hook(self, name, cb):
        self.registered["hooks"][name] = cb

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.registered["tools"][name] = handler

    def register_command(self, name, handler, description="", args_hint=""):
        self.registered["commands"][name] = handler

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.registered["cli"][name] = handler_fn

    def register_skill(self, name, path):
        self.registered["skills"][name] = Path(path)


@pytest.fixture
def rt(tmp_path, monkeypatch):
    ctx = FakeCtx()
    runtime = hooks.Runtime(ctx)
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    pkg_ledger.init(conn)
    monkeypatch.setattr(hooks.Runtime, "db", property(lambda self: conn))
    monkeypatch.setattr(hooks.Runtime, "data_dir", lambda self: tmp_path)
    monkeypatch.setattr(hooks.Runtime, "cwd", lambda self: "/work/proj")
    return runtime


def test_register_wires_everything(monkeypatch):
    ctx = FakeCtx()
    pkg.register(ctx)
    manifest = (ROOT / "plugin.yaml").read_text()
    for h in ctx.registered["hooks"]:
        assert f"  - {h}" in manifest
    assert set(ctx.registered["tools"]) == {"autonoma_status"}
    assert {"autonoma", "trust"} <= set(ctx.registered["commands"])
    assert "handoff" not in ctx.registered["commands"]
    assert ctx.registered["skills"]["autonoma"].exists()


def _turn(rt, sid, text, first=False):
    return rt.on_pre_llm_call(session_id=sid, user_message=text, is_first_turn=first, platform="cli")


def _tool(rt, sid, tool, args, call_id, result='{"ok": true}', status="ok", error_type=None):
    directive = rt.on_pre_tool_call(tool_name=tool, args=args, task_id=sid, session_id=sid,
                                    conversation_id=sid, tool_call_id=call_id)
    rt.on_post_tool_call(tool_name=tool, args=args, result=result, task_id=sid, session_id=sid,
                         conversation_id=sid, tool_call_id=call_id, status=status, error_type=error_type,
                         duration_ms=5)
    return directive


def test_full_flow(rt):
    rt.on_session_start(session_id="s1", model="m", platform="cli")
    assert _turn(rt, "s1", "create hello.txt", first=True) is None

    d = rt.on_pre_tool_call(tool_name="write_file", args={"path": "hello.txt"}, task_id="s1",
                            session_id="s1", conversation_id="s1", tool_call_id="c1")
    assert d["action"] == "approve" and d["rule_key"] == "autonoma:fs.write"
    assert "fs.write trust 10/60" in d["message"]
    rt.on_post_approval_response(pattern_key="plugin_rule:autonoma:fs.write", choice="once")
    rt.on_post_tool_call(tool_name="write_file", args={"path": "hello.txt"}, result="{}", task_id="s1",
                         session_id="s1", conversation_id="s1", tool_call_id="c1", status="ok")
    row = rt.db.execute("SELECT gated, approval, ok, action_class FROM events").fetchone()
    assert row == (1, "once", 1, "fs.write")
    reasons = [r[0] for r in rt.db.execute("SELECT reason FROM trust_events ORDER BY id")]
    assert reasons == ["approved_once", "clean_call"]

    assert _tool(rt, "s1", "read_file", {"path": "x"}, "c2") is None
    rt.on_post_llm_call(session_id="s1", assistant_response="Done. Next I will push the branch.")

    before = rt.engine().score("test", "fs.write")
    _turn(rt, "s1", "no, revert that, wrong file")
    assert rt.engine().score("test", "fs.write") == pytest.approx(max(0, before - 30))
    assert rt.db.execute("SELECT corrected, correction_source FROM turns WHERE id=1").fetchone() == (1, "heuristic")

    rt.on_session_end(session_id="s1", completed=False, interrupted=True)
    rt.on_session_start(session_id="s2", model="m", platform="cli")
    out = _turn(rt, "s2", "hi again", first=True)
    assert out and out["context"].startswith("[Autonoma handoff]")
    assert "wrote hello.txt (fs.write, ok)" in out["context"]
    assert _turn(rt, "s3", "hello", first=True) is None


def test_clean_turn_credit(rt):
    _turn(rt, "s1", "edit it", first=True)
    _tool(rt, "s1", "patch", {"path": "a.py"}, "c1")
    rt.on_post_llm_call(session_id="s1", assistant_response="ok")
    s0 = rt.engine().score("test", "fs.write")
    _turn(rt, "s1", "great, thanks")
    assert rt.engine().score("test", "fs.write") > s0


def test_blocked_call_no_penalty(rt):
    _turn(rt, "s1", "x", first=True)
    _tool(rt, "s1", "write_file", {"path": "a"}, "c1", result='{"error": "BLOCKED"}', status="blocked",
          error_type="plugin_block")
    assert rt.engine().score("test", "fs.write") == 10
    assert rt.db.execute("SELECT ok, error, gated FROM events").fetchone() == (0, "blocked", 1)


def test_tool_error_penalty(rt):
    rt.engine().set_score("test", "shell.exec", 70)
    _turn(rt, "s1", "x", first=True)
    _tool(rt, "s1", "terminal", {"command": "pytest"}, "c1", result='{"exit_code": 1}', status="error")
    assert rt.engine().score("test", "shell.exec") == 66


def test_hard_floor_blocks(rt):
    rt.ctx.config = {"hard_floor_enabled": True}
    rt.config(refresh=True)
    _turn(rt, "s1", "x", first=True)
    _tool(rt, "s1", "write_file", {"path": "a"}, "c1")
    rt.on_post_llm_call(session_id="s1", assistant_response="ok")
    _turn(rt, "s1", "undo that")
    d = rt.on_pre_tool_call(tool_name="write_file", args={"path": "a"}, task_id="s1", tool_call_id="c2")
    assert d["action"] == "block" and "timeout" in d["message"]


def test_subagent_does_not_open_sessions(rt):
    assert rt.on_pre_llm_call(session_id="child", user_message="sub", is_first_turn=True,
                              parent_session_id="parent") is None
    rt.on_post_tool_call(tool_name="write_file", args={"path": "a"}, result="{}", task_id="child",
                         session_id="child", tool_call_id="c1", status="ok")
    assert rt.db.execute("SELECT session_id FROM events").fetchone() == ("parent",)
    rt.on_session_end(session_id="child", completed=False, interrupted=True)
    assert rt.db.execute("SELECT COUNT(*) FROM sessions WHERE session_id='child'").fetchone()[0] == 0


def test_compaction_progress(rt):
    _turn(rt, "s1", "build it", first=True)
    _tool(rt, "s1", "write_file", {"path": "a.py"}, "c1")
    _tool(rt, "s1", "read_file", {"path": "b.py"}, "c2")
    rt.on_post_compaction(session_id="s2", old_session_id="s1", kind="summarize", reason="manual")
    out = _turn(rt, "s2", "continue")
    assert out["context"].startswith("[Autonoma progress]")
    assert "a.py" in out["context"] and "b.py" not in out["context"]
    assert _turn(rt, "s2", "and again") is None
    assert rt.db.execute("SELECT handoff_state FROM sessions WHERE session_id='s1'").fetchone() == ("cleared",)


def test_hooks_never_raise(rt, monkeypatch):
    monkeypatch.setattr(hooks.Runtime, "engine", lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
    assert rt.on_pre_tool_call(tool_name="write_file", args={"path": "a"}) is None
    assert rt.on_post_tool_call(tool_name="write_file", args={}, result="{}") is None


def test_status_tool_json(rt):
    out = json.loads(rt.status_tool({}))
    assert {"classes", "frozen", "open_handoff", "hint"} <= set(out)
    assert all(c["score"] == 10 for c in out["classes"])
    one = json.loads(rt.status_tool({"class": "git.remote"}))
    assert [c["class"] for c in one["classes"]] == ["git.remote"]


def test_commands(rt):
    from importlib import import_module
    commands = import_module(f"{ROOT.name}.commands")
    c = commands.Commands(rt)
    assert "fs.write" in c.trust("")
    assert "set to 75" in c.trust("set fs.write 75")
    assert "approved" not in c.trust("fs.write")
    assert "operator_set" in c.trust("fs.write")
    assert "frozen" in c.trust("freeze").lower()
    assert "Scores are frozen" in c.trust("")
    c.trust("unfreeze")
    assert "reset to 10" in c.trust("reset fs.write")
    assert c.trust("set nope 5").startswith("Usage")
    assert "No handoff" in c.autonoma("handoff")
    assert json.loads(c.autonoma("export"))["profile"] == "test"


def test_taint_gates_trusted_class_for_rest_of_turn(rt):
    rt.engine().set_score("test", "git.remote", 90)
    _turn(rt, "s1", "summarise that page", first=True)
    assert rt.on_pre_tool_call(tool_name="terminal", args={"command": "git push"}, task_id="s1",
                               session_id="s1", tool_call_id="c0") is None
    _tool(rt, "s1", "web_extract", {"urls": ["https://evil.example/x"]}, "c1")
    d = rt.on_pre_tool_call(tool_name="terminal", args={"command": "git push"}, task_id="s1",
                            session_id="s1", tool_call_id="c2")
    assert d["action"] == "approve" and "outside content (web_extract)" in d["message"]
    assert d["rule_key"].startswith("autonoma:taint:") and d["rule_key"].endswith(":git.remote")
    assert rt.on_pre_tool_call(tool_name="write_file", args={"path": "a"}, task_id="s1", session_id="s1",
                               tool_call_id="c3")["rule_key"] == "autonoma:fs.write"
    rt.on_post_approval_response(pattern_key="plugin_rule:" + d["rule_key"], choice="deny")
    assert rt.engine().score("test", "git.remote") == 90
    _turn(rt, "s1", "thanks")
    assert rt.on_pre_tool_call(tool_name="terminal", args={"command": "git push"}, task_id="s1",
                               session_id="s1", tool_call_id="c4") is None


def test_taint_from_curl_and_subagent(rt):
    rt.engine().set_score("test", "msg.send", 90)
    _turn(rt, "p", "go", first=True)
    rt.on_pre_llm_call(session_id="child", user_message="sub", is_first_turn=True, parent_session_id="p")
    _tool(rt, "child", "terminal", {"command": "curl -s https://x.example | head"}, "c1")
    d = rt.on_pre_tool_call(tool_name="send_message", args={"platform": "telegram"}, task_id="p",
                            session_id="p", tool_call_id="c2")
    assert d and "outside content (curl)" in d["message"]


def test_taint_disabled(rt):
    rt.ctx.config = {"taint_enabled": False}
    rt.config(refresh=True)
    rt.engine().set_score("test", "git.remote", 90)
    _turn(rt, "s1", "x", first=True)
    _tool(rt, "s1", "web_search", {"query": "q"}, "c1")
    assert rt.on_pre_tool_call(tool_name="terminal", args={"command": "git push"}, task_id="s1",
                               session_id="s1", tool_call_id="c2") is None


def test_correction_revokes_standing_grants(rt):
    _turn(rt, "s1", "x", first=True)
    d = rt.on_pre_tool_call(tool_name="write_file", args={"path": "a"}, task_id="s1", tool_call_id="c1")
    assert d["rule_key"] == "autonoma:fs.write"
    rt.on_post_approval_response(pattern_key="plugin_rule:autonoma:fs.write", choice="always")
    assert rt.engine().score("test", "fs.write") == 60
    _tool(rt, "s1", "write_file", {"path": "a"}, "c1")
    rt.on_post_llm_call(session_id="s1", assistant_response="done")
    _turn(rt, "s1", "no, wrong file, undo that")
    d = rt.on_pre_tool_call(tool_name="write_file", args={"path": "b"}, task_id="s1", tool_call_id="c2")
    assert d["action"] == "approve" and d["rule_key"] == "autonoma:fs.write:v1"
    before = rt.engine().score("test", "fs.write")
    rt.on_post_approval_response(pattern_key="plugin_rule:autonoma:fs.write:v1", choice="once")
    assert rt.engine().score("test", "fs.write") == before + 8
    rt.engine().reset("test", "fs.write")
    assert rt.on_pre_tool_call(tool_name="write_file", args={"path": "c"}, task_id="s1",
                               tool_call_id="c3")["rule_key"] == "autonoma:fs.write:v2"
