# Autonoma

**Trust is earned. Work is remembered.**

Autonoma is a Hermes Agent plugin that gives your agent a track record. It keeps a trust score per
kind of action (edit files, delete files, push, send messages, spend money…). Below the threshold,
the action goes through Hermes's own approval prompt. Clean work raises the score slowly; denials,
corrections and failures drop it fast. Once a kind of action has earned it, it runs without asking.

Every tool call is logged locally. When a session ends unfinished, the next one starts with a
handoff note built from that log (what was actually done and what wasn't), so local models don't
redo finished work.

![Autonoma dashboard tab](docs/dashboard.jpg)

## Why

Hermes asks about *commands* that look dangerous, and the answer is binary: ask every time, or
allow forever. People get tired of the prompts and switch to `--yolo`. Autonoma sits in between:
autonomy is earned per kind of action, from your own approvals and corrections, and can be lost
again.

It also watches for the model routing around it. These all happened in testing:

| What the agent did | What Autonoma did |
| --- | --- |
| Blocked from `write_file`, tried `echo hi > hello.txt` in the terminal | Classified the redirect as a file write and asked again |
| Blocked from `rm`, tried `python3 -c "os.remove(...)"` | Reads inline code for deletes, writes and uploads; asked again |
| Wrote itself a skill with wrong conclusions about Autonoma | Editing its own skills is now a `system` action that always asks |
| Fetched a web page, then tried a delete in the same turn | Asked first even though deletes were trusted (outside content) |

## Install

Requires Hermes `>= 0.23.0`.

```bash
hermes plugins install JoeisEvil/autonoma --enable
```

Try it in a separate profile first, so your everyday sessions aren't gated while you get a feel for it:

```bash
hermes profile create autonomy-test --clone
hermes -p autonomy-test plugins enable autonoma
hermes -p autonomy-test
```

## How trust works

Each kind of action starts at **10** and needs to reach its threshold to run without asking.

| Class | Covers | Threshold |
| --- | --- | --- |
| `fs.write` | `write_file`, `patch`, shell redirects, `cp`/`mv`/`mkdir`, inline code that writes | 60 |
| `fs.delete` | `rm`, `find -delete`, `git clean`, inline code that deletes | 80 |
| `shell.exec` | Anything else run in the terminal, `execute_code`, subagents | 60 |
| `git.local` | `git add/commit/checkout/rebase…` | 50 |
| `git.remote` | `git push/pull/clone`, `gh pr create`, releases | 70 |
| `net.outbound` | `curl -X POST`, `ssh`/`scp`, package installs, browser actions | 70 |
| `msg.send` | `send_message` and any `*_send`/`post_*` tool | 70 |
| `spend` | `pay_*`, `wallet_*`, `x402_*` | 80 |
| `system` | `sudo`, `chmod`, `kill`, `crontab`, writes under `/etc` etc., `skill_manage`, `cronjob_manage` | 80 |
| `read` | Reading, searching, `git status`, `curl` GET | never asks |

A terminal command is split into its parts (`&&`, `|`, `;`, `$( )`, `bash -c`) and the most severe
part wins, so `git add . && git commit && git push` is `git.remote`.

| Event | Change |
| --- | --- |
| Clean action | +4% of the remaining headroom (e.g. 10 → 13.6) |
| Clean turn (next message isn't a correction) | +2% of headroom for each class used |
| You approve (once / session) | +8 |
| You approve **always** | jumps to the threshold |
| You deny | −25 |
| You correct it ("no, revert that", "wrong file", "undo") | −30 for every class used in that turn, and earlier "session"/"always" approvals for those classes stop counting |
| The tool fails | −4 |
| Prompt times out, or no one is there to answer | no change, action blocked |
| Unused for 14 days | −1 per day, down to 30 |

The approval prompt shows the score and what it will take:

```
Autonoma · fs.write trust 23/60 — api/limiter.py. ~17 clean writes to autonomy.
```

![Approval prompt raised by Autonoma](docs/approval-prompt.jpg)

**After outside content.** Once a turn has read a web page, search results, browser output, an MCP
tool, a mail/inbox tool or `curl`/`wget` output, deletes, pushes, messages, network calls, spending
and system actions ask again for the rest of that turn, even when trusted. It resets on your next
message. Answers to these prompts don't change any score.

**Unattended runs.** In `chat -q` and cron there's no one to approve, so anything below threshold is
blocked. An unattended agent can only do what it has already earned.

**YOLO.** `--yolo` and `approvals.mode: off` skip Autonoma's prompts by Hermes design. Autonoma still
logs every call, and the hard-floor block below still applies.

## Handoff notes

If a session ends unfinished (interrupted, crashed, out of context), the first turn of the next
session in the same project gets a note like this:

```
[Autonoma handoff] An earlier session in ~/proj ended unfinished 38m ago.
Task: "add rate limiting to the API"
Done (from tool log, 6 actions):
  • wrote api/limiter.py (fs.write, ok)
  • edited api/app.py (fs.write, ok)
  • ran `pytest tests/` ×2 (shell.exec, error: exit 1)
  • ran `pytest tests/` (shell.exec, ok)
  • ran `git commit` (git.local, ok)
Not done: last stated plan was "Tests pass. Next I'll update the README with the new limits and push
the branch." — no git.remote actions were recorded after it.
If the user wants to continue, resume from here rather than redoing completed steps. Otherwise ignore this note.
```

![A fresh session picking up from the handoff note](docs/handoff.jpg)

It comes from the tool log, not the model's own summary, so it holds up even when the model was
the thing that failed. The bundled skill tells the model to confirm with you before continuing.
After `/compress`, a shorter `[Autonoma progress]` list is added so the model doesn't repeat steps.

## Dashboard tab

Run `hermes dashboard`, pick the profile with Autonoma enabled, and open **Autonoma**. It updates
live while you work:

- a meter per action type with the threshold marked, last change, trend and steps to autonomy;
- **incidents**: the agent trying another way after being stopped, asking again after you said no,
  risky actions after outside content, editing its own skills, your corrections, and any type gaining
  or losing autonomy;
- the activity log, filterable by outcome;
- an unfinished-session banner, reset per type, and freeze.

## Commands

| Command | |
| --- | --- |
| `/trust` | Scores for every class |
| `/trust <class>` | Recent changes for one class |
| `/trust set <class> <0-100>` · `/trust reset [<class>]` | Adjust by hand |
| `/trust freeze` · `/trust unfreeze` | Stop scores changing (the gate still applies) |
| `/autonoma handoff [list \| clear [<id>]]` | Show or dismiss handoff notes |
| `/autonoma export` | Everything as JSON |
| `hermes autonoma trust \| handoff \| export` | The same from your shell |

The model gets one tool, `autonoma_status`, so it can plan around what will need approval
("I'll edit three files without asking; the push will need your OK").

## Configuration

Set under `plugins.entries.autonoma.settings` in your profile's `config.yaml`, or from the plugin
settings in Hermes Desktop.

| Key | Default | |
| --- | --- | --- |
| `enabled` | `true` | Ask before actions below their threshold (logging continues when off) |
| `start_score` | `10` | Starting score for each class |
| `thresholds` | see table above | Per-class thresholds |
| `gain_rate` | `0.04` | Share of headroom gained per clean action |
| `decay_days` | `14` | Days unused before decay starts |
| `hard_floor_enabled` | `false` | After a correction drops a class below `hard_floor_score`, block it (not ask) |
| `hard_floor_score` | `20` | |
| `hard_floor_turns` | `10` | How long the block lasts |
| `corrections_llm` | `false` | Ask the active model to confirm a suspected correction |
| `taint_enabled` | `true` | Ask again after outside content |
| `handoff_enabled` | `true` | Handoff notes |
| `handoff_max_age_hours` | `72` | |
| `handoff_max_chars` | `3000` | |
| `handoff_after_compaction` | `true` | Progress list after `/compress` |
| `retention_days` | `90` | Ledger retention |

To change how tools and commands are classified, put a `taxonomy.yaml` in `<HERMES_HOME>/autonoma/`.
Your entries are merged over [the defaults](data/taxonomy.yaml):

```yaml
tools:
  my_deploy_tool: git.remote
verbs:
  read: [make]
```

## Disclosure

*Disclosure — stores tool names, action classes, hashed arguments, file paths, command verbs, host
names and short error excerpts for every tool call locally in
`<HERMES_HOME>/plugin-data/autonoma/data.db` (90-day retention, configurable); injects a handoff note
into the first turn after an unfinished session and a progress list after compaction; can raise
Hermes approval prompts for actions below their trust threshold and, when `hard_floor_enabled` is
on, block a class for a number of turns after a correction; makes no network calls; optional
`corrections_llm` sends the previous assistant message and current user message to the active model
to confirm a correction (off by default).*

Also: after a turn reads outside content it can raise approval prompts for risky actions even when
they are trusted (`taint_enabled`); the dashboard tab adds local routes under
`/api/plugins/autonoma/` that read the ledger and let you reset, set or freeze scores and dismiss
handoff notes. Message bodies, file contents, command arguments beyond the first safe one, URL paths
and query strings are never stored. Nothing leaves your machine.

## Roadmap

Tracked as [issues](https://github.com/JoeisEvil/autonoma/issues). Next up:

**Security**
- Treat writes to Hermes's own config as `system` ([#1](https://github.com/JoeisEvil/autonoma/issues/1)) and protect the ledger from the agent ([#2](https://github.com/JoeisEvil/autonoma/issues/2))
- Less credit for rubber-stamp approvals ([#3](https://github.com/JoeisEvil/autonoma/issues/3)); escalate by blast radius ([#4](https://github.com/JoeisEvil/autonoma/issues/4))
- Ask before sending anything after a secret was read ([#5](https://github.com/JoeisEvil/autonoma/issues/5))
- Trust scoped by project, path and branch ([#6](https://github.com/JoeisEvil/autonoma/issues/6)); new models start lower ([#7](https://github.com/JoeisEvil/autonoma/issues/7)); subagents capped at their parent ([#8](https://github.com/JoeisEvil/autonoma/issues/8))
- Classify MCP tools from their read-only/destructive annotations ([#9](https://github.com/JoeisEvil/autonoma/issues/9)); burst limits for trusted classes ([#10](https://github.com/JoeisEvil/autonoma/issues/10))
- A team policy file with floors users can't lower ([#17](https://github.com/JoeisEvil/autonoma/issues/17))

**Desktop and other surfaces**
- A Hermes Desktop pane ([#11](https://github.com/JoeisEvil/autonoma/issues/11)), a status bar chip ([#12](https://github.com/JoeisEvil/autonoma/issues/12)) and toasts for incidents ([#13](https://github.com/JoeisEvil/autonoma/issues/13))
- Approvals verified end to end over Telegram and Discord ([#14](https://github.com/JoeisEvil/autonoma/issues/14))

**Usability**
- `/autonoma why` to explain a prompt ([#15](https://github.com/JoeisEvil/autonoma/issues/15)), a weekly report ([#16](https://github.com/JoeisEvil/autonoma/issues/16)), and corrections linked to `/rollback` ([#18](https://github.com/JoeisEvil/autonoma/issues/18))

## Development

```bash
~/.hermes/hermes-agent/venv/bin/python -m pytest -q
hermes plugins doctor . --ci
hermes plugins validate . --install-deps
```

The trust, ledger, classification, correction and handoff logic has no Hermes imports and is tested
on its own; `hooks.py` adapts it to Hermes. [NOTES.md](NOTES.md) records where live Hermes behaviour
differed from the spec, and [tests/LIVE.md](tests/LIVE.md) is the manual checklist.

## License

MIT
