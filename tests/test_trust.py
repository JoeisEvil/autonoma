import pytest

from trust import TrustEngine, calls_to_autonomy

P = "default"


@pytest.fixture
def eng(db, clock):
    return TrustEngine(db, {}, clock=clock)


def test_start_score_on_first_sight(eng, db):
    assert eng.score(P, "fs.write") == 10
    assert db.execute("SELECT COUNT(*) FROM trust").fetchone()[0] == 1


def test_read_never_gated(eng):
    assert eng.decide(P, "read").directive() is None


def test_gain_saturates(eng):
    for _ in range(1000):
        s = eng.apply(P, "fs.write", "clean_call")
    assert s <= 100
    assert s > 99


def test_denied_then_approved_nets_negative(eng):
    start = eng.score(P, "git.remote")
    eng.apply(P, "git.remote", "denied")
    eng.apply(P, "git.remote", "approved_once")
    assert eng.score(P, "git.remote") < start


def test_clamped_at_zero(eng):
    for _ in range(5):
        eng.apply(P, "fs.delete", "denied")
    assert eng.score(P, "fs.delete") == 0


def test_every_apply_logs(eng, db):
    eng.apply(P, "fs.write", "clean_call")
    eng.apply(P, "fs.write", "tool_error")
    rows = db.execute("SELECT reason FROM trust_events ORDER BY id").fetchall()
    assert [r[0] for r in rows] == ["clean_call", "tool_error"]


def test_approved_always_lifts_to_threshold(eng):
    eng.apply(P, "fs.write", "approved_always")
    assert eng.score(P, "fs.write") == 60
    assert eng.decide(P, "fs.write").directive() is None


def test_corrected_hits_every_touched_class(eng):
    for c in ("fs.write", "git.local"):
        eng.set_score(P, c, 70)
    eng.apply_correction(P, ["fs.write", "git.local", "read"], current_turn=5)
    assert eng.score(P, "fs.write") == 40
    assert eng.score(P, "git.local") == 40


def test_gate_approve_message(eng):
    d = eng.decide(P, "fs.write", "wrote hello.txt").directive()
    assert d["action"] == "approve"
    assert d["rule_key"] == "autonoma:fs.write"
    assert "fs.write trust 10/60" in d["message"]
    assert "wrote hello.txt" in d["message"]
    assert "clean writes to autonomy" in d["message"]


def test_silent_at_threshold(eng):
    eng.set_score(P, "fs.write", 60)
    assert eng.decide(P, "fs.write").directive() is None


def test_disabled_never_gates(db, clock):
    eng = TrustEngine(db, {"enabled": False}, clock=clock)
    assert eng.decide(P, "system").directive() is None


def test_threshold_config_override(db, clock):
    eng = TrustEngine(db, {"thresholds": {"fs.write": 5}}, clock=clock)
    assert eng.decide(P, "fs.write").directive() is None
    assert eng.threshold(P, "fs.delete") == 80


def test_hard_floor_timeout_blocks(db, clock):
    eng = TrustEngine(db, {"hard_floor_enabled": True}, clock=clock)
    eng.set_score(P, "fs.write", 30)
    eng.apply_correction(P, ["fs.write"], current_turn=3)
    d = eng.decide(P, "fs.write", current_turn=4).directive()
    assert d["action"] == "block"
    assert "9 turns left" in d["message"]
    assert eng.decide(P, "fs.write", current_turn=13).directive()["action"] == "approve"


def test_hard_floor_off_never_blocks(eng):
    eng.set_score(P, "fs.write", 30)
    eng.apply_correction(P, ["fs.write"], current_turn=3)
    assert eng.decide(P, "fs.write", current_turn=4).directive()["action"] == "approve"


def test_reset_clears_timeout(db, clock):
    eng = TrustEngine(db, {"hard_floor_enabled": True}, clock=clock)
    eng.apply_correction(P, ["fs.write"], current_turn=1)
    eng.reset(P, "fs.write")
    assert eng.decide(P, "fs.write", current_turn=2).directive()["action"] == "approve"
    assert eng.score(P, "fs.write") == 10


def test_decay_only_after_decay_days(eng, clock):
    eng.set_score(P, "fs.write", 80)
    eng.apply(P, "fs.write", "clean_call")
    s0 = eng.score(P, "fs.write")
    clock.advance(13 * 86400)
    assert eng.tick_decay(P) == {}
    clock.advance(3 * 86400)
    eng.tick_decay(P)
    assert eng.score(P, "fs.write") == pytest.approx(s0 - 2)
    eng.tick_decay(P)
    assert eng.score(P, "fs.write") == pytest.approx(s0 - 2)
    clock.advance(1000 * 86400)
    eng.tick_decay(P)
    assert eng.score(P, "fs.write") == 30


def test_decay_never_raises_low_scores(eng, clock):
    eng.apply(P, "fs.write", "clean_call")
    s0 = eng.score(P, "fs.write")
    clock.advance(100 * 86400)
    eng.tick_decay(P)
    assert eng.score(P, "fs.write") == s0


def test_frozen_blocks_all_deltas(eng):
    eng.set_frozen(P, True)
    for reason in ("clean_call", "denied", "corrected", "approved_always", "tool_error"):
        eng.apply(P, "fs.write", reason)
    assert eng.score(P, "fs.write") == 10
    assert eng.decide(P, "fs.write").directive()["action"] == "approve"
    eng.set_frozen(P, False)
    eng.apply(P, "fs.write", "clean_call")
    assert eng.score(P, "fs.write") > 10


def test_operator_set_works_while_frozen(eng):
    eng.set_frozen(P, True)
    eng.set_score(P, "fs.write", 75)
    assert eng.score(P, "fs.write") == 75


def _simulate(score, threshold, rate):
    n = 0
    while score < threshold and n < 99:
        score += (100 - score) * rate
        n += 1
    return n


@pytest.mark.parametrize("score", [0, 10, 25, 49.9, 59, 59.99])
@pytest.mark.parametrize("threshold", [50, 60, 70, 80, 99])
def test_calls_to_autonomy_matches_simulation(score, threshold):
    expected = _simulate(score, threshold, 0.04)
    got = calls_to_autonomy(score, threshold, 0.04)
    assert got == (expected if score < threshold else 0)


def test_calls_to_autonomy_caps():
    assert calls_to_autonomy(0, 100, 0.04) == 99
    assert calls_to_autonomy(70, 60) == 0


def test_status_lists_all_default_classes(eng):
    st = eng.status(P)
    assert {r["class"] for r in st} >= {"fs.write", "git.remote", "system"}
    assert all(r["score"] == 10 for r in st)
