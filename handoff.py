import os
import re
import time
from datetime import datetime

MAX_LINES = 25
STALE_SECONDS = 120
TASK_CHARS = 500
PLAN_CHARS = 500

SESSION_COLS = ("session_id", "conversation_id", "profile", "cwd", "platform", "model", "started_ts",
                "ended_ts", "completed", "interrupted", "original_task", "last_plan", "handoff_state")

PLAN_HINTS = [
    (re.compile(r"\b(push|pull request|open a pr|\bpr\b|release|publish)", re.I), "git.remote"),
    (re.compile(r"\b(commit|branch|merge|rebase|stash)", re.I), "git.local"),
    (re.compile(r"\b(delete|remove|clean ?up|rm )", re.I), "fs.delete"),
    (re.compile(r"\b(send|email|message|notify|post to|reply)", re.I), "msg.send"),
    (re.compile(r"\b(install|deploy|upload|curl|request|webhook)", re.I), "net.outbound"),
    (re.compile(r"\b(write|create|edit|update|add|modify|refactor|implement|fix)", re.I), "fs.write"),
    (re.compile(r"\b(run|test|build|execute|lint)", re.I), "shell.exec"),
]

_FENCE = re.compile(r"```.*?(```|$)", re.S)
_JSON_BLOB = re.compile(r"\{\s*\"(name|tool|function|arguments)\".*?\}", re.S)


def clean_text(text, limit):
    text = _FENCE.sub(" ", text or "")
    text = _JSON_BLOB.sub(" ", text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _row_to_session(row):
    return dict(zip(SESSION_COLS, row)) if row else None


def _select(where):
    return f"SELECT {', '.join(SESSION_COLS)} FROM sessions WHERE {where}"


def open_session(db, *, session_id, profile, conversation_id=None, cwd=None, platform=None,
                 model=None, ts=None):
    db.execute(
        "INSERT INTO sessions (session_id, conversation_id, profile, cwd, platform, model, started_ts)"
        " VALUES (?,?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET"
        " conversation_id=COALESCE(excluded.conversation_id, sessions.conversation_id),"
        " cwd=COALESCE(sessions.cwd, excluded.cwd), platform=COALESCE(excluded.platform, sessions.platform),"
        " model=COALESCE(excluded.model, sessions.model)",
        (session_id, conversation_id, profile, cwd, platform, model, ts or time.time()))
    db.commit()


def get_session(db, session_id):
    return _row_to_session(db.execute(_select("session_id=?"), (session_id,)).fetchone())


def begin_turn(db, session_id, user_message, conversation_id=None):
    db.execute(
        "UPDATE sessions SET original_task=COALESCE(original_task, ?), ended_ts=NULL, completed=NULL,"
        " interrupted=NULL, conversation_id=COALESCE(conversation_id, ?),"
        " handoff_state=CASE WHEN handoff_state='open' THEN 'none' ELSE handoff_state END"
        " WHERE session_id=?",
        (clean_text(user_message, TASK_CHARS) or None, conversation_id, session_id))
    db.commit()


def set_last_plan(db, session_id, assistant_response):
    plan = clean_text(assistant_response, PLAN_CHARS)
    if plan:
        db.execute("UPDATE sessions SET last_plan=? WHERE session_id=?", (plan, session_id))
        db.commit()


def _has_work(db, session_id):
    r = db.execute("SELECT 1 FROM events WHERE session_id=? AND action_class!='read' LIMIT 1",
                   (session_id,)).fetchone()
    return r is not None


def close_session(db, session_id, completed, interrupted, ts=None):
    s = get_session(db, session_id)
    if s is None:
        return None
    unfinished = (not completed) or bool(interrupted)
    state = s["handoff_state"]
    if unfinished and state == "none" and _has_work(db, session_id):
        state = "open"
    elif not unfinished and state == "open":
        state = "none"
    db.execute(
        "UPDATE sessions SET ended_ts=?, completed=?, interrupted=?, handoff_state=? WHERE session_id=?",
        (ts or time.time(), int(bool(completed)), int(bool(interrupted)), state, session_id))
    db.commit()
    return state


def _last_activity(db, session):
    r = db.execute("SELECT MAX(ts) FROM events WHERE session_id=?", (session["session_id"],)).fetchone()
    return max(filter(None, [r[0] if r else None, session["started_ts"], session["ended_ts"]]))


def candidates(db, profile, *, now=None, max_age_hours=72, exclude=()):
    now = now or time.time()
    rows = db.execute(
        _select("profile=? AND handoff_state IN ('none','open') AND started_ts>=?"
                " ORDER BY started_ts DESC"),
        (profile, now - max_age_hours * 3600 * 2)).fetchall()
    out = []
    for row in rows:
        s = _row_to_session(row)
        if s["session_id"] in exclude:
            continue
        last = _last_activity(db, s)
        if now - last > max_age_hours * 3600:
            continue
        crashed = s["handoff_state"] == "none" and s["completed"] is None and s["ended_ts"] is None \
            and now - last >= STALE_SECONDS and _has_work(db, s["session_id"])
        if s["handoff_state"] == "open" or crashed:
            s["last_activity"] = last
            out.append(s)
    return out


def find_open(db, profile, cwd, *, now=None, max_age_hours=72, exclude=()):
    found = candidates(db, profile, now=now, max_age_hours=max_age_hours, exclude=exclude)
    if not found:
        return None, 0
    same = [s for s in found if cwd and s["cwd"] == cwd]
    best = max(same or found, key=lambda s: s["last_activity"])
    return best, len(found) - 1


def consume(db, session_id):
    db.execute("UPDATE sessions SET handoff_state='consumed' WHERE session_id=?", (session_id,))
    db.commit()


def clear(db, session_id=None, profile=None):
    if session_id:
        n = db.execute("UPDATE sessions SET handoff_state='cleared' WHERE session_id=? AND"
                       " handoff_state IN ('open','consumed','none')", (session_id,)).rowcount
    else:
        ids = [s["session_id"] for s in candidates(db, profile, max_age_hours=24 * 365)]
        ids += [r[0] for r in db.execute(
            "SELECT session_id FROM sessions WHERE profile=? AND handoff_state='consumed'", (profile,))]
        n = 0
        for sid in set(ids):
            n += db.execute("UPDATE sessions SET handoff_state='cleared' WHERE session_id=?", (sid,)).rowcount
    db.commit()
    return n


def latest_note_session(db, profile):
    found, _ = find_open(db, profile, None, max_age_hours=24 * 365)
    if found:
        return found
    return _row_to_session(db.execute(
        _select("profile=? AND handoff_state='consumed' ORDER BY started_ts DESC LIMIT 1"),
        (profile,)).fetchone())


def last_user_message(db, session_id):
    r = db.execute("SELECT user_excerpt FROM turns WHERE session_id=? ORDER BY id DESC LIMIT 1",
                   (session_id,)).fetchone()
    return r[0] if r else None


def ago(ts, now=None):
    d = max(0, (now or time.time()) - ts)
    if d > 86400:
        return "on " + datetime.fromtimestamp(ts).strftime("%b %-d %H:%M")
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)}m ago"
    return f"{int(d // 3600)}h ago"


def short_path(path):
    if not path:
        return "an unknown directory"
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


def _action_text(tool, cls, summary):
    s = summary or ""
    if tool == "terminal":
        return f"ran `{s}`" if s else "ran a command"
    if tool == "write_file":
        return f"wrote {s}"
    if tool in ("patch", "edit_file"):
        return f"edited {s}"
    if cls == "fs.delete":
        return f"deleted {s}"
    return f"{tool} {s}".strip()


def _outcome(ok, error):
    if ok:
        return "ok"
    return f"error: {error}" if error else "error"


def action_lines(events, limit=MAX_LINES, include_read=False):
    groups = []
    for ts, tool, cls, summary, ok, error in events:
        if cls == "read" and not include_read:
            continue
        key = (tool, cls, summary, bool(ok))
        if groups and groups[-1][0] == key:
            groups[-1][1] += 1
            groups[-1][2] = error
        else:
            groups.append([key, 1, error])
    lines = []
    for (tool, cls, summary, ok), n, error in groups:
        times = f" ×{n}" if n > 1 else ""
        lines.append(f"  • {_action_text(tool, cls, summary)}{times} ({cls}, {_outcome(ok, error)})")
    total = len(lines)
    if total > limit:
        lines = lines[:limit] + [f"  … and {total - limit} more"]
    return lines, sum(1 for e in events if include_read or e[2] != "read")


def _not_done(plan, events):
    if not plan:
        return None
    seen = {e[2] for e in events}
    missing = []
    for rx, cls in PLAN_HINTS:
        if rx.search(plan) and cls not in seen and cls not in missing:
            missing.append(cls)
    excerpt = clean_text(plan, 220)
    line = f'Not done: last stated plan was "{excerpt}"'
    if missing:
        line += f" — no {', '.join(missing[:2])} actions were recorded after it."
    return line


def build_note(session, events, max_chars=3000, now=None, others=0, last_request=None):
    max_chars = min(int(max_chars), 10000)
    head = [f"[Autonoma handoff] An earlier session in {short_path(session.get('cwd'))} ended unfinished "
            f"{ago(session.get('last_activity') or session.get('ended_ts') or session['started_ts'], now)}."]
    task = session.get("original_task")
    if task:
        head.append(f'Task: "{clean_text(task, 300)}"')
    if last_request and task and clean_text(last_request, 200) not in task:
        head.append(f'Last request: "{clean_text(last_request, 200)}"')
    tail = []
    nd = _not_done(session.get("last_plan"), events)
    if nd:
        tail.append(nd)
    if others:
        tail.append(f"{others} other unfinished session{'s' if others != 1 else ''}; /autonoma handoff list")
    tail.append("If the user wants to continue, resume from here rather than redoing completed steps. "
                "Otherwise ignore this note.")

    limit = MAX_LINES
    while True:
        lines, count = action_lines(events, limit)
        body = [f"Done (from tool log, {count} action{'s' if count != 1 else ''}):"] + (lines or ["  • nothing recorded"])
        note = "\n".join(head + body + tail)
        if len(note) <= max_chars or limit <= 1:
            break
        limit = max(1, limit // 2 if limit > 8 else limit - 1)
    return note if len(note) <= max_chars else note[: max_chars - 1] + "…"


def build_progress(events, max_chars=2000):
    lines, count = action_lines(events, MAX_LINES)
    if not count:
        return ""
    limit = MAX_LINES
    while True:
        note = "\n".join(
            ["[Autonoma progress] Context was compacted. Already done in this conversation (from tool log):"]
            + lines + ["Do not redo these steps unless the user asks."])
        if len(note) <= max_chars or limit <= 1:
            return note if len(note) <= max_chars else note[: max_chars - 1] + "…"
        limit = max(1, limit // 2)
        lines, _ = action_lines(events, limit)
