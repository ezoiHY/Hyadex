# Group discussions

A group discussion puts the user, Claude Code and Codex in one shared timeline
on the same server. It is built to spend as few provider tokens as possible
while every agent stays fully native.

Implementation: [`hyadex/groups.py`](../hyadex/groups.py) (engine and models),
[`hyadex/routes/groups.py`](../hyadex/routes/groups.py) (HTTP/WebSocket routes) and
`HyadexGroupRuntime` in [`agent_server.py`](../agent_server.py) (the adapter that
connects groups to ordinary chats). Tests: `tests/test_group_chat.py` and
`tests/test_hyadex_server.py`.

## Model

- A **group** owns the shared timeline, a short **brief** (goal, decisions, open
  questions), settings, an optional **goal**, and up to six **participants**.
- Each participant is an agent with a unique `@handle` (default: the backend
  name, for example `@claude`, `@codex`, `@codex2`) backed by its own **member
  chat**. A member chat is an ordinary chat marked with `group_member`. It uses
  the same admission, queue, provider runtime, recovery and native continuity
  (Claude resume, Codex thread) as any chat, and it loads the agent's own MCP
  servers, skills and project configuration.
- Member chats are hidden from `GET /api/sessions` and global search, but
  `GET /api/sessions/{id}` opens them, and their public form includes
  `group_member: {group_id, handle}`. The member chat shows exactly the prompt
  its agent received.
- All members share the group's working directory.
- Each participant keeps its own permission settings
  (`claude_permission_mode`, `codex_approval_policy`, `codex_sandbox_mode`).
  When an agent waits for approval, its status becomes `needs_action`; approve
  it in that member chat.

State lives in `STATE_DIR/groups/<group_id>/`: `group.json` (record, rounds,
dispatches), `events.jsonl` (append-only timeline with a per-group `seq`) and
`messages/<seq>.md` (full text of messages longer than 2,000 characters).

## How a message is handled

1. `POST /api/groups/{id}/messages` appends the user message and opens a
   **round**.
2. Targets are the explicit `mentions` list, or `@handles` found in the text
   (outside code spans; `@all` means everyone), or the default responder.
3. Each target receives a **dispatch**: one new turn in its member chat whose
   prompt contains only the group messages after that member's cursor, written
   by others. The member's cursor then advances.
4. When the member's turn ends, its final reply is posted to the group with a
   **receipt**: the files the turn changed in the shared Git worktree. The server
   computes it from `git status` snapshots, so it costs no model tokens.
5. If the reply mentions other members, they are queued in the same round
   (an **agent hand-off**), up to `max_agent_hops` per round (default 4).
6. The round finishes when its queue is empty and no dispatch is active.

Turns a member runs outside the group (for example a native goal continuing,
or a message sent directly in the member chat) are mirrored into the group
with `origin: "direct"` and are not routed further.

## Token budget rules

1. **Wake on mention only.** Silent members cost nothing; they catch up the
   next time they are addressed.
2. **Append-only deltas.** Nothing a member has already seen is re-sent or
   rewritten, so each provider's prompt cache keeps hitting.
3. **Publish results, not process.** Tool calls, file reads, command output and
   reasoning stay in the member chat; the group gets the final reply plus the
   receipt.
4. **Reference instead of paste.** Messages longer than `delta_char_limit`
   (default 6,000) are clipped in deltas and name the file with the full text.
   Agents share the workspace, so they open files only when needed.
5. **Charter once, brief once per version.** The rules of the group go to each
   member once; the brief is resent only after it changes.
6. **Budgets and brakes.** Hand-offs are limited per round, and
   `POST /api/groups/{id}/stop` ends rounds, cancels queued dispatches and stops
   running members.

In the end-to-end check (below), a nine-dispatch discussion sent about 4,800
prompt characters in total (4,738 and 4,854 in two runs). A follow-up question to Claude carried one new message
(178 characters) instead of the whole history.

## Reliability

- **Write-ahead.** A dispatch is saved before its member turn is admitted. The
  dispatch id is recorded on the turn as its `client_request_id`. After a
  restart, recovery finds the exact turn by run id, queued id or that id, and
  re-sends nothing. A reply already on the timeline is never recorded twice;
  its hand-offs run once.
- **Every ending is observed.** `turn_started`, `turn_finished`,
  `turn_stopped` and `turn_unqueued` are observed on both the normal and the
  durable event paths, so queued, force-sent and unqueued turns all settle. A
  dispatch whose message is removed from the queue gives that member its
  messages again next time.
- **Stop.** Stop re-reads each dispatch's live state. It removes queued turns,
  interrupts running ones (including a queued turn that started meanwhile), and
  finishes dispatches whose member chat is gone or idle. Archiving a group stops
  it too.
- **Integrity.** A torn last line of `events.jsonl` is repaired on load, and a
  failed append is truncated. Message text cannot open or close
  `<group_message>` blocks, so one agent cannot pose as the user to another.

## Modes

| Option | Behaviour |
| --- | --- |
| `mode: "sequential"` (default) | Targets answer one after another; later members see earlier replies. |
| `mode: "parallel"` | Targets answer independently at the same time, as in a committee's blind first round. Hand-offs that follow run sequentially. |
| `analysis_only: true` | Every dispatch ends with a no-edits instruction (advisor and committee reviews). |

## Autopilot groups

[Autopilot](AUTOPILOT.md) runs are built on this engine. A run owns a group
of kind `autopilot` (`kind` and `meta.autopilot_run_id` in the group's public
form). In those groups:

- Agent mentions never hand off work; the run's state machine decides who acts.
- `POST /api/groups/{id}/messages` returns 409; steer runs with their approve,
  feedback and pause controls.
- The run posts orchestrator messages (`author_kind: "orchestrator"`, shown to
  agents as `from="autopilot"`) with an `audience`. Members outside the audience
  never receive them. Progress notes use the audience `["user"]`, so no agent
  receives them.

## Goals and scheduled jobs

- `PUT /api/groups/{id}/goal` `{handle, objective}` sets a native goal on that
  member (Claude `/goal`, Codex thread goal) and announces it to the member
  once. The member's continuation turns are mirrored into the group.
- `POST /api/groups/{id}/jobs` takes the same body as chat jobs (title, prompt,
  interval/cron/rrule schedule). At each run the prompt is posted into the
  group as a message with `origin: "job"`, so mentions in the prompt choose who
  works. The job is anchored to the default responder's member chat for
  scheduling and history. A busy anchor chat does not defer it, because busy
  members queue group messages; server-wide limits (memory, active runs,
  pending updates) still apply. Removing the anchor member moves the job to the
  new default responder. `GET` lists and `DELETE /api/groups/{id}/jobs/{job_id}`
  removes group jobs. Deleting a group deletes its jobs.

## API

All HTTP routes require the server token (`Authorization: Bearer <token>`).

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/groups?include_archived=` | List groups |
| POST | `/api/groups` | Create: `title`, `cwd`, `brief`, `participants[]`, `settings` |
| GET | `/api/groups/{id}` | Group, participants with live status, open rounds, active dispatches |
| PATCH | `/api/groups/{id}` | `title`, `brief`, `settings`, `archived` |
| DELETE | `/api/groups/{id}` | Delete the group; member chats are archived |
| POST | `/api/groups/{id}/participants` | Add a participant (joins from the present) |
| DELETE | `/api/groups/{id}/participants/{handle}` | Remove an idle participant |
| POST | `/api/groups/{id}/messages` | `text`, optional `mentions`, `mode`, `analysis_only` |
| GET | `/api/groups/{id}/events?after=&limit=` | Timeline page |
| WS | `/api/groups/{id}/events/stream?after=` | Replay after `after`, then a `stream_ready` control frame (`through_seq`), then live events |
| POST | `/api/groups/{id}/stop` | Stop the current rounds |
| PUT | `/api/groups/{id}/goal` | Set a native goal on one member |
| GET/POST | `/api/groups/{id}/jobs` | List or schedule group jobs |
| DELETE | `/api/groups/{id}/jobs/{job_id}` | Delete a group job |

Participant fields: `backend` (`claude` or `codex` unless more are enabled),
`handle`, `label`, `role`, `model`, `effort`, `claude_permission_mode`,
`codex_approval_policy`, `codex_sandbox_mode`. Settings: `default_responder`,
`max_agent_hops` (0–12), `delta_char_limit` (500–50,000).

Timeline event types: `group_created`, `message` (`author`, `author_kind`
`user|agent|system|orchestrator`, `text`, `mentions`, `round_id`, optional
`receipt`, `origin`, `full_text_path`, `job_id`, `audience`), `dispatch`, `member_status`
(`queued|running|idle|stopped|failed`), `round_finished`, `brief_updated`,
`settings_updated`, `participant_added`, `participant_removed`, `goal_set`,
`group_archived`, `group_restored`.

## Validation

Real end-to-end check (2026-10-25) on an isolated `dev` instance with Claude
Code 2.1.309 and Codex 0.163.0-alpha.11.1:

- Codex created a file in the shared Git workspace; its reply carried the
  receipt `changed_files: ["hello.txt"]`, and its `@claude` mention handed
  the round to Claude, which verified the file.
- A second question to Claude carried only the new message, with no charter.
- A parallel, analysis-only round produced independent answers from both.
- Stop interrupted a running Codex command and closed the round as `stopped`.
- A message to a busy Codex was queued, started when Codex was free, and both
  rounds finished.
- A group job, run through the job runner, posted its prompt with
  `origin: "job"` and Codex answered it; the job was listed and deleted.
- The WebSocket stream delivered all 53 events in order without gaps.
- Member chats stayed out of the chat list but opened directly; the server log
  had no errors.

Not yet exercised with real agents: approval prompts (`needs_action`) inside a
group, group goals, and a job firing at its scheduled time (rather than a
manual run). These are covered by unit and wiring tests only.
