# 后端瘦身与架构整理计划

状态：执行中。基线是 `main` 的 `e8cb56d`。

进度（2026-10-27）：

| 阶段 | 状态 | 提交 |
| --- | --- | --- |
| 0 安全网 | 部分完成：6 个已知失败测试中 3 个修好，另外 3 个随被删的功能一起删除；新增 `tests/test_route_inventory.py`，锁定客户端在用的路由；用隔离实例录了一份 GET 接口的响应形状快照用于比对。契约测试和磁盘兼容夹具仍待补 | 随阶段 1、2 提交 |
| 1 删除死代码 | 完成。`uninstall.sh` 的旧单体分支暂留：命名实例管理仍走这条路径 | `513c31f` |
| 2 删除无人使用的功能 | 完成，按 §4 的决定：终端保留（Mac 端已接入终端面板），工作区浏览、自定义端点、Codex 登录管理、跨聊天全部删除。Codex 的自动登录切换（用户在终端里 `codex login` 后新 turn 自动用新登录）保留，它属于原生登录的一部分，不是管理界面 | `8be5cfe` `3a24cb8` `3fc39b8` `4880c0d` |
| 3 在原文件内理清边界 | 完成：事件改走事件总线（`hyadex/events.py`）；会话存储的锁只能经 `STORE.transaction()`；20 个运行态全局收拢进 `RunState`（`hyadex/core/runs.py`）；鉴权声明在路由上（`route_auth`），中间件不再保存路径。另修复 Claude 上下文显示问题 | `c701f2b` `8301652` `04ab2b0` `b565f8d` |
| 4–6 | 未开始 | |

阶段 2 结束时：`agent_server.py` 从 79,132 行降到约 55,500 行；路由从 198 个降到 109 个（不计 HEAD）；测试 2,934 个全部通过（删除的是被删功能的测试）。

## 1. 现状

| 项目 | 数量 |
| --- | --- |
| 后端 Python 总量 | 约 117,000 行 |
| `agent_server.py` | 79,132 行，约 1,600 个顶层函数，153 个内联路由 |
| 其余顶层模块 | 45 个，约 30,000 行 |
| `hyadex/` 包 | 约 17,700 行 |
| HTTP/WS 路由 | 200 个 |
| 测试 | 217 个文件，4,230 个用例，其中 6 个失败（原因见 §2.5） |
| Mac 客户端 | 约 11,700 行，用到 41 个路由 |

`agent_server.py` 内部没有分区注释。按函数前缀和调用关系，可以分成这些域（行号是近似值）：

- **配置/常量** 1–1.5k
- **工作区文件** 1.5k–3.3k
- **Git 差异** 3.4k–4.4k
- **事件序号与索引** 4.4k–4.9k
- **鉴权** 4.9k–5.3k
- **会话存储 `SessionStore`** 8.5k–9.8k
- **定时任务 `JobStore` 与调度器** 9.8k–11.8k
- **推送中心 `SubscriberHub`** 11.8k–12.3k
- **全局运行态** 12.3k–12.9k
- **事件追加** 12.9k–13.4k
- **队列与转向（steer）** 17.4k–21.5k
- **终端/tmux** 22.5k–24.3k
- **时间线索引** 26.9k–30.6k
- **跨聊天投递** 32.2k–39.4k
- **历史同步与导入** 41.7k–43.8k
- **上下文用量** 45.7k–46.6k
- **审批交互** 46.6k–47.6k
- **provider 管理器** 48.5k–49.6k
- **Claude 运行器** 55.9k–58.8k
- **Codex 运行器** 58.8k–62.4k
- **turn 准入** 62.4k–63.8k
- **生命周期 `lifespan`** 65.4k–65.7k
- **路由** 65.7k–79k

### 1.1 结构性问题

这些是要整理的对象，不只是删代码。

1. **底层依赖上层。** 写事件的最底层函数 `append_event` 直接调用 `HUB.broadcast` 和 `GROUPS.observe_member_event`（12998、13226），持久化层反向依赖讨论组引擎。
2. **锁被外部直接使用。** 会话存储的私有锁 `STORE._lock` 在 55 个函数、65 处被外部直接拿；会话 dict 被原地修改。任何域都能改任何会话的任何字段。
3. **运行态是散落的全局字典。** `ACTIVE`、`BUSY_SESSIONS`、`CURRENT_TURNS`、`QUEUED_TURNS`、`RUN_METADATA`、`STOP_REQUESTS` 等由准入、两个运行器、停止、队列、跨聊天共同读写，没有一个对象对 turn 的生命周期负责。
4. **讨论组适配层越界。** `HyadexGroupRuntime` 直接调用路由处理函数（`stop_turn_endpoint`、`update_session`、`put_claude_goal`），直接改 `JOBS.jobs` 并手动盖 `_revision`。Autopilot 用的是讨论组引擎的私有成员（`_append_message`、`groups`、`lock`）。
5. **跨聊天和队列互相改对方的数据。** 跨聊天直接改 `QUEUED_TURNS`，`_start_turn_locked` 里引用了 15 次 `CROSS_CHAT`。
6. **鉴权规则写死路径。** `require_agent_token` 里硬编码了一组 admin 路径，`AGENT_HELPER_ROUTE_RULES` 用正则列出 helper 路由。删或改路由都要同步改这两处。
7. **巨型函数。** 23 个函数超过 300 行，例如 `run_codex_app_server` 2,908 行、`run_claude_sdk` 1,750 行、`_build_timeline_index_locked` 1,311 行、`_start_turn_locked` 1,280 行、`stop_turn` 926 行。
8. **测试绑死全局变量。** 测试里直接引用 `agent_server.STORE` 884 次、`CROSS_CHAT` 432 次、`ACTIVE` 240 次、`QUEUED_TURNS` 235 次。这是拆分时最大的隐性成本。
9. **生命周期不完整。** `lifespan` 直接引用约 30 个私有全局。`GROUPS.drain()` 和 `AUTOPILOT.drain()` 写好了但从未被调用。

好消息：没有循环导入，没有任何模块 import `agent_server`；`hyadex/routes/*` 已经是依赖注入写法，可以作为后续的范本。

## 2. 路由与模块盘点

### 2.1 谁在调用

服务端从不通过 HTTP 调用自己。仓库里只有三类真实调用方：

- **Mac 客户端：** 41 个路由。
- **智能体 helper CLI：** `hyadex_{chats,jobs,publish,emergency,notify}.py`，只用 15 个 `/api/agent/*` 路由。
- **安装/服务管理：** 只用 `/api/health` 和 `/api/admin/execution/*`。

其余路由只被测试调用，是给旧的 AgentsDock 桌面端和移动端准备的。

| 结论 | 路由数 |
| --- | --- |
| Mac 在用 | 41 |
| helper 在用 | 15 |
| 计划中的客户端功能可能要用（待你决定，见 §4） | 93 |
| 无人使用，可删 | 51 |

### 2.2 可直接删除的路由（51 个，约 4,300 行实现）

| 功能 | 约行数 |
| --- | --- |
| 交接摘要 digest | 1,250 |
| 导入本机 CLI 会话（`local-sessions`、`bulk-import`） | 780 |
| Codex 高级操作（load/rotate/rollback/review/shell/background-terminals/permission-profiles）、`claude/mcp`、`provider/reload` | 640 |
| 时间线置顶 pins | 500 |
| 手动管理 agent 交接路由的 CRUD（`@Chat` 内联授权是另一条路径，不受影响） | 340 |
| 端口隧道 | 330 |
| 历史维护、导入、去重路由（修复引擎本身保留） | 160 |
| 空聊天 dismiss/discard、侧栏排序 order | 180 |

小项合计约 300 行：

- 用户侧跨聊天别名和账本；
- 用户 inbox；
- 会话级 jobs 重复路由；
- 无会话作用域的 `/api/files/{id}`；
- `files/{id}/event`；
- `processes`；
- `diagnostics/host`；
- `skip-cross-chat-delivery`；
- 工作区里重复注册的 GET/HEAD。

### 2.3 已经死掉的模块（约 4,300 行，另有约 12 个测试文件）

`execution_activation`、`execution_durability`、`execution_legacy_runner`、`execution_update_status`、`update_runner`、`update_preparation`、`update_recovery`。

我核对过：它们只被彼此和测试引用。`update_runner` 另外还被 `test_server_instances` 借用了一个函数。

### 2.4 半死模块（约 3,900 行）

`activation_transaction`（2,458 行）、`execution_preparation`、`execution_recovery`（连同 `execution_recovery_status`）、`update_handoff`。

现在的安装/卸载流程只用到其中几个小函数：

- `pending_execution_worker_operation`
- `_binding` / `_check_binding`
- `require_retired_owners`
- `_installed_layout` / `_no_activation`

把这些函数挪进 `execution_install`/`execution_manage` 之后，四个模块就能整体删除。

另外还有几项可以一起处理：

- **如果不再走 npm 分发**，可以删：`npm/`、三个打包脚本、`systemd/*.example`（它描述的还是旧的单服务布局）、`uninstall.sh` 里的旧单体分支。
- **`pair.cjs` 先别删。** 它生成配对二维码，Android 客户端配对时可能用得上。
- **`hyadex/inventory.py` 要同步更新。** 它的安装清单里仍然列着全部 20 个 `execution_*`/`update_*`/`activation_*` 文件，这些文件现在都会被装到用户机器上。

### 2.5 六个失败的测试

都是环境假设或测试本身的问题，不是产品缺陷：

| 测试 | 原因 | 处理 |
| --- | --- | --- |
| 2 个安装权限测试 | 本机 umask 是 077，`mkdir(mode=0o755)` 实际得到的是 0700 | 测试里显式 `chmod` |
| 2 个历史测试 | macOS 的 `/var` 是指向 `/private/var` 的软链接，代码返回的是 resolve 后的路径，测试拿未 resolve 的路径去比 | 测试改为比较 `resolve()` 之后的路径 |
| 1 个 Codex 测试 | 只 mock 了管理器，没 mock 本机真实 codex 二进制的身份探测 | 补 patch |
| 1 个 npm 测试 | 本机 `python3` 是 3.9 | 随 npm 分发一起删掉 |

## 3. 目标架构

### 3.1 原则

1. **单向依赖：** `api` → `features` → `core`。`providers` 实现统一接口，被 `core.turns` 调用。下层永远不 import 上层。
2. **一个状态一个主人。**
   - 会话字段只能经 `SessionStore` 的方法修改，外部不再拿它的锁；
   - turn 的生命周期集中在一个 `RunRegistry` 里（替代 `ACTIVE`/`BUSY`/`CURRENT_TURNS`/`RUN_METADATA`/`STOP_REQUESTS`）；
   - 队列由 `TurnQueue` 负责。
3. **事件总线代替硬钩子。** `EventLog.append()` 写盘后发布到 `EventBus`。WebSocket 推送、讨论组观察成员事件、标题生成等都订阅总线，事件层不再认识讨论组。
4. **依赖注入代替模块全局。**
   - `create_app()` 构造一个 `Services` 容器，各路由用 FastAPI 依赖取用；
   - 测试用 fixture 构造隔离的 `Services`，不再 patch 全局。
5. **鉴权声明在路由上。** 每个 router 声明自己的权限依赖（`user` / `agent_helper` / `admin`），中间件不再维护路径清单。
6. **兼容性不变。**
   - 磁盘格式不变：`sessions.json`、事件 JSONL、`groups/`、`autopilot/`、`jobs.json`、`files/`、`code_diffs/`、两个 SQLite 文件；
   - Mac 用到的 41 个路由和 helper 用到的 15 个路由，请求和响应形状不变。

### 3.2 目标目录

```
hyadex/
  app.py                 create_app(): 组装 Services、routers、lifespan
  config.py              环境变量 → Settings，路径集中在这里
  services.py            Services 容器（构造顺序 = 依赖顺序）
  core/
    auth.py              token 校验，user/agent/admin 三个依赖
    events.py            EventLog（序号、追加、读取、补发）+ EventBus
    hub.py               WebSocket 订阅与推送（订阅 EventBus）
    sessions.py          SessionStore：只暴露方法，内部加锁
    runs.py              RunRegistry：一个 turn 从准入到结束的全部状态
    queue.py             TurnQueue：排队、编辑、移动、立即运行
    admission.py         start_turn（拆分自 _start_turn_locked）
    stop.py              停止（拆分自 stop_turn）
    recovery.py          重启后的队列和 turn 恢复
  providers/
    base.py              Provider 协议：run_turn / stop / resolve_interaction /
                         runtime / context_usage / catalog / usage / goals
    claude/              sdk_runner, print_runner, projection, interactions,
                         usage, catalog, goals, history, side_question
    codex/               app_server（管理器）, runner, exec_runner, projection,
                         interactions, threads, goals, history, auth, endpoints
  features/
    groups/  autopilot/  jobs/  files/  workspace/  diffs/  terminal/
    titles/  search/  timeline/  alerts/  side_questions/  mailbox/（视 §4 决定）
  api/
    health.py sessions.py turns.py interactions.py files.py runtime.py
    groups.py autopilot.py jobs.py agent.py（helper 专用）admin.py
  ops/
    service.py（worker/gateway）transport.py control.py install.py manage.py
    uninstall.py instances.py
cli/
  hyadex_jobs.py hyadex_publish.py hyadex_notify.py …（agent 在会话里运行的 helper）
agent_server.py          过渡期的兼容门面：从新模块 re-export 旧名字，测试迁移完后删除
```

### 3.3 关键接口

```python
class Provider(Protocol):
    backend: str
    async def run_turn(self, turn: TurnContext, sink: EventSink) -> TurnResult: ...
    async def stop(self, session_id: str, run_id: str) -> None: ...
    async def resolve_interaction(self, session_id: str, interaction_id: str, response: dict) -> None: ...
    async def runtime(self, session_id: str) -> RuntimeSnapshot: ...      # 含 context usage、权限模式
    async def catalog(self, refresh: bool) -> Catalog: ...                # 模型与推理强度
    async def account_usage(self, refresh: bool) -> AccountUsage: ...

class EventLog:
    def append(self, session_id: str, type: str, payload: dict) -> Event: ...   # 写盘后发布到 bus
    def read(self, session_id: str, after: int = 0, limit: int = 500) -> list[Event]: ...

class SessionStore:
    def get(self, session_id: str) -> Session: ...
    def update(self, session_id: str, **changes) -> Session: ...          # 唯一修改入口
```

运行器只做两件事：调用 provider，并把它的输出翻译成事件写进 `EventSink`。准入、排队、停止、恢复都在 `core` 里实现，两个 provider 共用一套。

## 4. 需要你决定的功能

下表这些功能现在后端有实现、客户端没在用。前面写的是我的建议。

| 功能 | 后端规模 | 建议 | 理由 |
| --- | --- | --- | --- |
| **跨聊天消息（agent 之间互发消息、交接）** | `agent_server` 内约 12.5k 行，加上 store/mailbox/hyadex_chats 约 4k 行 | **删除**；或冻结、先不迁移 | 体量最大、耦合最深（队列、准入、鉴权）。讨论组已经覆盖"多个 agent 协作"，而且更省 token。删掉它是这次整理收益最大的一步 |
| 附件上传、历史分页、搜索、fork、删除聊天、未读 | 小 | 保留 | 客户端近期就需要 |
| 新建/删除定时任务、运行历史；讨论组和 Autopilot 的修改/删除、讨论组 jobs | 小 | 保留 | 客户端近期就需要 |
| goals（Claude/Codex 原生目标） | 约 1.6k 行 | 保留 | 讨论组和 Autopilot 依赖它 |
| 队列编辑、移动、立即运行；compact；刷新上下文 | 小 | 保留 | 客户端近期就需要 |
| 原生命令/技能（provider-commands） | 小 | 保留 | 对齐原生 CLI 的 `/命令` |
| side questions（`/btw` 旁路提问） | 约 1.8k 行 | 保留，排在后面接入 | 原生 Claude、Codex 都有这个功能 |
| 终端/tmux | 约 2.2k 行加路由 | 待定 | 原生 CLI 的桌面端有终端面板；Mac 端暂时没有计划 |
| 工作区文件浏览与编辑 | 约 1.5k 行加路由 | 待定 | 与"在 Finder/编辑器里打开"有重叠 |
| 自定义 Codex 端点、Codex 登录管理、provider 账户设置 | 约 2.1k 行，加 23 个 admin 路由 | 待定 | 不用自定义端点就可以删；用的话要做设置页 |
| 子代理（subagents）快照 | 约 2.1k 行 | 保留（只读） | 显示 Claude/Codex 的子代理状态，与原生对齐 |
| 紧急告警与通知 | 小 | 保留 | 以后接 Android 推送 |
| npm 分发与 `hyadex` CLI | `npm/` 加脚本 | 删除，保留 `pair.cjs` | 现在只走源码安装 |
| 导入本机已有的 CLI 会话 | 约 780 行 | 删除 | 已列入 §2.2 |

如果按建议执行（删除跨聊天、删除 npm，其余保留），后端会从约 11.7 万行降到约 8.5 万–9 万行。估算：

- 阶段 1 删除约 8k 行；
- §2.2 的路由删除约 4.3k 行；
- 跨聊天删除约 16k 行。

如果终端、工作区、自定义端点也一并删除，还能再减约 6k 行。拆分完成后，最大的单个文件预计不超过 2,000 行。

## 5. 分阶段执行

每个阶段结束时必须同时满足：

- 全量测试全绿；
- Mac 客户端的端到端检查（LiveCheck：聊天、审批、讨论组、Autopilot）通过；
- 在隔离实例上用旧状态目录启动成功，能读出原有聊天。

每个阶段单独提交，可以单独回滚。

### 阶段 0：安全网（先做，约 1 天）

1. **修好 6 个失败测试**（§2.5），全量全绿后，把 `.github/workflows/server-ci.yml` 设为合并门槛。
2. **契约测试。** 对 Mac 用到的 41 个路由和 helper 用到的 15 个路由，在隔离实例上录下请求和响应的"形状快照"（字段名、类型、事件类型集合）。之后每个阶段都要比对，形状不一致就失败。
3. **磁盘兼容测试。** 准备一份真实的旧状态目录夹具（脱敏），作为启动和读取的回归用例。
4. **路由使用统计。** 加一个默认关闭的计数中间件，你日常使用一周，用实际数据核对 §2.1 的结论。
5. **合并工作目录。** 把 Codex 的 `refactor/hyadex-slimdown` 分支对齐到 `main`。此后规定 `agent_server.py` 同一时间只允许一个 agent 修改，避免巨型文件冲突。

### 阶段 1：删除死代码（低风险）

1. 删除 §2.3 的 7 个死模块及其测试，同步更新 `hyadex/inventory.py`、打包脚本和 `test_server_instances`。
2. 把 §2.4 需要的几个函数迁入 `execution_install`/`execution_manage`，再删除那 4 个半死模块。
3. 删除 npm 分发、旧的 systemd 示例、`uninstall.sh` 的旧单体分支；`pair.cjs` 移到 `ops/`。
4. 修订 `SERVER_REFERENCE.md` 和 `BACKEND_STATUS.md`。

预计删除约 8,000 行代码和约 20 个测试文件。

### 阶段 2：删除无人使用的功能（按 §4 的决定）

1. 删除 §2.2 的 51 个路由及其实现，同步清理中间件路径清单里对应的条目。
2. 删除跨聊天前，先确认一件事：helper 里的 `hyadex_jobs`/`publish`/`notify`/`emergency` 走的是 `execute_provider_tool` 这条公共通道，不依赖跨聊天授权，删除后必须保留这条通道。具体做法：
   - 把 provider 工具执行从跨聊天授权里剥离出来；
   - 删除 `hyadex_chats`、mailbox、cross_chat store 和对应路由；
   - 清理 `_start_turn_locked` 和队列里的跨聊天分支；
   - 删除 `cross_chat.sqlite3` 的读写，但旧文件保留在磁盘上，不删；
   - 更新提示词里关于 `@Chat` 的说明。
3. 删除其他被你否决的功能（终端、工作区、自定义端点等）。

### 阶段 3：在原文件内理清边界（还不搬文件）

先把耦合理顺再拆文件，这样每一步的改动都比较小、容易审：

1. **EventBus：** `append_event` 只写盘和发布；讨论组、推送改为订阅。
2. **SessionStore 方法化：** 逐个替换 65 处外部加锁，改为调用 `update()` 或专用方法。
3. **RunRegistry：** 把运行态全局字典收拢进一个对象；准入、运行器、停止、恢复都只经它的接口读写。
4. **讨论组适配层改用公开接口：** 停止 turn、更新会话、设置目标、创建任务都调服务方法，不再调路由函数。Autopilot 改用讨论组引擎的公开方法。
5. **鉴权改为路由依赖：** 删除路径清单和正则规则。
6. 调用 `GROUPS.drain()`/`AUTOPILOT.drain()`，补全关闭流程。

### 阶段 4：拆成包（由下往上，一个域一次提交）

顺序：config/paths → auth → events/hub → sessions → files/diffs/workspace → jobs → titles/search/timeline → context usage → interactions → providers（先 Codex 后 Claude）→ admission/queue/stop/recovery → api routers → lifespan。

- 每搬完一个域，`agent_server.py` 都保留同名 re-export，旧测试无需改动；
- 同时把这个域的测试迁移到新 fixture；
- 全部迁移完成后，删掉兼容门面；
- `execution_service.py` 的启动目标从 `"agent_server:app"` 改为 `"hyadex.app:app"`。

### 阶段 5：拆巨型函数

`run_codex_app_server`、`run_claude_sdk`、`_build_timeline_index_locked`、`_start_turn_locked`、`stop_turn` 按阶段拆成小函数或事件处理表。

改之前先补"特征测试"：用录下的真实事件流作为输入，对输出做快照，保证重构前后行为一致。

### 阶段 6：原生 CLI 兼容检查与文档

1. 新增 `scripts/check_provider_contracts.py`，每次升级 Claude Code 或 Codex 后运行，检查：
   - 模型目录能解析；
   - 权限模式列表包含客户端已知的值；
   - `get_usage` 的结构；
   - 审批请求的字段。
2. 重写 `ARCHITECTURE.md`（按 §3），把 `SERVER_REFERENCE.md` 精简到只剩保留下来的接口。

## 6. 分工与风险

- **分工：**
  - **Codex** 负责后端阶段 0–5，在一个分支上串行做；
  - **我（Claude）** 负责契约测试、Mac 端 LiveCheck 验收，以及客户端随后端变化做的配合修改。

  每个阶段结束都由你确认，再合并到 `main`。
- **风险与对策：**

| 风险 | 对策 |
| --- | --- |
| 恢复类代码出回归（重启后的队列和 turn 恢复） | 现有大量测试覆盖这些路径，拆分期间一个都不删 |
| 测试大量依赖全局变量，迁移量大 | 兼容门面让旧测试先继续跑，按域分批迁移 |
| 删除跨聊天后，agent 的 helper 失效 | 阶段 2 第 2 条先剥离 provider 工具通道；契约测试覆盖 helper 用的 15 个路由 |
| 两个 agent 同时改同一个巨型文件 | 阶段 0 规定 `agent_server.py` 同一时间只允许一个 agent 修改 |
