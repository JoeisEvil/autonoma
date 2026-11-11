import importlib
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent))
insights = importlib.import_module(f"{ROOT.name}.insights")
pl = importlib.import_module(f"{ROOT.name}.ledger")
pt = importlib.import_module(f"{ROOT.name}.trust")
ptax = importlib.import_module(f"{ROOT.name}.taxonomy")

P = "default"
T0 = 1_800_000_000.0


def _db():
    conn = sqlite3.connect(":memory:")
    pl.init(conn)
    return conn


def _ev(db, tool, cls, summary, ts, sid="s1", ok=True, error=None, gated=False, approval=None, flags=None):
    return pl.insert_event(db, profile=P, session_id=sid, tool=tool, action_class=cls, args={"s": summary, "t": ts},
                           args_summary=summary, ok=ok, error=error, gated=gated, approval=approval, ts=ts,
                           flags=flags)


def test_outcomes_and_stats():
    db = _db()
    _ev(db, "read_file", "read", "a.py", T0)
    _ev(db, "write_file", "fs.write", "a.py", T0 + 1)
    _ev(db, "write_file", "fs.write", "b.py", T0 + 2, gated=True, approval="once")
    _ev(db, "terminal", "fs.delete", "rm x", T0 + 3, ok=False, error="blocked", gated=True)
    _ev(db, "terminal", "shell.exec", "pytest", T0 + 4, ok=False, error="exit 1")
    evs = insights._events(db, P, 0)
    assert [e["outcome"] for e in evs] == ["read", "auto", "approved", "stopped", "error"]
    s = insights.stats(evs)
    assert s == {"actions": 4, "reads": 1, "auto": 1, "approved": 1, "stopped": 1, "errors": 1, "autonomy_pct": 25}


def test_incidents():
    db = _db()
    _ev(db, "write_file", "fs.write", "hello.txt", T0, ok=False, error="blocked", gated=True)
    _ev(db, "read_file", "read", "x", T0 + 5)
    _ev(db, "terminal", "fs.write", "echo > hello.txt", T0 + 10, ok=False, error="blocked", gated=True)
    _ev(db, "terminal", "fs.delete", "rm a", T0 + 20, sid="s2", ok=False, error="blocked", gated=True,
        flags="taint:curl")
    _ev(db, "skill_manage", "system", "keys: operations", T0 + 30, sid="s2")
    _ev(db, "terminal", "git.remote", "git push", T0 + 40, sid="s3", ok=False, error="blocked", gated=True,
        approval="deny")
    _ev(db, "terminal", "git.remote", "git push", T0 + 50, sid="s3", ok=False, error="blocked", gated=True)
    _ev(db, "terminal", "fs.write", "late", T0 + 400, sid="s3")
    kinds = [(i["kind"], i["detail"]) for i in insights.incidents(insights._events(db, P, 0))]
    assert ("workaround", "hello.txt  →  echo > hello.txt") in kinds
    assert ("outside_content", "rm a  (after curl)") in kinds
    assert ("self_edit", "keys: operations") in kinds
    assert ("denied", "git push") in kinds
    assert ("workaround", "rm a  →  keys: operations") in kinds
    assert not any(k == "workaround" and "late" in d for k, d in kinds)
    assert sum(1 for k, _ in kinds if k == "workaround") == 2
    assert ("retry", "git push") in kinds


def test_build_state_crossings_and_json():
    db = _db()
    engine = pt.TrustEngine(db, {}, clock=lambda: T0)
    engine.set_score(P, "fs.write", 59)
    engine.apply(P, "fs.write", "clean_call")
    engine.apply(P, "fs.write", "denied")
    _ev(db, "write_file", "fs.write", "a", T0)
    state = insights.build_state(db, engine, P, now=T0 + 10, window_s=3600, taxonomy=ptax.Taxonomy.load())
    json.dumps(state)
    assert [c["class"] for c in state["classes"]][0] == "system"
    fw = next(c for c in state["classes"] if c["class"] == "fs.write")
    assert fw["history"][-3:] == [59, 60.6, 35.6]
    kinds = [i["kind"] for i in state["incidents"]]
    assert "autonomy" in kinds and "demoted" in kinds
    assert state["handoff"] is None and state["stats"]["actions"] == 1


def test_flags_column_migrates_old_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(pl.SCHEMA.replace("  approval TEXT\n);", "  approval TEXT\n);", 1))
    conn.execute("ALTER TABLE events RENAME TO events_old")
    conn.execute("CREATE TABLE events AS SELECT * FROM events_old WHERE 0")
    conn.execute("DROP TABLE events_old")
    assert "flags" not in {r[1] for r in conn.execute("PRAGMA table_info(events)")}
    pl.init(conn)
    assert "flags" in {r[1] for r in conn.execute("PRAGMA table_info(events)")}


def test_workaround_needs_same_turn():
    db = _db()
    _ev(db, "terminal", "fs.delete", "rm a", T0, ok=False, error="blocked", gated=True, approval="deny")
    _ev(db, "patch", "fs.write", "b.py", T0 + 60)
    evs = insights._events(db, P, 0)
    assert any(i["kind"] == "workaround" for i in insights.incidents(evs))
    assert not any(i["kind"] in ("workaround", "retry")
                   for i in insights.incidents(evs, turn_starts={"s1": [T0 + 30]}))
