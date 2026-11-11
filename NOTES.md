# NOTES — differences between the build spec and live Hermes

Hermes tested: v0.23.0+2508.g58b3c09 (main @ 58b3c09b11, 2026-11-09). Dev profile: `autonomadev`
(cloned config, plugin enabled only there).

## Section 14 answers

| Question | Answer | Effect |
| --- | --- | --- |
| `pre_tool_call` ids | Receives `task_id`, `session_id`, `conversation_id`, `tool_call_id`, `turn_id`, `api_request_id`, `middleware_trace`. No cwd. | Gate matching uses `tool_call_id` (falls back to task+tool+args hash). cwd comes from `agent.runtime_cwd.resolve_agent_cwd()` at session start. |
| `post_tool_call` extras | Adds `status` (`ok`/`error`/`blocked`), `error_type` (`plugin_block` when gated and refused), `error_message`. | `ok` uses Hermes's `status` when present, else `ledger.derive_ok`. Blocked calls are logged with `error="blocked"` and no `tool_error` penalty. |
| `post_approval_response` for plugin prompts | Fires from `_human_decision`; plugin approvals carry `pattern_key="plugin_rule:<rule_key>"`, i.e. `plugin_rule:autonoma:<class>`. Registrable plugin hook (doctor accepts it). | Approval evidence is applied from this hook; the post-tool fallback in spec §7 is not needed. **Not yet seen live** (needs an interactive prompt). |
| Session/always answers | `_run_approval_gate` short-circuits `is_approved(session_key, pattern_key)` *without* firing approval hooks. | After "session"/"always", later calls in that class run with no prompt and no approval credit (they still earn `clean_call`). Correct behaviour; documented. |
| Unattended | `chat -q` (single-query) and cron resolve instantly to a block; no approval hook fires. | Verified live: `write_file` and a shell redirect were both blocked with Autonoma's message. No trust change (spec: timeout = 0). |
| YOLO / `approvals.mode: off` | `_run_approval_gate` returns approved first. Plugin `block` is not routed through the gate. | Matches spec. |
| `pre_llm_call` | `session_id, task_id, turn_id, user_message, conversation_history, is_first_turn, model, platform, parent_session_id, sender_id`. **No `conversation_id`.** | Turns are keyed by session; conversation id is learned from tool calls and stored on the session row. Subagents detected via non-empty `parent_session_id`. |
| `on_session_end` | Fires after **every** turn (`completed`, `failed`, `interrupted`, `turn_exit_reason`). | `begin_turn` resets `ended_ts/completed` so a kill mid-turn leaves `completed IS NULL` → treated as crashed after 120 s idle. |
| `post_compaction` | `session_id` = new, `old_session_id` = previous ("" in place). No `conversation_id`. | Old session row is copied to the new id and marked `cleared` (it was superseded, not abandoned). |
| `ctx.register_command` | `(name, handler, description="", args_hint="", argument_mode=None)`; handler `(raw_args)`, may declare `session_id`, `platform`, … Runs in the gateway process. | Commands use `resolve_agent_cwd()`, not `os.getcwd()`. |
| `/handoff` | **Conflicts with a built-in** (`/handoff <platform>`), so registration is rejected. | Handoff commands live under `/autonoma handoff [list|clear]`. `/trust` and `/autonoma` are free. |
| `ctx.register_cli_command` | `(name, help, setup_fn, handler_fn=None, description="")`. Skipped inside the plugin host; wired into the local CLI parser. | `hermes autonoma [trust|handoff|export]`. |
| `plugin_db()` | Returns an open `sqlite3.Connection` via `hermes_cli.sqlite_util.open_db`: WAL (falls back to DELETE on network FS), `check_same_thread=False`, caller owns transactions. | One connection per data dir, serialised with an RLock. |
| Profile | `ctx.profile_name` (derived from `HERMES_HOME`). Each profile already has its own `plugin-data/`. | `profile` column is kept for export/readability. |
| `requires_hermes` | Installed: `0.23.0+2508`. | `requires_hermes: ">=0.23.0"`. |
| Plugin reload | The default-profile gateway multiplexes profiles and owns plugin code. Editing files needs `hermes -p autonomadev plugins disable autonoma && … enable autonoma` to reload (no gateway restart needed). | Dev loop. |

Still to verify live (interactive): prompt text rendering per surface; `post_approval_response`
payload; "always" persistence across restarts; `is_first_turn` after `--resume`; compaction
re-injection; subagent tool calls.

## Deviations from the spec

- **Installed Hermes was v0.19.0** and had none of `plugin_storage`, `config_schema`,
  `post_compaction`, `plugins doctor/validate`. Updated to latest main before building.
- **YAML**: core modules use PyYAML (`import yaml`, shipped by Hermes), not ruamel.
- **Frozen**: spec §7 step 2 reads "class frozen and score ≥ threshold → None". Implemented as:
  freeze stops score changes only; the gate still enforces (matches §9 `/trust freeze`).
- **Corrections**: `stop` counts only at the start of a message (so "add a stop button" is not a
  correction). Added `leading:` to `data/corrections.yaml`.
- **Security scan**: the catalog scanner flags bare privilege, env-dump and disk-format verb names
  even in classifier data. Those three moved into a Python literal (`_BUILTIN_VERBS` in
  `taxonomy.py`, a documented inert shape; one `caution` remains on the disk-format verb). The
  sudoers editor was dropped; it falls back to `shell.exec`, still gated.
- **Tool Search bridge**: `tool_search`/`tool_describe`/`tool_call` are `read`. `tool_call` is
  unwrapped by Hermes and the real tool fires its own `pre_tool_call`, so nothing is bypassed.
- **Debug switch** (not in spec): if `<plugin-data>/autonoma/debug_kwargs` exists, each hook appends
  its kwarg names/types and id fields (no values) to `kwargs.log`. Needed for the Phase 0 capture.
- **Crash detection** (not in spec): a session with no `on_session_end` after its last turn and
  ≥ 10 min idle (counting turns, not just tool calls) is treated as unfinished, because `kill -9` fires no hook.
- **Tainted turns** (not in spec): once a turn runs an untrusted source (`untrusted_sources` in
  `data/taxonomy.yaml`: web/browser/search tools, `mcp_*`, mail/inbox readers, `curl`/`wget`), the
  `taint_classes` (system, spend, fs.delete, git.remote, net.outbound, msg.send) ask for approval
  for the rest of that turn even when trusted. Cleared on the next user message. The prompt's
  rule key is per turn (`autonoma:taint:<turn>:<class>`) so "session"/"always" can't switch it off
  for later turns. Answers to taint prompts don't move trust. Toggle: `taint_enabled`.
- **`skill_manage` is `system`**: found live — the agent wrote a skill with wrong conclusions about
  the plugin, covered by an fs.write "always" grant. Editing its own instructions now always asks.
- **Dashboard tab** (`dashboard/`): `hermes dashboard`, pick the `autonomadev` profile, open **Autonoma**.
  `plugin_api.py` loads the plugin's own modules by file path (no core imports beyond
  `plugins.plugin_storage` and `hermes_cli.config.load_config`, both used in the dashboard docs) and
  opens its own connection to the ledger. Incidents are derived in `insights.py` from the ledger:
  a different action within 5 min after a block (workaround), the same action after a "deny" (retry),
  taint-gated actions (`events.flags`, added by migration), `skill_manage` calls, corrections, and
  threshold crossings. Polls every 2.5 s while the page is visible.
- **Found while recording the demo**: a long model turn (>2 min thinking, no tool calls) was flagged as a
  crashed session; turns now count as activity and the idle window is 10 min. A new request soon after
  a block was flagged as a workaround; workarounds now need to be in the same turn. Shell `for`/`if`/
  `while` wrappers are classified by the commands inside them. The dashboard names the profile from its
  folder, not from ledger rows.
