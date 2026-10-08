---
session_id: {{session_id}}
parent_session_id: {{parent_session_id}}
generation: {{generation}}
created_at: {{created_at}}
git_branch: {{git_branch}}
git_head: {{git_head}}
status: pending
---

# Session handoff

Write for a reader with ZERO prior context. Be specific: file paths, function names,
commands, exact error messages. Keep the whole document under 2,500 words. Never include
secrets (tokens, API keys, `.env` contents); refer to them by name only.

## 1. Objective and Definition of Done

One paragraph: the user's real goal (not the last instruction). Then a short list of
what "done" means.

## 2. Current state (done and verified)

What is finished and how it was verified (tests passing, commands run and their results).

## 3. In progress

Exactly where work stopped (file, function, line if useful) and the VERY NEXT concrete
action, phrased so the next session can execute it immediately.

## 4. Remaining plan

Ordered checklist of what is left.

## 5. Decisions made and approaches rejected

Each decision with its reason, so it is not relitigated. Rejected approaches and why.

## 6. Gotchas, failing tests, environment quirks, open questions

Known failures, flaky things, environment specifics, and questions only the user can answer.

## 7. Key files and commands

`path` — one-line role. Commands to run or verify (build, test, lint, start).

## 8. Constraints and user preferences stated this session

Anything the user asked for or forbade in this session.

## 9. Memory updates

Durable facts to promote into `.claude/memory/` (decisions.md, gotchas.md, conventions.md).
One bullet each; write "none" if nothing qualifies.
