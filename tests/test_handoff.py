import handoff
import ledger

P = "default"


def _event(db, sid, tool, cls, summary, ok=True, ts=0, error=None, conv="c1"):
    return ledger.insert_event(db, profile=P, conversation_id=conv, session_id=sid, tool=tool,
                               action_class=cls, args={"s": summary, "ts": ts}, args_summary=summary,
                               ok=ok, error=error, ts=ts)


def _session(db, sid, clock, cwd="/home/u/proj", conv="c1", offset=0):
    handoff.open_session(db, session_id=sid, profile=P, conversation_id=conv, cwd=cwd,
                         ts=clock() + offset)
    handoff.begin_turn(db, sid, "add rate limiting to the API", conv)


def test_read_only_session_produces_no_handoff(db, clock):
    _session(db, "s1", clock)
    _event(db, "s1", "read_file", "read", "a.py", ts=clock())
    assert handoff.close_session(db, "s1", completed=False, interrupted=True) == "none"
    clock.advance(600)
    assert handoff.find_open(db, P, "/home/u/proj", now=clock())[0] is None


def test_unfinished_session_opens_and_consumes_once(db, clock):
    _session(db, "s1", clock)
    _event(db, "s1", "write_file", "fs.write", "api/limiter.py", ts=clock())
    assert handoff.close_session(db, "s1", completed=False, interrupted=True) == "open"
    s, others = handoff.find_open(db, P, "/home/u/proj", now=clock() + 10)
    assert s["session_id"] == "s1" and others == 0
    handoff.consume(db, "s1")
    assert handoff.find_open(db, P, "/home/u/proj", now=clock() + 20)[0] is None


def test_completed_session_closes_stale_open(db, clock):
    _session(db, "s1", clock)
    _event(db, "s1", "write_file", "fs.write", "a", ts=clock())
    handoff.close_session(db, "s1", completed=False, interrupted=True)
    handoff.begin_turn(db, "s1", "next")
    assert handoff.close_session(db, "s1", completed=True, interrupted=False) == "none"


def test_crashed_session_without_end_hook_is_found(db, clock):
    _session(db, "s1", clock)
    _event(db, "s1", "write_file", "fs.write", "a", ts=clock())
    assert handoff.find_open(db, P, None, now=clock() + 30)[0] is None
    assert handoff.find_open(db, P, None, now=clock() + 300)[0] is None
    s, _ = handoff.find_open(db, P, None, now=clock() + 700)
    assert s["session_id"] == "s1"


def test_cwd_preferred_over_recency(db, clock):
    _session(db, "here", clock, cwd="/home/u/proj")
    _event(db, "here", "write_file", "fs.write", "a", ts=clock())
    handoff.close_session(db, "here", False, True)
    _session(db, "elsewhere", clock, cwd="/home/u/other", offset=100)
    _event(db, "elsewhere", "write_file", "fs.write", "b", ts=clock() + 100)
    handoff.close_session(db, "elsewhere", False, True)
    s, others = handoff.find_open(db, P, "/home/u/proj", now=clock() + 500)
    assert s["session_id"] == "here" and others == 1
    s, _ = handoff.find_open(db, P, "/nowhere", now=clock() + 500)
    assert s["session_id"] == "elsewhere"


def test_max_age(db, clock):
    _session(db, "s1", clock)
    _event(db, "s1", "write_file", "fs.write", "a", ts=clock())
    handoff.close_session(db, "s1", False, True)
    assert handoff.find_open(db, P, None, now=clock() + 73 * 3600, max_age_hours=72)[0] is None


def test_note_format_and_grouping(db, clock):
    _session(db, "s1", clock)
    t = clock()
    _event(db, "s1", "write_file", "fs.write", "api/limiter.py", ts=t)
    _event(db, "s1", "patch", "fs.write", "api/app.py", ts=t + 1)
    _event(db, "s1", "terminal", "shell.exec", "pytest", ok=False, error="2 failed", ts=t + 2)
    _event(db, "s1", "terminal", "shell.exec", "pytest", ts=t + 3)
    _event(db, "s1", "terminal", "shell.exec", "pytest", ts=t + 4)
    _event(db, "s1", "terminal", "read", "ls", ts=t + 5)
    _event(db, "s1", "terminal", "git.local", "git commit", ts=t + 6)
    handoff.set_last_plan(db, "s1", "Next I'll push the branch and open a PR.")
    handoff.close_session(db, "s1", False, True, ts=t + 7)
    s, _ = handoff.find_open(db, P, "/home/u/proj", now=t + 7200)
    note = handoff.build_note(s, ledger.events_for_session(db, "s1"), now=t + 7200)
    assert note.startswith("[Autonoma handoff] An earlier session in")
    assert "ended unfinished 1h ago" in note
    assert 'Task: "add rate limiting to the API"' in note
    assert "Done (from tool log, 6 actions):" in note
    assert "  • wrote api/limiter.py (fs.write, ok)" in note
    assert "  • edited api/app.py (fs.write, ok)" in note
    assert "  • ran `pytest` (shell.exec, error: 2 failed)" in note
    assert "  • ran `pytest` ×2 (shell.exec, ok)" in note
    assert "ls" not in note.split("Done")[1].split("Not done")[0]
    assert "no git.remote" in note
    assert note.rstrip().endswith("Otherwise ignore this note.")


def test_note_under_cap_with_200_events(db, clock):
    _session(db, "s1", clock)
    for i in range(200):
        _event(db, "s1", "write_file", "fs.write", f"src/module_{i}.py", ts=clock() + i)
    s = handoff.get_session(db, "s1")
    s["last_activity"] = clock() + 200
    note = handoff.build_note(s, ledger.events_for_session(db, "s1"), max_chars=3000, now=clock() + 300)
    assert len(note) <= 3000
    assert "… and" in note
    assert note.count("  • ") <= 25
    small = handoff.build_note(s, ledger.events_for_session(db, "s1"), max_chars=600, now=clock() + 300)
    assert len(small) <= 600


def test_absolute_time_after_a_day(db, clock):
    assert handoff.ago(clock(), now=clock() + 2 * 86400).startswith("on ")
    assert handoff.ago(clock(), now=clock() + 30) == "just now"
    assert handoff.ago(clock(), now=clock() + 600) == "10m ago"


def test_progress_excludes_read(db, clock):
    t = clock()
    _event(db, "s1", "read_file", "read", "a.py", ts=t)
    _event(db, "s1", "write_file", "fs.write", "b.py", ts=t + 1)
    note = handoff.build_progress(ledger.events_for_conversation(db, "c1"))
    assert "b.py" in note and "a.py" not in note
    assert len(note) <= 2000


def test_progress_empty_when_only_reads(db, clock):
    _event(db, "s1", "read_file", "read", "a.py", ts=clock())
    assert handoff.build_progress(ledger.events_for_conversation(db, "c1")) == ""


def test_last_plan_strips_code(db, clock):
    _session(db, "s1", clock)
    handoff.set_last_plan(db, "s1", "Here:\n```python\nsecret = 1\n```\nThen run tests.")
    assert handoff.get_session(db, "s1")["last_plan"] == "Here: Then run tests."


def test_clear(db, clock):
    _session(db, "s1", clock)
    _event(db, "s1", "write_file", "fs.write", "a", ts=clock())
    handoff.close_session(db, "s1", False, True)
    assert handoff.clear(db, profile=P) == 1
    assert handoff.find_open(db, P, None, now=clock() + 10)[0] is None


def test_continuing_a_session_closes_its_handoff(db, clock):
    _session(db, "s1", clock)
    _event(db, "s1", "write_file", "fs.write", "a", ts=clock())
    handoff.close_session(db, "s1", completed=False, interrupted=False)
    handoff.begin_turn(db, "s1", "keep going")
    assert handoff.get_session(db, "s1")["handoff_state"] == "none"
