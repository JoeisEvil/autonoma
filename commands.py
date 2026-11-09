import json
import logging

from . import formatting, handoff

logger = logging.getLogger(__name__)

TRUST_HELP = ("Usage: /trust [<class>] | /trust set <class> <0-100> | /trust reset [<class>] | "
              "/trust freeze | /trust unfreeze")
HANDOFF_HELP = "Usage: /autonoma handoff [list | clear [<session_id>]]"
AUTONOMA_HELP = ("Usage: /autonoma [trust … | handoff … | export | help]\n"
                 f"  {TRUST_HELP}\n  {HANDOFF_HELP}")


class Commands:
    def __init__(self, runtime):
        self.rt = runtime

    def _engine(self):
        return self.rt.engine()

    def _rows(self):
        return self._engine().status(self.rt.profile, self.rt.current_turn())

    def overview(self):
        rows = self._rows()
        out = [formatting.trust_table(rows, frozen=any(r["frozen"] for r in rows),
                                      enabled=self.rt.config()["enabled"])]
        found, others = handoff.find_open(self.rt.db, self.rt.profile, self.rt.cwd(),
                                          max_age_hours=self.rt.config()["handoff_max_age_hours"])
        if found:
            task = handoff.clean_text(found.get("original_task") or "", 60)
            more = f" (+{others} more)" if others else ""
            out.append(f"\nOpen handoff from {handoff.ago(found['last_activity'])}: \"{task}\"{more}. "
                       "/autonoma handoff to view.")
        return "\n".join(out)

    def trust(self, raw=""):
        try:
            parts = raw.split()
            engine, p = self._engine(), self.rt.profile
            classes = engine.known_classes(p)
            if not parts:
                return self.overview()
            cmd = parts[0].lower()
            if cmd in ("help", "-h", "--help"):
                return TRUST_HELP
            if cmd == "set":
                if len(parts) != 3 or parts[1] not in classes:
                    return f"{TRUST_HELP}\nClasses: {', '.join(classes)}"
                try:
                    value = float(parts[2])
                except ValueError:
                    return "Score must be a number from 0 to 100."
                if not 0 <= value <= 100:
                    return "Score must be a number from 0 to 100."
                after = engine.set_score(p, parts[1], value)
                self.rt.write_snapshot()
                return f"{parts[1]} trust set to {after:.0f}."
            if cmd == "reset":
                target = parts[1] if len(parts) > 1 else None
                if target and target not in classes:
                    return f"Unknown class {target}. Classes: {', '.join(classes)}"
                engine.reset(p, target)
                self.rt.write_snapshot()
                start = engine.config["start_score"]
                return (f"{target} reset to {start:.0f}; timeout cleared." if target
                        else f"All classes reset to {start:.0f}; timeouts cleared.") + (
                    "\nNote: an 'always' answer at an Autonoma prompt is remembered by Hermes itself; "
                    "remove plugin_rule:autonoma:* from your approval allowlist to undo it.")
            if cmd in ("freeze", "unfreeze"):
                engine.set_frozen(p, cmd == "freeze")
                self.rt.write_snapshot()
                return ("Trust frozen: scores won't change; the gate still applies." if cmd == "freeze"
                        else "Trust unfrozen: scores update again.")
            if cmd in classes:
                return formatting.history_table(cmd, engine.history(p, cmd, 10))
            return f"Unknown class or subcommand: {cmd}\n{TRUST_HELP}"
        except Exception as exc:
            logger.exception("/trust failed")
            return f"Autonoma error: {exc}"

    def handoff(self, raw=""):
        try:
            parts = raw.split()
            db, p = self.rt.db, self.rt.profile
            if not parts:
                s = handoff.latest_note_session(db, p)
                if s is None:
                    return "No handoff notes."
                s.setdefault("last_activity", s.get("ended_ts") or s["started_ts"])
                return self.rt.note_for(s) + f"\n\n(session {s['session_id'][:12]}, {s['handoff_state']})"
            cmd = parts[0].lower()
            if cmd == "list":
                return formatting.handoff_list(handoff.candidates(db, p, max_age_hours=24 * 365))
            if cmd == "clear":
                sid = parts[1] if len(parts) > 1 else None
                if sid:
                    match = [s["session_id"] for s in handoff.candidates(db, p, max_age_hours=24 * 365)
                             if s["session_id"].startswith(sid)]
                    sid = match[0] if len(match) == 1 else sid
                n = handoff.clear(db, sid, profile=p)
                self.rt.write_snapshot()
                return f"Cleared {n} handoff note{'s' if n != 1 else ''}."
            return HANDOFF_HELP
        except Exception as exc:
            logger.exception("/autonoma handoff failed")
            return f"Autonoma error: {exc}"

    def export(self):
        data = formatting.export(self._engine(), self.rt.db, self.rt.profile, self.rt.current_turn())
        return json.dumps(data, indent=2, default=str)

    def autonoma(self, raw=""):
        try:
            parts = raw.strip().split(maxsplit=1)
            if not parts:
                return self.overview()
            cmd, rest = parts[0].lower(), (parts[1] if len(parts) > 1 else "")
            if cmd == "trust":
                return self.trust(rest)
            if cmd == "handoff":
                return self.handoff(rest)
            if cmd == "export":
                return self.export()
            return AUTONOMA_HELP
        except Exception as exc:
            logger.exception("/autonoma failed")
            return f"Autonoma error: {exc}"

    # --- `hermes autonoma …` ------------------------------------------------------------------

    def setup_cli(self, parser):
        subs = parser.add_subparsers(dest="autonoma_command")
        t = subs.add_parser("trust", help="Show or change trust scores")
        t.add_argument("args", nargs="*", help="[<class>] | set <class> <n> | reset [<class>] | freeze | unfreeze")
        h = subs.add_parser("handoff", help="Show, list or clear handoff notes")
        h.add_argument("args", nargs="*", help="[list | clear [<session_id>]]")
        subs.add_parser("export", help="Print trust, trust events and open sessions as JSON")
        parser.set_defaults(func=self.run_cli)

    def run_cli(self, args):
        sub = getattr(args, "autonoma_command", None)
        rest = " ".join(getattr(args, "args", None) or [])
        if sub == "trust":
            print(self.trust(rest))
        elif sub == "handoff":
            print(self.handoff(rest))
        elif sub == "export":
            print(self.export())
        else:
            print(self.overview())


def register(ctx, runtime):
    c = Commands(runtime)
    ctx.register_command("autonoma", c.autonoma, description="Autonoma: trust scores and handoff notes",
                         args_hint="[trust|handoff|export]")
    ctx.register_command("trust", c.trust, description="Autonoma trust scores per action class",
                         args_hint="[<class>|set|reset|freeze|unfreeze]")
    ctx.register_cli_command(name="autonoma", help="Autonoma trust scores and handoff notes",
                             setup_fn=c.setup_cli, handler_fn=c.run_cli)
    return c
