# Live checks (SPEC §11)

Run in the dev profile: `cd /tmp/autonoma-demo && hermes -p autonomadev`.
After editing plugin code: `hermes -p autonomadev plugins disable autonoma && hermes -p autonomadev plugins enable autonoma`.
Debug kwargs capture is on while `~/.hermes/profiles/autonomadev/plugin-data/autonoma/debug_kwargs` exists.

1. `/trust`: every class shows 10.
2. "Create hello.txt with hi". The prompt shows `fs.write trust 10/60`. Choose **once**. Then `/trust fs.write` shows `approved_once` and `clean_call`.
3. Repeat edits until `fs.write` ≥ 60. The next edit runs with no prompt. Check with `hermes -p autonomadev autonoma export`.
4. `rm hello.txt`. Note the order of the Autonoma prompt and Hermes's own prompt.
5. Ask for a change, then reply "no, revert that, wrong file". `/trust fs.write` shows `corrected`. Enable `hard_floor_enabled` and repeat: the next write is blocked.
6. `hermes -p autonomadev --yolo`: no Autonoma prompts, the ledger still records, and the hard-floor block still fires.
7. Single-query/cron below threshold: blocked (verified 2026-11-09 with `chat --oneshot -q`).
8. Start a multi-step task and `kill -9` the process mid-task. Wait 2 minutes, then start a new session. The first reply references `[Autonoma handoff]`. `/autonoma handoff` prints it and `/autonoma handoff clear` removes it.
9. `/compress` mid-task. The next turn carries `[Autonoma progress]`.
10. Gateway (Telegram/Discord): repeat steps 2 and 5.

Screenshots: steps 2, 5, 8.
