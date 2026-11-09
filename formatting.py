import time

from . import handoff, trust

BAR = 10


def bar(score, threshold, width=BAR):
    filled = int(round(score / 100 * width))
    mark = min(width - 1, int(threshold / 100 * width))
    cells = ["█" if i < filled else "░" for i in range(width)]
    if cells[mark] == "░":
        cells[mark] = "┊"
    return "".join(cells)


def state(row):
    if row["in_timeout"]:
        return f"timeout {row['turns_left']}t"
    if row["autonomous"]:
        return "autonomous"
    return f"~{row['calls_to_autonomy']} {trust.VERBS.get(row['class'], 'calls')}"


def trust_table(rows, frozen=False, enabled=True):
    width = max([len(r["class"]) for r in rows] + [5])
    lines = [f"{'class':<{width}}  {'trust':<{BAR}}  score  state"]
    for r in rows:
        lines.append(f"{r['class']:<{width}}  {bar(r['score'], r['threshold'])}  "
                     f"{r['score']:>3.0f}/{r['threshold']:<3.0f} {state(r)}")
    notes = []
    if frozen:
        notes.append("Scores are frozen (/trust unfreeze to resume).")
    if not enabled:
        notes.append("Trust gate is disabled in settings; scores still update.")
    return "\n".join(lines + ([""] + notes if notes else []))


def history_table(cls, events):
    if not events:
        return f"No trust events for {cls} yet."
    lines = [f"Last {len(events)} trust events for {cls}:"]
    for e in events:
        lines.append(f"  {handoff.ago(e['ts']):>12}  {e['delta']:+6.1f} → {e['score_after']:5.1f}  {e['reason']}")
    return "\n".join(lines)


def hint(rows):
    auto = [r["class"] for r in rows if r["autonomous"] and not r["in_timeout"]]
    ask = [r["class"] for r in rows if not r["autonomous"] and not r["in_timeout"]]
    blocked = [r["class"] for r in rows if r["in_timeout"]]
    parts = []
    parts.append(f"You may act without approval for: {', '.join(auto)}." if auto
                 else "Every non-read action needs the user's approval.")
    if ask and auto:
        parts.append(f"These will ask the user first: {', '.join(ask)}.")
    if blocked:
        parts.append(f"Blocked after a correction: {', '.join(blocked)}; ask the user before retrying.")
    parts.append("Batch approval-needing actions and tell the user up front.")
    return " ".join(parts)


def handoff_list(sessions, now=None):
    if not sessions:
        return "No unfinished sessions."
    lines = ["Unfinished sessions:"]
    for s in sessions:
        task = handoff.clean_text(s.get("original_task") or "(no task recorded)", 60)
        lines.append(f"  {s['session_id'][:12]}  {handoff.ago(s['last_activity'], now):>10}  "
                     f"{handoff.short_path(s.get('cwd'))}  \"{task}\"")
    return "\n".join(lines)


def export(engine, db, profile, current_turn):
    rows = engine.status(profile, current_turn)
    open_sessions = handoff.candidates(db, profile, max_age_hours=24 * 365)
    return {
        "generated_ts": time.time(),
        "profile": profile,
        "trust": rows,
        "trust_events": engine.recent_events(profile, 200),
        "open_sessions": [{k: s.get(k) for k in ("session_id", "cwd", "original_task", "last_plan",
                                                  "started_ts", "last_activity", "handoff_state")}
                          for s in open_sessions],
    }
