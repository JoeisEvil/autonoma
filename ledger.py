import hashlib
import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  profile TEXT NOT NULL,
  conversation_id TEXT,
  session_id TEXT,
  cwd TEXT,
  tool TEXT NOT NULL,
  action_class TEXT NOT NULL,
  args_hash TEXT NOT NULL,
  args_summary TEXT,
  ok INTEGER NOT NULL,
  error TEXT,
  duration_ms INTEGER,
  gated INTEGER NOT NULL DEFAULT 0,
  approval TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_conv ON events(conversation_id, ts);
CREATE INDEX IF NOT EXISTS ix_events_class ON events(profile, action_class, ts);
CREATE INDEX IF NOT EXISTS ix_events_session ON events(session_id, ts);

CREATE TABLE IF NOT EXISTS turns (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  profile TEXT NOT NULL,
  conversation_id TEXT,
  session_id TEXT,
  user_excerpt TEXT,
  classes_touched TEXT,
  corrected INTEGER NOT NULL DEFAULT 0,
  correction_source TEXT
);
CREATE INDEX IF NOT EXISTS ix_turns_conv ON turns(conversation_id, id);

CREATE TABLE IF NOT EXISTS trust (
  profile TEXT NOT NULL,
  action_class TEXT NOT NULL,
  score REAL NOT NULL,
  threshold_override REAL,
  frozen INTEGER NOT NULL DEFAULT 0,
  timeout_until_turn INTEGER,
  updated_ts REAL NOT NULL,
  last_used_ts REAL,
  PRIMARY KEY (profile, action_class)
);

CREATE TABLE IF NOT EXISTS trust_events (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  profile TEXT NOT NULL,
  action_class TEXT NOT NULL,
  delta REAL NOT NULL,
  score_after REAL NOT NULL,
  reason TEXT NOT NULL,
  ref_event_id INTEGER
);
CREATE INDEX IF NOT EXISTS ix_trust_events_class ON trust_events(profile, action_class, ts);

CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  conversation_id TEXT,
  profile TEXT NOT NULL,
  cwd TEXT,
  platform TEXT,
  model TEXT,
  started_ts REAL NOT NULL,
  ended_ts REAL,
  completed INTEGER,
  interrupted INTEGER,
  original_task TEXT,
  last_plan TEXT,
  handoff_state TEXT NOT NULL DEFAULT 'none'
);
"""

GATE_MATCH_WINDOW_S = 60.0
ERROR_EXCERPT = 200

_LEADING_ERROR = re.compile(r"(?i)^\s*(error|traceback|exception)\b")
_EXIT_CODE = re.compile(r"(?i)\bexit(?:ed)?[ _]?(?:with )?(?:code|status)\b[\"']?\s*[:=]?\s*(-?\d+)")


def init(db: sqlite3.Connection) -> sqlite3.Connection:
    db.executescript(SCHEMA)
    if "flags" not in {r[1] for r in db.execute("PRAGMA table_info(events)")}:
        db.execute("ALTER TABLE events ADD COLUMN flags TEXT")
    db.commit()
    return db


def args_hash(args) -> str:
    try:
        canon = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        canon = repr(args)
    return hashlib.sha256(canon.encode("utf-8", "replace")).hexdigest()


def _excerpt(text, n=ERROR_EXCERPT):
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def derive_ok(result) -> tuple[bool, str | None]:
    if result is None:
        return True, None
    if isinstance(result, (dict, list)):
        parsed, text = result, json.dumps(result, default=str)
    else:
        text = str(result)
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None

    if isinstance(parsed, dict):
        err = parsed.get("error")
        if err:
            return False, _excerpt(err)
        if parsed.get("success") is False or parsed.get("ok") is False:
            return False, _excerpt(parsed.get("message") or parsed.get("output") or "failed")
        code = parsed.get("exit_code", parsed.get("returncode"))
        if isinstance(code, int) and code != 0:
            tail = parsed.get("stderr") or parsed.get("output") or ""
            return False, _excerpt(f"exit {code}" + (f": {tail}" if tail else ""))
        return True, None

    if _LEADING_ERROR.match(text):
        return False, _excerpt(text)
    m = _EXIT_CODE.search(text)
    if m and int(m.group(1)) != 0:
        return False, _excerpt(f"exit {m.group(1)}")
    return True, None


def insert_event(db, *, profile, tool, action_class, args, args_summary, ok, error=None,
                 conversation_id=None, session_id=None, cwd=None, duration_ms=None,
                 gated=False, approval=None, ts=None, flags=None) -> int:
    cur = db.execute(
        "INSERT INTO events (ts, profile, conversation_id, session_id, cwd, tool, action_class,"
        " args_hash, args_summary, ok, error, duration_ms, gated, approval, flags)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ts or time.time(), profile, conversation_id, session_id, cwd, tool, action_class,
         args_hash(args), args_summary, int(bool(ok)), error, duration_ms, int(bool(gated)), approval,
         flags),
    )
    db.commit()
    return cur.lastrowid


def set_event_approval(db, event_id, approval):
    db.execute("UPDATE events SET approval=? WHERE id=?", (approval, event_id))
    db.commit()


def events_for_session(db, session_id):
    return db.execute(
        "SELECT ts, tool, action_class, args_summary, ok, error FROM events"
        " WHERE session_id=? ORDER BY ts, id", (session_id,)).fetchall()


def events_for_conversation(db, conversation_id, since_ts=None):
    q = "SELECT ts, tool, action_class, args_summary, ok, error FROM events WHERE conversation_id=?"
    params = [conversation_id]
    if since_ts is not None:
        q += " AND ts>=?"
        params.append(since_ts)
    return db.execute(q + " ORDER BY ts, id", params).fetchall()


def classes_since(db, *, profile, conversation_id, session_id, since_ts):
    key, val = ("conversation_id", conversation_id) if conversation_id else ("session_id", session_id)
    rows = db.execute(
        f"SELECT DISTINCT action_class FROM events WHERE profile=? AND {key}=? AND ts>=?",
        (profile, val, since_ts)).fetchall()
    return sorted(r[0] for r in rows)


def start_turn(db, *, profile, conversation_id, session_id, user_message, ts=None) -> int:
    cur = db.execute(
        "INSERT INTO turns (ts, profile, conversation_id, session_id, user_excerpt) VALUES (?,?,?,?,?)",
        (ts or time.time(), profile, conversation_id, session_id, (user_message or "")[:200]))
    db.commit()
    return cur.lastrowid


def last_turn(db, *, profile, conversation_id, session_id, before_id=None):
    key, val = ("conversation_id", conversation_id) if conversation_id else ("session_id", session_id)
    q = f"SELECT id, ts, classes_touched, corrected FROM turns WHERE profile=? AND {key}=?"
    params = [profile, val]
    if before_id is not None:
        q += " AND id<?"
        params.append(before_id)
    return db.execute(q + " ORDER BY id DESC LIMIT 1", params).fetchone()


def set_turn_classes(db, turn_id, classes):
    db.execute("UPDATE turns SET classes_touched=? WHERE id=?", (json.dumps(sorted(classes)), turn_id))
    db.commit()


def mark_turn_corrected(db, turn_id, source):
    db.execute("UPDATE turns SET corrected=1, correction_source=? WHERE id=?", (source, turn_id))
    db.commit()


def turn_counter(db, profile) -> int:
    return db.execute("SELECT COALESCE(MAX(id),0) FROM turns WHERE profile=?", (profile,)).fetchone()[0]


def prune(db, retention_days, now=None) -> int:
    cutoff = (now or time.time()) - retention_days * 86400
    n = db.execute("DELETE FROM events WHERE ts<?", (cutoff,)).rowcount
    n += db.execute("DELETE FROM turns WHERE ts<?", (cutoff,)).rowcount
    db.commit()
    return n


@dataclass
class PendingGate:
    task_id: str | None
    tool: str
    args_hash: str
    action_class: str
    ts: float
    call_id: str | None = None
    approval: str | None = None
    note: str | None = None


@dataclass
class GateTracker:
    window: float = GATE_MATCH_WINDOW_S
    _pending: list = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, task_id, tool, args, action_class, now=None, call_id=None, note=None):
        now = now or time.time()
        with self._lock:
            self._expire(now)
            self._pending.append(PendingGate(task_id, tool, args_hash(args), action_class, now, call_id or None,
                                             note=note))

    def match(self, task_id, tool, args, now=None, call_id=None) -> PendingGate | None:
        now = now or time.time()
        h = args_hash(args)
        with self._lock:
            self._expire(now)
            for i, p in enumerate(self._pending):
                if call_id and p.call_id:
                    hit = p.call_id == call_id
                else:
                    hit = p.tool == tool and p.args_hash == h and p.task_id == task_id
                if hit:
                    return self._pending.pop(i)
        return None

    def set_approval(self, action_class, approval) -> bool:
        with self._lock:
            for p in reversed(self._pending):
                if p.action_class == action_class and p.approval is None:
                    p.approval = approval
                    return True
        return False

    def drain(self, task_id=None) -> list:
        with self._lock:
            keep, out = [], []
            for p in self._pending:
                (out if task_id is None or p.task_id == task_id else keep).append(p)
            self._pending = keep
            return out

    def _expire(self, now):
        self._pending = [p for p in self._pending if now - p.ts <= self.window]
