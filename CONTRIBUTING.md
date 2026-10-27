# Contributing to Hyadex

This repository is the Hyadex server. Runtime modules are being moved from the repository
root into `hyadex/`; Python tests live in `tests/`. Package ownership and dependency
rules are documented in [architecture](docs/ARCHITECTURE.md). Read [AGENTS.md](AGENTS.md) first.

## Development and tests

Use Python 3.13, matching the pull-request workflow.

```bash
uv sync --locked --python 3.13
ulimit -s "$(ulimit -Hs)"
HOME="$(mktemp -d)" PYTHONDONTWRITEBYTECODE=1 uv run --python 3.13 python scripts/run_test_shard.py --index 0 --count 1
```

Run one test module with its package name:

```bash
HOME="$(mktemp -d)" PYTHONDONTWRITEBYTECODE=1 uv run --python 3.13 python -m unittest tests.test_group_chat -v
```

For a quick check of the extracted packages, run:

```bash
.venv/bin/python scripts/run_fast_tests.py
```

This runner creates a temporary home and rejects imports of the server
(`agent_server`, `hyadex.main` and the test namespace `tests.hyadex_server`).
It covers calendar and job policy, native provider-client contracts, runtime
input validation, group/Autopilot engines and routes, the app factory, transcript
parsing and runtime inventories. It does not replace the
full integration/provider/installer suite above; `--list` shows its exact scope.

`tests/test_route_inventory.py` lists the routes that the Mac client, the
in-chat helpers and planned client features call. Removing or renaming one of
those routes must update its caller and that list together.

Several modules read the home directory when the server is imported, so
always use a temporary `HOME`; never point tests at installed services or real
chat history. On macOS a few inherited suites (installer and memory-admission checks) depend on Linux or on free memory and may fail
locally; CI runs on Linux.

Try changes against a real server with:

```bash
.venv/bin/python scripts/dev_server.py
```

It runs the isolated `dev` named instance on port 7871. See
[named instances](docs/NAMED_INSTANCES_TESTING.md) for more.

Pull requests to `main` run the lock check and the full Python suite in
[Server CI](.github/workflows/server-ci.yml). This workflow does not publish
releases. Tests are not included in release archives.

## Documentation

Keep the README focused on installation and basic use. Detailed API contracts,
security invariants and verification records belong in `docs/`, for example
[group discussions](docs/GROUP_DISCUSSIONS.md), [Autopilot](docs/AUTOPILOT.md),
the [server reference](docs/SERVER_REFERENCE.md) and the
[backend checkpoint](docs/BACKEND_STATUS.md).

## Before opening a pull request

- Run relevant tests and explain any skipped or environment-dependent checks.
- Keep changes focused; do not deploy or restart an existing service as part of
  a test run.
- Never commit chat state, uploads, tokens, `.env` files, private hostnames,
  personal machine paths, or compiled caches.
- Review `git diff --check` and describe user-facing behavior in the PR.
