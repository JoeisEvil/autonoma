import json

import pytest

import ledger

OK_RESULTS = [
    json.dumps({"output": "hello", "exit_code": 0, "error": None}),
    json.dumps({"success": True, "path": "a.py"}),
    json.dumps({"content": "line 1\nline 2", "total_lines": 2}),
    json.dumps({"results": [], "error": ""}),
    "Wrote 120 bytes to hello.txt",
    "all tests passed",
    "",
    None,
    json.dumps([1, 2, 3]),
    "The word error appears mid-sentence, which is fine.",
]

ERROR_RESULTS = [
    json.dumps({"error": "File not found: a.py"}),
    json.dumps({"output": "2 failed, 10 passed", "exit_code": 1, "error": None}),
    json.dumps({"success": False, "message": "patch did not apply"}),
    json.dumps({"exit_code": 127, "stderr": "command not found"}),
    "Error: permission denied",
    "Traceback (most recent call last):\n  File x",
    "Exception: boom",
    "  error: something",
    "Command failed with exit code 2",
    "process exited with status 1",
]


@pytest.mark.parametrize("result", OK_RESULTS)
def test_ok_results(result):
    ok, err = ledger.derive_ok(result)
    assert ok, result
    assert err is None


@pytest.mark.parametrize("result", ERROR_RESULTS)
def test_error_results(result):
    ok, err = ledger.derive_ok(result)
    assert not ok, result
    assert err and len(err) <= 200


def test_fixture_size():
    assert len(OK_RESULTS) + len(ERROR_RESULTS) >= 20


def test_error_excerpt_capped():
    ok, err = ledger.derive_ok(json.dumps({"error": "x" * 1000}))
    assert not ok and len(err) == 200


def test_args_hash_canonical():
    assert ledger.args_hash({"a": 1, "b": 2}) == ledger.args_hash({"b": 2, "a": 1})
    assert ledger.args_hash({"a": 1}) != ledger.args_hash({"a": 2})


def test_insert_and_prune(db, clock):
    old = clock() - 100 * 86400
    ledger.insert_event(db, profile="p", tool="terminal", action_class="shell.exec", args={},
                        args_summary="ls", ok=True, ts=old)
    ledger.insert_event(db, profile="p", tool="terminal", action_class="shell.exec", args={},
                        args_summary="ls", ok=True, ts=clock())
    ledger.start_turn(db, profile="p", conversation_id="c", session_id="s", user_message="hi", ts=old)
    assert ledger.prune(db, 90, now=clock()) == 2
    assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_gate_tracker_matches_within_window(clock):
    g = ledger.GateTracker()
    g.add("t1", "write_file", {"path": "a"}, "fs.write", now=clock())
    assert g.match("t1", "write_file", {"path": "b"}, now=clock()) is None
    assert g.match("t2", "write_file", {"path": "a"}, now=clock()) is None
    m = g.match("t1", "write_file", {"path": "a"}, now=clock() + 30)
    assert m and m.action_class == "fs.write"
    assert g.match("t1", "write_file", {"path": "a"}, now=clock() + 31) is None


def test_gate_tracker_expires(clock):
    g = ledger.GateTracker()
    g.add("t1", "write_file", {"path": "a"}, "fs.write", now=clock())
    assert g.match("t1", "write_file", {"path": "a"}, now=clock() + 61) is None


def test_gate_tracker_drain(clock):
    g = ledger.GateTracker()
    g.add("t1", "terminal", {"command": "rm x"}, "fs.delete", now=clock())
    g.add("t2", "terminal", {"command": "rm y"}, "fs.delete", now=clock())
    assert [p.task_id for p in g.drain("t1")] == ["t1"]
    assert len(g.drain()) == 1


def test_turns_and_classes(db, clock):
    t = ledger.start_turn(db, profile="p", conversation_id="c", session_id="s", user_message="x" * 500, ts=clock())
    ledger.insert_event(db, profile="p", conversation_id="c", session_id="s", tool="write_file",
                        action_class="fs.write", args={}, args_summary="a", ok=True, ts=clock() + 1)
    classes = ledger.classes_since(db, profile="p", conversation_id="c", session_id="s", since_ts=clock())
    ledger.set_turn_classes(db, t, classes)
    row = ledger.last_turn(db, profile="p", conversation_id="c", session_id="s")
    assert json.loads(row[2]) == ["fs.write"]
    assert len(db.execute("SELECT user_excerpt FROM turns").fetchone()[0]) == 200
    assert ledger.turn_counter(db, "p") == t


def test_gate_tracker_prefers_call_id(clock):
    g = ledger.GateTracker()
    g.add("t1", "write_file", {"path": "a"}, "fs.write", now=clock(), call_id="call-1")
    assert g.match("t1", "write_file", {"path": "a", "modified": True}, now=clock(), call_id="call-2") is None
    assert g.match("t1", "write_file", {"path": "changed"}, now=clock(), call_id="call-1") is not None


def test_gate_tracker_records_approval(clock):
    g = ledger.GateTracker()
    g.add("t1", "write_file", {"path": "a"}, "fs.write", now=clock(), call_id="c1")
    assert g.set_approval("fs.write", "once")
    assert not g.set_approval("git.remote", "once")
    assert g.match("t1", "write_file", {}, now=clock(), call_id="c1").approval == "once"
