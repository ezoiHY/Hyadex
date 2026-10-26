# Development rules

- Match each provider's native user-facing behavior: Codex chats must match
  Codex, and Claude chats must match Claude. Preserve the thinking text and
  activity each provider exposes, including live updates, chronological order,
  and retention after completion or interruption.
- Verify provider parity separately against the corresponding native runtime
  and client. Do not infer Claude parity from Codex tests, treat headings as
  proof of complete content, or claim parity from visibility changes alone.
- Keep new subsystems in their own modules (as `hyadex/groups.py` does) and wire
  them into `agent_server.py` through a small adapter. Do not grow the
  monolith when a module can own the logic.
- Run tests with a temporary `HOME` and state. Exercise changes against a real
  server through `scripts/dev_server.py` (an isolated named instance). Never
  restart, reconfigure or reuse the state of an installed server, including
  an AgentsDock server on the same machine, while testing.
- New runtime modules must be listed in every release manifest:
  `install.sh` (`RELEASE_FILES` and the staging list), `deploy.sh` and
  `scripts/package_release.py`. The startup integration tests catch omissions.
- Agent-visible prompts stay compact: group dispatches send only unseen
  messages, never re-send history, and never rewrite earlier context.
- Do not publish releases, push tags, create repositories, make the
  repository public or enable in-app updates without an explicit request.
- Record user-facing changes in `docs/DEV_LOG.md`, and keep the AgentsDock
  attribution in `NOTICE`.
