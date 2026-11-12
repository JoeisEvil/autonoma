import json
import logging
import os
import threading
import time
from pathlib import Path

from . import corrections, formatting, handoff, ledger, taxonomy, trust

logger = logging.getLogger(__name__)

PLUGIN = "autonoma"
RULE_PREFIX = f"plugin_rule:{PLUGIN}:"
TAINT_PREFIX = "taint:"
APPROVAL_REASONS = {
    "once": "approved_once",
    "session": "approved_session",
    "always": "approved_always",
    "deny": "denied",
    "timeout": "timeout",
    "cancelled": "timeout",
    "notify_failed": "timeout",
}
BLOCK_ERROR_TYPES = {"plugin_block", "edit_approval_denied", "edit_approval_error"}

CONFIG_KEYS = {
    "enabled": True,
    "start_score": 10,
    "thresholds": None,
    "gain_rate": 0.04,
    "decay_days": 14,
    "hard_floor_enabled": False,
    "hard_floor_score": 20,
    "hard_floor_turns": 10,
    "corrections_llm": False,
    "handoff_enabled": True,
    "handoff_max_age_hours": 72,
    "handoff_max_chars": 3000,
    "handoff_after_compaction": True,
    "retention_days": 90,
    "taint_enabled": True,
}


def _text(message):
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        return " ".join(p.get("text", "") for p in message if isinstance(p, dict) and p.get("type") == "text")
    return ""


def safe(default=None):
    def wrap(fn):
        def inner(self, *args, **kwargs):
            try:
                return fn(self, *args, **kwargs)
            except Exception:
                logger.exception("autonoma: %s failed", fn.__name__)
                return default
        inner.__name__ = fn.__name__
        return inner
    return wrap


class Runtime:
    def __init__(self, ctx):
        self.ctx = ctx
        self._lock = threading.RLock()
        self._db = None
        self._db_path = None
        self._config = None
        self._taxonomy = None
        self._detector = None
        self.gates = ledger.GateTracker()
        self.turns = {}
        self.parents = {}
        self.pending_progress = set()
        self.tainted = {}

    # --- plumbing -----------------------------------------------------------------------------

    @property
    def profile(self):
        try:
            return self.ctx.profile_name or "default"
        except Exception:
            return os.environ.get("HERMES_PROFILE") or "default"

    def data_dir(self) -> Path:
        from plugins.plugin_storage import plugin_data_dir
        return plugin_data_dir(PLUGIN)

    @property
    def db(self):
        from plugins.plugin_storage import plugin_db
        path = self.data_dir() / "data.db"
        with self._lock:
            if self._db is None or self._db_path != path:
                self._db = ledger.init(plugin_db(PLUGIN))
                self._db_path = path
            return self._db

    def config(self, refresh=False):
        if self._config is None or refresh:
            cfg = {}
            for key, default in CONFIG_KEYS.items():
                try:
                    cfg[key] = self.ctx.get_config(key, default=default)
                except Exception:
                    cfg[key] = default
            self._config = cfg
        return self._config

    def engine(self):
        c = self.config()
        return trust.TrustEngine(self.db, {
            "enabled": c["enabled"], "start_score": c["start_score"], "thresholds": c["thresholds"] or {},
            "gain_rate": c["gain_rate"], "decay_days": c["decay_days"],
            "hard_floor_enabled": c["hard_floor_enabled"], "hard_floor_score": c["hard_floor_score"],
            "hard_floor_turns": c["hard_floor_turns"],
        })

    @property
    def taxonomy(self):
        if self._taxonomy is None:
            override = None
            try:
                override = self.data_dir() / "taxonomy.yaml"
            except Exception:
                pass
            self._taxonomy = taxonomy.Taxonomy.load(override)
        return self._taxonomy

    @property
    def detector(self):
        if self._detector is None:
            self._detector = corrections.CorrectionDetector.load()
        return self._detector

    def cwd(self):
        try:
            from agent.runtime_cwd import resolve_agent_cwd
            return str(resolve_agent_cwd())
        except Exception:
            return os.getcwd()

    def root_session(self, session_id):
        seen = set()
        while session_id in self.parents and session_id not in seen:
            seen.add(session_id)
            session_id = self.parents[session_id]
        return session_id

    def is_subagent(self, session_id):
        return bool(session_id) and session_id in self.parents

    def conversation_for(self, session_id):
        s = handoff.get_session(self.db, session_id) if session_id else None
        return (s or {}).get("conversation_id")

    def ensure_session(self, session_id, platform=None, model=None, conversation_id=None):
        if not session_id:
            return None
        with self._lock:
            s = handoff.get_session(self.db, session_id)
            if s is None or (conversation_id and not s.get("conversation_id")):
                handoff.open_session(self.db, session_id=session_id, profile=self.profile,
                                     conversation_id=conversation_id, cwd=self.cwd(),
                                     platform=platform, model=model)
                s = handoff.get_session(self.db, session_id)
            return s

    def write_snapshot(self):
        try:
            data = formatting.export(self.engine(), self.db, self.profile, self.current_turn())
            path = self.data_dir() / "snapshot.json"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, default=str))
            tmp.replace(path)
        except Exception:
            logger.debug("autonoma: snapshot write failed", exc_info=True)

    def current_turn(self):
        return ledger.turn_counter(self.db, self.profile)

    # --- hooks --------------------------------------------------------------------------------

    def hook_map(self):
        return {name: self._debug_wrap(name, fn) for name, fn in self._hooks().items()}

    def _debug_wrap(self, name, fn):
        def wrapped(*args, **kwargs):
            try:
                flag = self.data_dir() / "debug_kwargs"
                if flag.exists():
                    shape = {k: type(v).__name__ for k, v in kwargs.items()}
                    with open(self.data_dir() / "kwargs.log", "a", encoding="utf-8") as f:
                        f.write(json.dumps({"ts": time.time(), "hook": name, "kwargs": shape,
                                            "ids": {k: str(kwargs.get(k))[:40] for k in (
                                                "session_id", "old_session_id", "conversation_id", "task_id",
                                                "tool_call_id", "parent_session_id", "is_first_turn",
                                                "completed", "interrupted", "pattern_key", "choice",
                                                "surface", "status", "error_type", "kind", "reason")
                                                if k in kwargs}}) + "\n")
            except Exception:
                pass
            return fn(*args, **kwargs)
        wrapped.__name__ = fn.__name__
        return wrapped

    def _hooks(self):
        return {
            "pre_tool_call": self.on_pre_tool_call,
            "post_tool_call": self.on_post_tool_call,
            "pre_llm_call": self.on_pre_llm_call,
            "post_llm_call": self.on_post_llm_call,
            "on_session_start": self.on_session_start,
            "on_session_end": self.on_session_end,
            "on_session_reset": self.on_session_reset,
            "post_compaction": self.on_post_compaction,
            "post_approval_response": self.on_post_approval_response,
        }

    @safe()
    def on_pre_tool_call(self, tool_name="", args=None, task_id="", tool_call_id="", **kwargs):
        args = args if isinstance(args, dict) else {}
        cls = self.taxonomy.classify(tool_name, args)
        if cls == "read":
            return None
        summary = self.taxonomy.summarize(tool_name, args)
        with self._lock:
            decision = self.engine().decide(self.profile, cls, summary, self.current_turn())
        source = self._taint_source(kwargs.get("session_id") or task_id, cls)
        note = None
        if source and decision.action != "block":
            decision = self._taint_decision(decision, cls, summary, source, kwargs.get("session_id") or task_id)
            note = f"taint:{source}"
        if decision.action == "approve":
            self.gates.add(task_id, tool_name, args, cls, call_id=tool_call_id, note=note)
        return decision.directive()

    def _taint_source(self, session_id, cls):
        cfg = self.config()
        if not (cfg["enabled"] and cfg.get("taint_enabled", True)) or cls not in self.taxonomy.taint_classes:
            return None
        return self.tainted.get(self.root_session(session_id))

    def _taint_decision(self, decision, cls, summary, source, session_id):
        note = f"This turn read outside content ({source}); check this is what you asked for."
        if decision.action == "approve":
            decision.message = f"{decision.message} {note}"
            return decision
        turn = (self.turns.get(self.root_session(session_id)) or (0,))[0]
        what = f" — {summary}" if summary else ""
        decision.action = "approve"
        decision.message = (f"Autonoma · {cls} is trusted ({decision.score:.0f}/{decision.threshold:.0f}){what}. "
                            f"{note}")
        decision.rule_key = f"{PLUGIN}:{TAINT_PREFIX}{turn}:{cls}"
        return decision

    @safe()
    def on_post_tool_call(self, tool_name="", args=None, result=None, task_id="", duration_ms=None,
                          session_id="", conversation_id="", tool_call_id="", status=None,
                          error_type=None, error_message=None, **kwargs):
        args = args if isinstance(args, dict) else {}
        cls = self.taxonomy.classify(tool_name, args)
        pending = self.gates.match(task_id, tool_name, args, call_id=tool_call_id)
        blocked = error_type in BLOCK_ERROR_TYPES
        if status in ("ok", "error"):
            ok = status == "ok"
            error = ledger._excerpt(error_message) if (error_message and not ok) else None
            if not ok and not error:
                error = ledger.derive_ok(result)[1]
        else:
            ok, error = ledger.derive_ok(result)
        if blocked:
            ok, error = False, "blocked"

        sid = self.root_session(session_id or task_id)
        source = None if blocked else self.taxonomy.untrusted_source(tool_name, args)
        if source and sid:
            self.tainted.setdefault(sid, source)
        with self._lock:
            session = self.ensure_session(sid, conversation_id=conversation_id or None)
            conv = conversation_id or (session or {}).get("conversation_id")
            event_id = ledger.insert_event(
                self.db, profile=self.profile, conversation_id=conv, session_id=sid,
                cwd=(session or {}).get("cwd"), tool=tool_name, action_class=cls, args=args,
                args_summary=self.taxonomy.summarize(tool_name, args), ok=ok, error=error,
                duration_ms=duration_ms, gated=pending is not None,
                approval=pending.approval if pending else None,
                flags=pending.note if pending else None)
            if cls != "read" and not blocked:
                self.engine().apply(self.profile, cls, "clean_call" if ok else "tool_error", event_id)
        if cls != "read":
            self.write_snapshot()
        return None

    @safe()
    def on_post_approval_response(self, pattern_key="", choice="", **kwargs):
        if not pattern_key.startswith(RULE_PREFIX):
            return None
        cls = pattern_key[len(RULE_PREFIX):]
        reason = APPROVAL_REASONS.get(choice)
        if reason is None:
            return None
        if cls.startswith(TAINT_PREFIX):
            self.gates.set_approval(cls.rsplit(":", 1)[-1], f"taint:{choice}")
            return None
        cls = cls.split(":", 1)[0]
        self.gates.set_approval(cls, choice)
        with self._lock:
            self.engine().apply(self.profile, cls, reason)
        self.write_snapshot()
        return None

    @safe()
    def on_session_start(self, session_id="", model="", platform="", **kwargs):
        with self._lock:
            self.config(refresh=True)
            self.ensure_session(session_id, platform=platform, model=model)
            ledger.prune(self.db, self.config()["retention_days"])
            self.engine().tick_decay(self.profile)
        return None

    @safe()
    def on_pre_llm_call(self, session_id="", user_message=None, is_first_turn=False, model="",
                        platform="", parent_session_id="", **kwargs):
        if parent_session_id:
            self.parents[session_id] = parent_session_id
            return None
        text = _text(user_message)
        self.tainted.pop(session_id, None)
        parts = []
        with self._lock:
            session = self.ensure_session(session_id, platform=platform, model=model)
            conv = (session or {}).get("conversation_id")
            self._judge_previous_turn(session_id, conv, text)
            turn_id = ledger.start_turn(self.db, profile=self.profile, conversation_id=conv,
                                        session_id=session_id, user_message=text)
            self.turns[session_id] = (turn_id, time.time())
            handoff.begin_turn(self.db, session_id, text)
            cfg = self.config()
            if is_first_turn and cfg["handoff_enabled"]:
                note = self._handoff_note(session_id)
                if note:
                    parts.append(note)
            if session_id in self.pending_progress:
                self.pending_progress.discard(session_id)
                if cfg["handoff_after_compaction"] and conv:
                    progress = handoff.build_progress(ledger.events_for_conversation(self.db, conv))
                    if progress:
                        parts.append(progress)
        self.write_snapshot()
        return {"context": "\n\n".join(parts)} if parts else None

    def _judge_previous_turn(self, session_id, conv, text):
        prev = ledger.last_turn(self.db, profile=self.profile, conversation_id=None, session_id=session_id)
        if prev is None or prev[3]:
            return
        prev_id, prev_ts, classes_json, _ = prev
        classes = json.loads(classes_json) if classes_json else ledger.classes_since(
            self.db, profile=self.profile, conversation_id=conv, session_id=session_id, since_ts=prev_ts)
        if not corrections.should_evaluate(classes):
            return
        engine = self.engine()
        source = "heuristic" if self.detector.is_correction(text) else None
        if source and self.config()["corrections_llm"]:
            source = "llm" if self._llm_confirms(session_id, text) else None
        gated = [c for c in classes if c != "read"]
        if source:
            ledger.mark_turn_corrected(self.db, prev_id, source)
            engine.apply_correction(self.profile, gated, self.current_turn())
        else:
            for c in gated:
                engine.apply(self.profile, c, "clean_turn")

    def _llm_confirms(self, session_id, text):
        try:
            s = handoff.get_session(self.db, session_id) or {}
            prompt = (f"Agent's previous message: {s.get('last_plan') or '(none)'}\n"
                      f"User's reply: {text[:1000]}\nIs the user correcting or rejecting what the agent did?")
            reply = self.ctx.llm.complete(
                messages=[{"role": "system", "content": corrections.LLM_SYSTEM_PROMPT},
                          {"role": "user", "content": prompt}],
                max_tokens=3, timeout=15)
            content = getattr(reply, "text", None) or getattr(reply, "content", None) or str(reply)
            return corrections.parse_llm_verdict(content)
        except Exception:
            logger.debug("autonoma: llm correction check failed", exc_info=True)
            return False

    def _handoff_note(self, session_id):
        cfg = self.config()
        s, others = handoff.find_open(self.db, self.profile, self.cwd(),
                                      max_age_hours=cfg["handoff_max_age_hours"], exclude={session_id})
        if s is None:
            return None
        note = self.note_for(s, others)
        handoff.consume(self.db, s["session_id"])
        return note

    def note_for(self, s, others=0):
        cfg = self.config()
        events = (ledger.events_for_conversation(self.db, s["conversation_id"]) if s.get("conversation_id")
                  else ledger.events_for_session(self.db, s["session_id"]))
        return handoff.build_note(s, events, max_chars=cfg["handoff_max_chars"], others=others,
                                  last_request=handoff.last_user_message(self.db, s["session_id"]))

    @safe()
    def on_post_llm_call(self, session_id="", assistant_response="", **kwargs):
        if self.is_subagent(session_id):
            return None
        with self._lock:
            handoff.set_last_plan(self.db, session_id, assistant_response or "")
            turn = self.turns.get(session_id)
            if turn:
                conv = self.conversation_for(session_id)
                classes = ledger.classes_since(self.db, profile=self.profile, conversation_id=conv,
                                               session_id=session_id, since_ts=turn[1])
                ledger.set_turn_classes(self.db, turn[0], classes)
        return None

    @safe()
    def on_session_end(self, session_id="", completed=True, interrupted=False, **kwargs):
        if self.is_subagent(session_id):
            return None
        with self._lock:
            handoff.close_session(self.db, session_id, completed, interrupted)
        return None

    @safe()
    def on_session_reset(self, session_id="", **kwargs):
        with self._lock:
            s = handoff.get_session(self.db, session_id)
            if s and s["ended_ts"] is None:
                handoff.close_session(self.db, session_id, completed=False, interrupted=True)
        return None

    @safe()
    def on_post_compaction(self, session_id="", old_session_id="", **kwargs):
        with self._lock:
            if old_session_id and old_session_id != session_id:
                old = handoff.get_session(self.db, old_session_id)
                if old:
                    handoff.open_session(self.db, session_id=session_id, profile=self.profile,
                                         conversation_id=old["conversation_id"], cwd=old["cwd"],
                                         platform=old["platform"], model=old["model"])
                    self.db.execute(
                        "UPDATE sessions SET original_task=COALESCE(original_task, ?) WHERE session_id=?",
                        (old["original_task"], session_id))
                    self.db.execute("UPDATE sessions SET handoff_state='cleared' WHERE session_id=?",
                                    (old_session_id,))
                    self.db.execute("UPDATE turns SET session_id=? WHERE session_id=?",
                                    (session_id, old_session_id))
                    self.db.commit()
                if old_session_id in self.turns:
                    self.turns[session_id] = self.turns.pop(old_session_id)
            self.pending_progress.add(session_id)
        return None

    # --- model-facing tool --------------------------------------------------------------------

    def status_tool(self, args=None, **kwargs):
        try:
            args = args if isinstance(args, dict) else {}
            only = args.get("class")
            with self._lock:
                engine = self.engine()
                rows = engine.status(self.profile, self.current_turn(), [only] if only else None)
                found, _ = handoff.find_open(self.db, self.profile, self.cwd(),
                                             max_age_hours=self.config()["handoff_max_age_hours"],
                                             exclude={kwargs.get("session_id")})
            classes = [{k: r[k] for k in ("class", "score", "threshold", "autonomous", "calls_to_autonomy",
                                          "in_timeout")} for r in rows]
            return json.dumps({
                "classes": classes,
                "frozen": any(r["frozen"] for r in rows),
                "open_handoff": found is not None,
                "hint": formatting.hint(rows),
            })
        except Exception as exc:
            logger.exception("autonoma_status failed")
            return json.dumps({"error": f"autonoma_status failed: {exc}"})
