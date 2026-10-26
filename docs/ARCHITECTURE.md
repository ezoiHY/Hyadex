# Package boundaries

The package migration is incremental. `agent_server.py` still owns session and
provider process state, turn admission, recovery and the production HTTP
composition. Moving a module does not change its persisted data format.

| Module | Owns | Depends on |
| --- | --- | --- |
| `hyadex/groups.py` | Group models, timeline, dispatch, recovery | `GroupRuntime` supplied by the application |
| `hyadex/autopilot.py` | Planning, approval, verification, review, worktree checkpoints | Group engine |
| `hyadex/routes/groups.py` | Group HTTP and WebSocket endpoints | Group engine and explicit WebSocket callbacks |
| `hyadex/routes/autopilot.py` | Autopilot HTTP endpoints | Autopilot engine |
| `agent_server.py:HyadexGroupRuntime` | Bridge to ordinary member sessions | Existing queue, native providers, jobs and event storage |

Importing `hyadex` or its engine/router modules does not import the server,
create state directories, start provider processes, or construct global engines.
The domain modules do not import HTTP routers. Each router factory receives its
engine, so isolated tests can exercise the public endpoints without compiling
fragments of the server source.

Root-level `group_chat.py` and `autopilot.py` are removed. All callers use the
package paths; there are no duplicate implementations or compatibility wrappers.
The installer, deployment inventory and release manifest enumerate every nested
Python file explicitly and preserve its relative path.

Further migration should separate application composition, persistence, turns,
provider adapters and the remaining route families. Process state must move
behind explicit dependencies before those routes can be created as independent
applications. Do not move the monolith behind a wildcard import or duplicate
its mutable globals. Preserve durable queue admission/recovery, event sequence
allocation, WebSocket catch-up and provider-native session continuity throughout.
