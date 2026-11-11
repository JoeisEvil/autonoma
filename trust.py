import math
import time
from dataclasses import dataclass

DEFAULT_THRESHOLDS = {
    "fs.write": 60, "fs.delete": 80, "shell.exec": 60, "git.local": 50, "git.remote": 70,
    "net.outbound": 70, "msg.send": 70, "spend": 80, "system": 80,
}

DEFAULTS = {
    "enabled": True,
    "start_score": 10,
    "thresholds": DEFAULT_THRESHOLDS,
    "gain_rate": 0.04,
    "turn_gain_rate": 0.02,
    "approve_gain": 8,
    "deny_penalty": 25,
    "correction_penalty": 30,
    "error_penalty": 4,
    "decay_days": 14,
    "decay_floor": 30,
    "hard_floor_enabled": False,
    "hard_floor_score": 20,
    "hard_floor_turns": 10,
}

VERBS = {
    "fs.write": "writes", "fs.delete": "deletes", "shell.exec": "commands", "git.local": "git ops",
    "git.remote": "pushes", "net.outbound": "requests", "msg.send": "messages", "spend": "payments",
    "system": "system ops",
}

REASONS = {"clean_call", "clean_turn", "approved_once", "approved_session", "approved_always",
           "denied", "timeout", "corrected", "tool_error", "decay", "operator_set", "reset",
           "gate_unresolved"}
USE_REASONS = {"clean_call", "tool_error", "approved_once", "approved_session", "approved_always",
               "denied", "corrected"}
OPERATOR_REASONS = {"operator_set", "reset"}
REVOKING_REASONS = ("corrected", "reset")
DAY = 86400.0


@dataclass
class GateDecision:
    action: str | None
    action_class: str
    score: float
    threshold: float
    message: str = ""
    rule_key: str = ""

    def directive(self):
        if self.action == "block":
            return {"action": "block", "message": self.message}
        if self.action == "approve":
            return {"action": "approve", "message": self.message, "rule_key": self.rule_key}
        return None


def calls_to_autonomy(score, threshold, rate=0.04, cap=99) -> int:
    if score >= threshold:
        return 0
    if threshold >= 100 or rate <= 0:
        return cap
    n = math.ceil(math.log((100 - threshold) / (100 - score)) / math.log(1 - rate) - 1e-9)
    return max(1, min(cap, n))


def _clamp(x):
    return max(0.0, min(100.0, float(x)))


class TrustEngine:
    def __init__(self, db, config=None, clock=time.time):
        self.db = db
        self.config = {**DEFAULTS, **{k: v for k, v in (config or {}).items() if v is not None}}
        self.config["thresholds"] = {**DEFAULT_THRESHOLDS, **(self.config.get("thresholds") or {})}
        self.clock = clock

    def _row(self, profile, cls):
        row = self.db.execute(
            "SELECT score, threshold_override, frozen, timeout_until_turn, updated_ts, last_used_ts"
            " FROM trust WHERE profile=? AND action_class=?", (profile, cls)).fetchone()
        if row is None:
            now = self.clock()
            start = _clamp(self.config["start_score"])
            frozen = int(self.is_frozen(profile))
            self.db.execute(
                "INSERT OR IGNORE INTO trust (profile, action_class, score, frozen, updated_ts)"
                " VALUES (?,?,?,?,?)", (profile, cls, start, frozen, now))
            self.db.commit()
            return self._row(profile, cls)
        return dict(zip(("score", "threshold_override", "frozen", "timeout_until_turn",
                         "updated_ts", "last_used_ts"), row))

    def score(self, profile, cls) -> float:
        return self._row(profile, cls)["score"]

    def threshold(self, profile, cls) -> float:
        row = self._row(profile, cls)
        if row["threshold_override"] is not None:
            return float(row["threshold_override"])
        return float(self.config["thresholds"].get(cls, DEFAULT_THRESHOLDS.get(cls, 60)))

    def is_frozen(self, profile) -> bool:
        r = self.db.execute("SELECT MAX(frozen) FROM trust WHERE profile=?", (profile,)).fetchone()
        return bool(r and r[0])

    def in_timeout(self, profile, cls, current_turn) -> int:
        until = self._row(profile, cls)["timeout_until_turn"]
        return max(0, until - current_turn) if until and self.config["hard_floor_enabled"] else 0

    def decide(self, profile, cls, summary="", current_turn=0) -> GateDecision:
        if cls == "read":
            return GateDecision(None, cls, 100, 0)
        score, threshold = self.score(profile, cls), self.threshold(profile, cls)
        if not self.config["enabled"]:
            return GateDecision(None, cls, score, threshold)
        left = self.in_timeout(profile, cls, current_turn)
        if left:
            msg = (f"Autonoma: {cls} is in timeout after a correction ({left} turns left). "
                   f"Ask the user to run /trust reset {cls} or wait.")
            return GateDecision("block", cls, score, threshold, msg)
        if score >= threshold:
            return GateDecision(None, cls, score, threshold)
        n = calls_to_autonomy(score, threshold, self.config["gain_rate"])
        what = f" — {summary}" if summary else ""
        msg = (f"Autonoma · {cls} trust {score:.0f}/{threshold:.0f}{what}. "
               f"~{n} clean {VERBS.get(cls, 'calls')} to autonomy.")
        return GateDecision("approve", cls, score, threshold, msg, self.rule_key(profile, cls))

    def rule_key(self, profile, cls):
        epoch = self.db.execute(
            "SELECT COUNT(*) FROM trust_events WHERE profile=? AND action_class=? AND reason IN (?, ?)",
            (profile, cls, *REVOKING_REASONS)).fetchone()[0]
        return f"autonoma:{cls}" if not epoch else f"autonoma:{cls}:v{epoch}"

    def delta_for(self, reason, score, threshold, value=None) -> float:
        c = self.config
        if reason == "clean_call":
            return (100 - score) * c["gain_rate"]
        if reason == "clean_turn":
            return (100 - score) * c["turn_gain_rate"]
        if reason in ("approved_once", "approved_session"):
            return c["approve_gain"]
        if reason == "approved_always":
            return max(score, threshold) - score
        if reason == "denied":
            return -c["deny_penalty"]
        if reason == "corrected":
            return -c["correction_penalty"]
        if reason == "tool_error":
            return -c["error_penalty"]
        if reason in ("operator_set", "reset"):
            target = c["start_score"] if value is None else value
            return _clamp(target) - score
        return 0.0

    def apply(self, profile, cls, reason, ref_event_id=None, value=None) -> float:
        if cls == "read":
            return 100.0
        if reason not in REASONS:
            raise ValueError(f"unknown trust reason: {reason}")
        row = self._row(profile, cls)
        score = row["score"]
        now = self.clock()
        if row["frozen"] and reason not in OPERATOR_REASONS:
            return score
        delta = 0.0 if reason in ("timeout", "gate_unresolved") else self.delta_for(
            reason, score, self.threshold(profile, cls), value)
        after = _clamp(score + delta)
        delta = after - score
        last_used = now if reason in USE_REASONS else row["last_used_ts"]
        timeout = None if reason == "reset" else row["timeout_until_turn"]
        self.db.execute(
            "UPDATE trust SET score=?, updated_ts=?, last_used_ts=?, timeout_until_turn=?"
            " WHERE profile=? AND action_class=?", (after, now, last_used, timeout, profile, cls))
        self.db.execute(
            "INSERT INTO trust_events (ts, profile, action_class, delta, score_after, reason, ref_event_id)"
            " VALUES (?,?,?,?,?,?,?)", (now, profile, cls, delta, after, reason, ref_event_id))
        self.db.commit()
        return after

    def apply_correction(self, profile, classes, current_turn, ref_event_id=None):
        results = {}
        for cls in classes:
            if cls == "read":
                continue
            after = self.apply(profile, cls, "corrected", ref_event_id)
            results[cls] = after
            if self.config["hard_floor_enabled"] and after < self.config["hard_floor_score"] \
                    and not self._row(profile, cls)["frozen"]:
                self.db.execute(
                    "UPDATE trust SET timeout_until_turn=? WHERE profile=? AND action_class=?",
                    (current_turn + int(self.config["hard_floor_turns"]), profile, cls))
                self.db.commit()
        return results

    def tick_decay(self, profile):
        now = self.clock()
        decay_days, floor = self.config["decay_days"], self.config["decay_floor"]
        changed = {}
        rows = self.db.execute(
            "SELECT action_class, score, last_used_ts, frozen FROM trust WHERE profile=?",
            (profile,)).fetchall()
        for cls, score, last_used, frozen in rows:
            if frozen or last_used is None or score <= floor:
                continue
            idle_start = last_used + decay_days * DAY
            last_decay = self.db.execute(
                "SELECT MAX(ts) FROM trust_events WHERE profile=? AND action_class=? AND reason='decay'",
                (profile, cls)).fetchone()[0]
            since = max(idle_start, last_decay or 0)
            days = int((now - since) // DAY)
            if days <= 0:
                continue
            after = max(float(floor), score - days)
            self.db.execute("UPDATE trust SET score=?, updated_ts=? WHERE profile=? AND action_class=?",
                            (after, now, profile, cls))
            self.db.execute(
                "INSERT INTO trust_events (ts, profile, action_class, delta, score_after, reason)"
                " VALUES (?,?,?,?,?,'decay')", (since + days * DAY, profile, cls, after - score, after))
            changed[cls] = after
        self.db.commit()
        return changed

    def set_score(self, profile, cls, value):
        return self.apply(profile, cls, "operator_set", value=value)

    def reset(self, profile, cls=None):
        classes = [cls] if cls else self.known_classes(profile)
        return {c: self.apply(profile, c, "reset") for c in classes}

    def set_frozen(self, profile, frozen: bool, classes=None):
        for c in classes or self.known_classes(profile):
            self._row(profile, c)
        self.db.execute("UPDATE trust SET frozen=? WHERE profile=?", (int(frozen), profile))
        self.db.commit()

    def known_classes(self, profile):
        rows = self.db.execute("SELECT action_class FROM trust WHERE profile=?", (profile,)).fetchall()
        return sorted(set(self.config["thresholds"]) | {r[0] for r in rows})

    def status(self, profile, current_turn=0, classes=None):
        out = []
        for cls in classes or self.known_classes(profile):
            row = self._row(profile, cls)
            t = self.threshold(profile, cls)
            last = self.db.execute(
                "SELECT ts, delta, reason FROM trust_events WHERE profile=? AND action_class=?"
                " ORDER BY id DESC LIMIT 1", (profile, cls)).fetchone()
            out.append({
                "class": cls,
                "score": round(row["score"], 1),
                "threshold": t,
                "autonomous": row["score"] >= t,
                "calls_to_autonomy": calls_to_autonomy(row["score"], t, self.config["gain_rate"]),
                "in_timeout": bool(self.in_timeout(profile, cls, current_turn)),
                "turns_left": self.in_timeout(profile, cls, current_turn),
                "frozen": bool(row["frozen"]),
                "last_event": {"ts": last[0], "delta": round(last[1], 1), "reason": last[2]} if last else None,
            })
        return out

    def history(self, profile, cls, limit=10):
        rows = self.db.execute(
            "SELECT ts, delta, score_after, reason FROM trust_events WHERE profile=? AND action_class=?"
            " ORDER BY id DESC LIMIT ?", (profile, cls, limit)).fetchall()
        return [{"ts": r[0], "delta": round(r[1], 1), "score_after": round(r[2], 1), "reason": r[3]}
                for r in rows]

    def recent_events(self, profile, limit=30):
        rows = self.db.execute(
            "SELECT ts, action_class, delta, score_after, reason FROM trust_events WHERE profile=?"
            " ORDER BY id DESC LIMIT ?", (profile, limit)).fetchall()
        return [{"ts": r[0], "class": r[1], "delta": round(r[2], 1), "score_after": round(r[3], 1),
                 "reason": r[4]} for r in rows]
