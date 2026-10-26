# Autopilot

Autopilot lets one agent plan and another implement, with a reviewer checking
every step. By default Claude Code plans and reviews and Codex implements. It
is the automated version of a [group discussion](GROUP_DISCUSSIONS.md): the
server decides who acts next, so agents spend tokens only on planning,
implementing and reviewing.

Implementation: [`hyadex/autopilot.py`](../hyadex/autopilot.py) and
[`hyadex/routes/autopilot.py`](../hyadex/routes/autopilot.py), wired in `agent_server.py`
(`AUTOPILOT`). Tests: `tests/test_autopilot.py`.

## Flow

```
goal -> plan (planner, read-only) -> [approve] -> for each task:
          execute (executor) -> verify (server, in the Codex sandbox) -> review (reviewer)
            approve -> commit exactly the reviewed tree -> next task   ([approve] again with approval=step)
            revise  -> back to the executor with the reviewer's instructions
            replan  -> discard the rejected edits; the planner re-plans the rest (finished tasks stay)
        -> done: a branch with one commit per task, ready for you to review and merge
```

Agents produce structured results only at three points, each as one JSON block
in their final reply:

| Role | Result |
| --- | --- |
| Planner | `{"summary", "tasks": [{"title", "detail", "files", "acceptance"}]}` |
| Executor | `{"status": "done" or "blocked", "summary", "notes"}` |
| Reviewer | `{"verdict": "approve", "revise" or "replan", "notes", "instructions"}` |

An invalid plan or verdict is asked for once more; a second failure moves the
run to `needs_attention`.

## Options

Every option is set per run when it is created.

| Option | Values | Default |
| --- | --- | --- |
| `approval` | `plan`: approve the plan, then automatic. `step`: also approve after every approved step. `none`: fully automatic. | `plan` |
| `isolation` | `worktree`: a new branch `hyadex/<run id>` in its own worktree (keeping the chosen subdirectory), with one commit per approved task; your working tree is untouched. `in_place`: edit the working directory directly, with no commits. | `worktree` |
| `reviewer` | `planner`: the planning session reviews; cheapest, and it knows the plan. `fresh`: a separate reviewer agent with no stake in the plan. `none`: the verification command alone decides. | `planner` |
| `verify_command` | A shell command the server runs after each step, such as `python3 -m unittest -q`. | none |
| `verify_sandbox` | `codex`: run it in the Codex sandbox (writes only inside the work tree and temporary directories, no network), the same boundary as the executor. `none`: run it unsandboxed; use only for trusted repositories. | `codex` |
| `verify_timeout_seconds` | 10–3,600 | 600 |
| `planner`, `executor`, `reviewer_agent` | Agent per role: `backend`, `model`, `effort`, and permission overrides. | Claude, Codex, Claude |
| `budgets` | `max_tasks` (1–30), `max_revisions` per task (0–5), `max_replans` (0–5) | 12, 2, 2 |
| `constraints` | Extra rules, given to every role. | none |

Default permissions:

- Claude as planner or reviewer reads without asking (`dontAsk`); as executor
  it uses `acceptEdits`. Shell commands then still need approval in its member
  chat.
- Codex as planner or reviewer runs `read-only`; as executor it runs
  `workspace-write` with no approval prompts.

## Rules that keep it honest

- **Verification is the gate.** A step whose verification failed is never
  approved: a reviewer's "approve" becomes "revise", with the command's output.
- **What is reviewed is what is committed.** The server runs verification
  first, then snapshots the work tree with a temporary Git index. The reviewer
  sees the diff of that snapshot (including files verification created that
  are not ignored), and approval commits exactly that tree on the run's branch
  (`commit-tree` and `update-ref`).
- **Agents cannot speak for the server.** Server sections (verification output,
  diff) and the executor's report are in fences their contents cannot close.
  The report is labelled as the executor's claim, not as evidence.
- **The verification command comes only from the user** who created the run.
  It runs without the server's or providers' credential variables (names
  containing KEY, TOKEN, SECRET, … and provider prefixes). Commands suggested
  in a plan are never executed by the server.
- **Git is used defensively.** Hooks are disabled for every autopilot Git
  command, diffs never run external diff or textconv programs, and output is
  read with size limits. The user's branch, index and working tree are never
  touched in `worktree` mode.
- **Rejected work is dropped.** When the reviewer asks to re-plan, a worktree
  is reset to its last checkpoint, so rejected edits cannot slip into a later
  commit. In place, the edits stay and the next task's diff shows them.

## Token use

- Every request to an agent is self-contained. Before each request, the member
  skips everything else in the group. The executor therefore never receives
  plans, verdicts, planning prompts or its own diff, and reviewers receive the
  executor's report once.
- The goal and constraints reach each role once. There is no group brief.
- Members get an autopilot charter rather than the discussion rules.
- The server runs verification and computes diffs. Revisions are reviewed
  against the start of the task, and the inline diff is clipped to 8,000
  characters, with the full diff on disk.

## When a run needs attention

`needs_attention` records why (`attention.kind`). **Resume** acts on that
reason:

| Reason | Resume |
| --- | --- |
| `revision_budget` | Retries the task with the last review's instructions and a fresh budget. |
| `replan_budget` | Allows one more re-plan. |
| `invalid_plan`, `interrupted` (restart), `no_reply`, `error` | Repeats the interrupted phase; after planning finished, it continues with the next unfinished task. |
| `invalid_verdict` | Asks for the review again. |
| `blocked` | Retries the task. |

**Feedback** takes text instead:

- While a task is unfinished, it asks the executor to revise that task.
- Otherwise it asks the planner to re-plan the remaining work.
- On a plan awaiting approval, it revises that plan. Finished tasks are always
  kept.
- After an approved step (`approval=step`), it adds a follow-up commit for that
  step.

**Pause** lets the current step finish; a verification that completes while
paused continues to review on resume and is never run twice. **Abort** stops
running agents and kills verification, including a process that finishes
starting after Abort; the worktree and branch are
kept. **Delete** removes the run and, with `remove_worktree=true`, its
worktree (never an in-place directory). A run that failed to move between
phases lands in `needs_attention`; it is never left half-moved.

## API

All routes require the server token.

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/autopilot/runs` | Create and start a run (`goal`, `cwd`, options above) |
| GET | `/api/autopilot/runs` | List runs (summaries) |
| GET | `/api/autopilot/runs/{id}` | Status, attention, plan, commits, last step (files, verification) |
| POST | `/api/autopilot/runs/{id}/approve` | Approve the plan (optionally with edited `tasks`) or the last step |
| POST | `/api/autopilot/runs/{id}/feedback` | `text`: see above |
| POST | `/api/autopilot/runs/{id}/pause` | Finish the current step, then wait |
| POST | `/api/autopilot/runs/{id}/resume` | Continue a paused run, or retry after `needs_attention` |
| POST | `/api/autopilot/runs/{id}/abort` | Stop now; the worktree and branch are kept |
| DELETE | `/api/autopilot/runs/{id}?remove_worktree=` | Delete the run (and optionally its worktree) |

Statuses: `planning`, `awaiting_approval` (`approval_kind`: `plan` or `step`),
`executing`, `verifying`, `reviewing`, `paused`, `needs_attention`, `done`,
`aborted`.

The run's live timeline is its group (`group_id`): use
`GET /api/groups/{group_id}/events` or the group event stream. The run owns
that group, so posting messages, editing, archiving, deleting, membership
changes, goals and group jobs are rejected with 409; steer runs with the run's
controls.

## Validation

**Unit tests.** 29 autopilot tests cover:

- every option, revise/replan and both budgets;
- exact commits including verification output, and the verification gate;
- forged report fences;
- pause/resume during verification (one run only), and verification finishing
  while paused;
- abort killing verification;
- failing transitions;
- long goals, subdirectories, and recovery before and after posting;
- secret scrubbing, timeouts with background children, and the real Codex
  sandbox blocking writes outside the work tree;
- Git hook suppression;
- route guards.

**Real end-to-end runs** (2026-10-25) on an isolated `dev` instance with Claude
Code 2.1.309 and Codex 0.163.0-alpha.11.1:

- **Defaults** (`plan`, `worktree`, `planner`, sandboxed verification):
  - Claude planned "add slugify to a small Python project" in two tasks and
    waited for approval.
  - Codex implemented each task. The server ran `python3 -m unittest` in the
    Codex sandbox after each one, and Claude approved both.
  - The branch got two commits holding only the two source files. The
    worktree's tests pass and the original tree was unchanged.
  - The executor never received planning prompts, plans, verdicts or review
    requests.
  - Five dispatches sent 10,272 prompt characters, 29% less than before these
    changes. The executor's first request was 59% smaller.
- **Alternate options** (`none`, `in_place`, `fresh`): a one-task run completed
  with no approval. The file was written in place without commits, a separate
  reviewer reviewed, and three dispatches sent 5,304 characters.

Not yet exercised with real agents: `approval=step`, revise and replan loops,
pause/resume and restart recovery (unit tests only).
