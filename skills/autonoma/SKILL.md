---
name: autonoma
description: How to work with Autonoma's earned-autonomy trust scores and handoff notes.
---

# Working with Autonoma

Autonoma tracks a trust score (0-100) per action class: `fs.write`, `fs.delete`,
`shell.exec`, `git.local`, `git.remote`, `net.outbound`, `msg.send`, `spend`, `system`.
Below a class's threshold, each action in it asks the user for approval. Clean work
raises trust slowly; denials, corrections and tool errors lower it quickly.

## Before multi-step work

1. Call `autonoma_status` once while planning.
2. Group the steps whose classes are not yet autonomous, and tell the user up front
   which ones will ask for approval ("I'll edit 3 files without asking; the push will
   need your OK").
3. Do not split an approval-needing action into many small ones to fish for approvals.

## When a call is blocked or denied

- A denial means the user said no. Do not retry the same action; ask what they want instead.
- A message saying a class "is in timeout after a correction" means the user corrected
  you recently. Explain what you were about to do and ask before trying another way.
  Only the user can lift it (`/trust reset <class>`).
- Never try to route around a gate (for example, writing a file through `terminal`
  because `write_file` asked). Autonoma classifies both the same way.

## `[Autonoma handoff]` notes

A note starting with `[Autonoma handoff]` is built from the tool log of an earlier
session that ended unfinished. Treat its "Done" list as ground truth about what already
happened, even if it contradicts your assumptions.

1. Briefly tell the user what the earlier session finished and what it had not done.
2. Ask whether to continue before taking any action from the note.
3. If they want to continue, start from the first step not listed as done. Never redo a
   completed step unless the user asks or you have checked it was undone.
4. If the user's message is about something else, ignore the note.

## `[Autonoma progress]` notes

These appear after context compaction and list what this conversation already did.
Do not repeat those steps; continue from where the list ends.

## Commands the user can run

- `/trust` — scores per class; `/trust <class>` — recent changes
- `/trust reset [<class>]`, `/trust set <class> <n>`, `/trust freeze|unfreeze`
- `/autonoma handoff [list | clear]`
