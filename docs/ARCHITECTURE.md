# Package boundaries

The package migration is incremental. `agent_server.py` still owns session and
provider process state, turn admission and recovery. It supplies the completed
route collection, authorization middleware and lifespan to the application factory. Moving a module does not change its persisted data format.

| Module | Owns | Depends on |
| --- | --- | --- |
| `hyadex/app.py` | FastAPI construction, middleware and lifespan composition | Explicit `ApplicationBindings` |
| `hyadex/store/cross_chat.py` | Durable cross-chat ledger, transactions and migrations | Explicit `CrossChatBindings`; mailbox SQL |
| `hyadex/store/mailbox.py` | Same-server mailbox SQL | Caller-owned SQLite transaction |
| `hyadex/store/transcripts.py` | Bounded JSONL parsing | Caller-owned binary streams |
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

Root-level `group_chat.py`, `autopilot.py`, `chat_mailbox.py` and
`transcript_reader.py` are removed. All callers use the
package paths; there are no duplicate implementations or compatibility wrappers.
The installer, deployment inventory and release manifest enumerate every nested
Python file explicitly and preserve its relative path.

`create_app(ApplicationBindings(...))` does not discover or import the legacy
server. HTTP authorization is mandatory, routes are snapshotted after
registration, and runtime/child-router lifespans keep their nesting and yielded
request state. Direct route metadata remains visible to the native mutation
admission guard. Group and Autopilot tests use this same factory. Production
route handlers still reference legacy process state; constructing a second
factory instance with those same handlers does not isolate that legacy state.

The cross-chat ledger receives clocks, validators, instance identity and runtime
cancellation joining through `CrossChatBindings`. It does not import the server
or keep a parallel copy of its state. Its SQL, transaction boundaries and
cancellation semantics are retained. The six ledger fixtures import the actual
class; some surrounding handler fixtures still compile source fragments.

Further migration should separate remaining persistence, turns,
provider adapters and the remaining route families. Process state must move
behind explicit dependencies before those routes can be created as independent
applications. Do not move the monolith behind a wildcard import or duplicate
its mutable globals. Preserve durable queue admission/recovery, event sequence
allocation, WebSocket catch-up and provider-native session continuity throughout.
