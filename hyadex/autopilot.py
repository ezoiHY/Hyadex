"""Autopilot: one agent plans, another implements, a reviewer checks each step.

An autopilot run is a state machine driven by the server, not by an agent:

    plan -> [approve] -> for each task: execute -> verify -> review
              ^                              |          |
              +---------- replan ------------+  revise -+

Agents only produce structured results at three points (a plan, a step report
and a review verdict). The server decides what happens next, runs the user's
verification command itself (inside the Codex sandbox by default) and commits
exactly the reviewed tree as one checkpoint per task.

Runs are built on group discussions: each run owns a group (kind
``autopilot``) whose members are the planner, the executor and an optional
reviewer, so dispatch, recovery, stop and the event stream are the group
engine's. Every request is self-contained and addressed with an ``audience``,
so each agent receives only what its role needs.

Options (all per run):
* ``approval``: ``plan`` (approve the plan, then automatic), ``step`` (also
  approve after every step) or ``none`` (fully automatic).
* ``isolation``: ``worktree`` (a new branch and worktree; one commit per
  approved task) or ``in_place`` (edit the working directory, no commits).
* ``reviewer``: ``planner`` (the planning session reviews), ``fresh`` (a
  separate reviewer agent) or ``none`` (the verification command decides).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from hyadex import groups as group_chat
from hyadex.groups import GroupEngine, ParticipantSpec, PostGroupMessageRequest


logger = logging.getLogger("hyadex.autopilot")

RUN_ID_RE = re.compile(r"^run_[0-9a-f]{16}$")
ACTIVE_STATUSES = {"planning", "executing", "verifying", "reviewing"}
FINAL_STATUSES = {"done", "aborted"}
INLINE_DIFF_CHARS = 8_000
MAX_DIFF_BYTES = 2_000_000
MAX_NAME_BYTES = 1_000_000
VERIFY_OUTPUT_TAIL_BYTES = 4_000
MAX_REPLY_PARSE_CHARS = 200_000
MAX_LISTED_FILES = 200
MAX_STAT_CHARS = 4_000
MAX_REPORT_CHARS = 4_000
MAX_RECORDED_REVIEWS = 50
GOAL_CONTEXT_CHARS = 4_000
AUTOPILOT_DELTA_LIMIT = 50_000
GIT_TIMEOUT_SECONDS = 120
VERIFY_KILL_GRACE_SECONDS = 10
# Safe for anything an agent may have written into the working tree:
# no hooks, no fsmonitor, no signing prompts.
GIT_SAFE_CONFIG = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false")
GIT_DIFF = ("diff", "--no-ext-diff", "--no-textconv", "--no-color")
GIT_IDENTITY_ENV = {"GIT_AUTHOR_NAME": "Hyadex Autopilot", "GIT_AUTHOR_EMAIL": "autopilot@hyadex.invalid",
                    "GIT_COMMITTER_NAME": "Hyadex Autopilot", "GIT_COMMITTER_EMAIL": "autopilot@hyadex.invalid"}
# Verification gets no server or provider credentials.
SECRET_ENV_PREFIXES = ("HYADEX_", "AGENTSDOCK_", "AGENTS_SERVER_", "ZENITHDOCK_", "ZENITHBOT_", "ANTHROPIC_",
                       "OPENAI_", "CLAUDE_", "CLAUDECODE", "CODEX_", "MCP_", "AWS_", "AZURE_", "GOOGLE_", "GH_",
                       "GITHUB_")
SECRET_ENV_PARTS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "PASSWD", "CREDENTIAL", "COOKIE", "AUTH")
ROLE_HANDLES = {"planner": "planner", "executor": "executor", "reviewer": "reviewer"}
NOTE_AUDIENCE = ["user"]  # "user" is a reserved handle: agents never receive notes


def now() -> float:
    return time.time()


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def fence(text: str, language: str = "") -> str:
    """A Markdown fence that the enclosed text cannot close."""

    longest = max((len(run) for run in re.findall(r"`+", text or "")), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}{language}\n{text}\n{ticks}"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class AgentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: group_chat.BackendName
    model: str | None = Field(default=None, max_length=200)
    effort: str | None = Field(default=None, max_length=40)
    claude_permission_mode: Literal["default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"] | None = None
    codex_approval_policy: Literal["never", "on-request", "untrusted"] | None = None
    codex_sandbox_mode: Literal["read-only", "workspace-write", "danger-full-access"] | None = None


class Budgets(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_tasks: int = Field(default=12, ge=1, le=30)
    max_revisions: int = Field(default=2, ge=0, le=5)
    max_replans: int = Field(default=2, ge=0, le=5)


class PlanTask(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=1, max_length=200)
    detail: str = Field(default="", max_length=8_000)
    files: list[str] = Field(default_factory=list, max_length=50)
    acceptance: list[str] = Field(default_factory=list, max_length=20)


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, max_length=20_000)
    title: str | None = Field(default=None, max_length=200)
    cwd: str | None = Field(default=None, max_length=4096)
    constraints: str | None = Field(default=None, max_length=8_000)
    approval: Literal["plan", "step", "none"] = "plan"
    isolation: Literal["worktree", "in_place"] = "worktree"
    reviewer: Literal["planner", "fresh", "none"] = "planner"
    planner: AgentSpec = Field(default_factory=lambda: AgentSpec(backend="claude"))
    executor: AgentSpec = Field(default_factory=lambda: AgentSpec(backend="codex"))
    reviewer_agent: AgentSpec | None = None
    verify_command: str | None = Field(default=None, max_length=2_000)
    verify_sandbox: Literal["codex", "none"] = "codex"
    verify_timeout_seconds: int = Field(default=600, ge=10, le=3_600)
    budgets: Budgets = Field(default_factory=Budgets)


class ApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tasks: list[PlanTask] | None = Field(default=None, min_length=1, max_length=30)


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=20_000)


# ---------------------------------------------------------------------------
# Structured replies (call through asyncio.to_thread: replies can be large)
# ---------------------------------------------------------------------------

def _fenced_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    position = 0
    while True:
        start = text.find("```", position)
        if start < 0:
            return blocks
        body = text.find("\n", start)
        if body < 0:
            return blocks
        end = text.find("```", body)
        if end < 0:
            return blocks
        blocks.append(text[body + 1:end])
        position = end + 3


def extract_json(text: str) -> dict[str, Any] | None:
    """Return the last top-level JSON object in a reply (fenced blocks preferred)."""

    text = (text or "")[-MAX_REPLY_PARSE_CHARS:]
    for block in reversed(_fenced_blocks(text)):
        with suppress(ValueError):
            value = json.loads(block.strip())
            if isinstance(value, dict):
                return value
    decoder = json.JSONDecoder()
    found: dict[str, Any] | None = None
    position = text.find("{")
    attempts = 0
    while position >= 0 and attempts < 200:
        attempts += 1
        try:
            value, end = decoder.raw_decode(text, position)
        except ValueError:
            position = text.find("{", position + 1)
            continue
        if isinstance(value, dict):
            found = value
        position = text.find("{", end)
    return found


def parse_plan(text: str, max_tasks: int) -> tuple[dict[str, Any] | None, str | None]:
    data = extract_json(text)
    if data is None:
        return None, "no JSON object was found"
    raw_tasks = data.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        return None, '"tasks" must be a non-empty list'
    if len(raw_tasks) > max_tasks:
        return None, f"the plan has {len(raw_tasks)} tasks; the limit is {max_tasks}"
    tasks = []
    for number, raw in enumerate(raw_tasks, 1):
        try:
            tasks.append(PlanTask.model_validate(raw).model_dump())
        except Exception as exc:
            return None, f"task {number} is invalid: {exc}"
    return {"summary": str(data.get("summary") or "")[:4_000], "tasks": tasks}, None


def parse_step_report(text: str) -> dict[str, Any]:
    data = extract_json(text) or {}
    status = str(data.get("status") or "done").lower()
    return {
        "status": "blocked" if status == "blocked" else "done",
        "summary": str(data.get("summary") or text or "")[:MAX_REPORT_CHARS],
        "notes": str(data.get("notes") or "")[:MAX_REPORT_CHARS],
    }


def parse_verdict(text: str) -> tuple[dict[str, Any] | None, str | None]:
    data = extract_json(text)
    if data is None:
        return None, "no JSON object was found"
    verdict = str(data.get("verdict") or "").lower()
    if verdict not in {"approve", "revise", "replan"}:
        return None, '"verdict" must be approve, revise or replan'
    return {"verdict": verdict, "notes": str(data.get("notes") or "")[:4_000],
            "instructions": str(data.get("instructions") or "")[:8_000]}, None


# ---------------------------------------------------------------------------
# Git (run in threads)
# ---------------------------------------------------------------------------

class GitError(RuntimeError):
    pass


def _git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"})
    environment.update(extra or {})
    return environment


def git(cwd: str | Path, *args: str, env: dict[str, str] | None = None, limit: int | None = None) -> str:
    command = ["git", *GIT_SAFE_CONFIG, *args]
    if limit is None:
        try:
            result = subprocess.run(command, cwd=str(cwd), env=_git_env(env), stdin=subprocess.DEVNULL,
                                    capture_output=True, timeout=GIT_TIMEOUT_SECONDS)
        except (OSError, subprocess.SubprocessError) as exc:
            raise GitError(f"git {args[0]} failed: {exc}") from exc
        if result.returncode != 0:
            raise GitError(result.stderr.decode("utf-8", "replace").strip()[:2_000] or f"git {args[0]} failed")
        return result.stdout.decode("utf-8", "replace")
    # Bounded read: never hold more than ``limit`` bytes of output in memory.
    try:
        process = subprocess.Popen(command, cwd=str(cwd), env=_git_env(env), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise GitError(f"git {args[0]} failed: {exc}") from exc
    truncated = False
    try:
        data = process.stdout.read(limit)
        truncated = bool(process.stdout.read(1))
        if truncated:
            process.kill()  # enough output; stop the rest
        process.wait(timeout=GIT_TIMEOUT_SECONDS)
    finally:
        if process.poll() is None:
            process.kill()
            with suppress(subprocess.SubprocessError):
                process.wait(timeout=GIT_TIMEOUT_SECONDS)
        process.stdout.close()
    if not truncated and process.returncode != 0:
        raise GitError(f"git {args[0]} failed")
    return data.decode("utf-8", "replace")


def repository_root(cwd: str) -> str | None:
    try:
        return git(cwd, "rev-parse", "--show-toplevel").strip() or None
    except GitError:
        return None


def create_worktree(repo: str, path: Path, branch: str) -> str:
    base = git(repo, "rev-parse", "HEAD").strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        git(repo, "worktree", "add", "-q", "-b", branch, str(path), base)
    except GitError:
        remove_worktree(repo, path)
        with suppress(GitError):
            git(repo, "branch", "-D", branch)
        raise
    return base


def remove_worktree(repo: str, path: Path) -> None:
    with suppress(GitError):
        git(repo, "worktree", "remove", "--force", str(path))
    shutil.rmtree(path, ignore_errors=True)
    with suppress(GitError):
        git(repo, "worktree", "prune")


def _index_path(workdir: str) -> Path:
    raw = git(workdir, "rev-parse", "--git-path", "index").strip()
    path = Path(raw)
    return path if path.is_absolute() else Path(workdir) / path


def snapshot_tree(workdir: str) -> str | None:
    """Write the working tree (tracked and untracked, honoring .gitignore) as a tree object.

    Uses a temporary index (seeded from the real one so unchanged files are not
    re-hashed); the real index and the working tree are never modified.
    Returns None outside a Git worktree.
    """

    if repository_root(workdir) is None:
        return None
    with tempfile.TemporaryDirectory(prefix="hyadex-autopilot-") as temporary:
        index = os.path.join(temporary, "index")
        env = {"GIT_INDEX_FILE": index}
        with suppress(GitError, OSError):
            shutil.copyfile(_index_path(workdir), index)
        try:
            git(workdir, "add", "-A", env=env)
        except GitError:
            # A torn copy of a busy index: start from HEAD instead.
            with suppress(OSError):
                os.remove(index)
            with suppress(GitError):
                git(workdir, "read-tree", "HEAD", env=env)
            git(workdir, "add", "-A", env=env)
        return git(workdir, "write-tree", env=env).strip()


def tree_diff(workdir: str, before: str | None, after: str | None) -> dict[str, Any]:
    if not before or not after:
        return {"available": False, "files": [], "file_count": 0, "stat": "", "diff": "", "truncated": False}
    names = [line for line in git(workdir, *GIT_DIFF, "--name-only", before, after, limit=MAX_NAME_BYTES).splitlines()
             if line]
    stat = git(workdir, *GIT_DIFF, "--stat", before, after, limit=MAX_NAME_BYTES)[:MAX_STAT_CHARS]
    diff = git(workdir, *GIT_DIFF, before, after, limit=MAX_DIFF_BYTES)
    return {"available": True, "files": names[:MAX_LISTED_FILES], "file_count": len(names), "stat": stat.strip(),
            "diff": diff, "truncated": len(diff.encode("utf-8")) >= MAX_DIFF_BYTES}


def commit_tree(workdir: str, branch: str, tree: str, message: str) -> str | None:
    """Commit exactly ``tree`` on top of the run's branch; None when nothing changed."""

    ref = f"refs/heads/{branch}"
    tip = git(workdir, "rev-parse", "--verify", ref).strip()
    if git(workdir, "rev-parse", f"{tip}^{{tree}}").strip() == tree:
        return None
    commit = git(workdir, "commit-tree", tree, "-p", tip, "-m", message, env=GIT_IDENTITY_ENV).strip()
    git(workdir, "update-ref", ref, commit, tip)
    # The working files already equal ``tree``; align the worktree's index.
    git(workdir, "reset", "-q")
    return commit


def discard_uncommitted(workdir: str) -> None:
    """Return a run's own worktree to its branch tip (never used in place)."""

    git(workdir, "reset", "-q", "--hard")
    git(workdir, "clean", "-fdq")


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verification_env() -> dict[str, str]:
    environment: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper.startswith(SECRET_ENV_PREFIXES) or any(part in upper for part in SECRET_ENV_PARTS):
            continue
        environment[key] = value
    # Byproducts that are not ignored become part of the reviewed step; skip the commonest one.
    environment.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    return environment


def verification_argv(command: str, *, sandbox: str, runner: str | None, writable_root: str) -> list[str]:
    shell = ["/bin/sh", "-c", command]
    if sandbox == "none":
        return shell
    if not runner:
        raise GitError("sandboxed verification needs the Codex CLI on the server")
    # Same boundary as the executor: write only inside the work tree, no network.
    roots = json.dumps([writable_root])
    return [runner, "sandbox", "-c", 'sandbox_mode="workspace-write"',
            "-c", f"sandbox_workspace_write.writable_roots={roots}", "--", *shell]


def _kill_group(pid: int) -> None:
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


async def run_verification(argv: list[str], workdir: str, timeout: int,
                           register: Callable[[Any], None] | None = None) -> dict[str, Any]:
    """Run the user's verification command; it never comes from an agent."""

    started = now()
    command = argv[-1]
    with tempfile.TemporaryFile() as output:
        try:
            process = await asyncio.create_subprocess_exec(
                *argv, cwd=workdir, env=verification_env(), stdin=subprocess.DEVNULL,
                stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            return {"command": command, "passed": False, "exit_code": None, "timed_out": False,
                    "output_tail": f"could not run: {exc}", "seconds": 0.0}
        if register is not None:
            register(process)
        timed_out = False
        try:
            await asyncio.wait_for(process.wait(), timeout)
        except asyncio.TimeoutError:
            timed_out = True
            _kill_group(process.pid)
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), VERIFY_KILL_GRACE_SECONDS)
        except asyncio.CancelledError:
            _kill_group(process.pid)
            raise
        size = output.seek(0, os.SEEK_END)
        output.seek(max(0, size - VERIFY_OUTPUT_TAIL_BYTES))
        tail = output.read().decode("utf-8", "replace")
    exit_code = None if timed_out else process.returncode
    return {"command": command, "passed": exit_code == 0, "exit_code": exit_code, "timed_out": timed_out,
            "output_tail": tail, "seconds": round(now() - started, 1)}


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class AutopilotEngine:
    def __init__(self, root: Path, groups: GroupEngine) -> None:
        self.root = root
        self.groups = groups
        self.runs: dict[str, dict[str, Any]] = {}
        self.by_group: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        # run id -> {"token": verification token, "process": the running process}
        self._verifying: dict[str, dict[str, Any]] = {}
        groups.listeners.append(self._on_group_event)

    # -- persistence -----------------------------------------------------------

    def run_dir(self, run_id: str) -> Path:
        return self.root / run_id

    def lock(self, run_id: str) -> asyncio.Lock:
        lock = self._locks.get(run_id)
        if lock is None:
            lock = self._locks[run_id] = asyncio.Lock()
        return lock

    async def save(self, run: dict[str, Any]) -> None:
        if self.runs.get(run["id"]) is not run:
            return  # deleted meanwhile; never recreate its folder
        run["updated_at"] = now()
        snapshot = json.loads(json.dumps(run))
        await asyncio.to_thread(group_chat.atomic_write_json, self.run_dir(run["id"]) / "run.json", snapshot)

    async def _write_file(self, run: dict[str, Any], name: str, text: str) -> str:
        path = self.run_dir(run["id"]) / name
        if self.runs.get(run["id"]) is run:
            def write() -> None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            await asyncio.to_thread(write)
        return str(path)

    async def load(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.runs.clear()
        self.by_group.clear()
        for entry in sorted(self.root.iterdir()):
            record = entry / "run.json"
            if not RUN_ID_RE.match(entry.name) or not record.is_file():
                continue
            try:
                run = json.loads(record.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                logger.exception("skipping unreadable autopilot run %s", record)
                continue
            if isinstance(run, dict) and run.get("id") == entry.name:
                self.runs[entry.name] = run
                if run.get("group_id"):
                    self.by_group[run["group_id"]] = entry.name

    async def recover(self) -> None:
        """Continue runs after a restart; call after the group engine recovered."""

        for run_id, run in list(self.runs.items()):
            if run.get("status") in FINAL_STATUSES:
                continue
            async with self.lock(run_id):
                try:
                    await self._recover_run(run)
                except Exception as exc:
                    logger.exception("autopilot recovery failed run=%s", run_id)
                    await self._needs_attention(run, "error", f"Recovery failed: {exc}")

    async def _recover_run(self, run: dict[str, Any]) -> None:
        status = run.get("status")
        if status == "verifying":
            await self._start_verification(run)
            return
        phase = run.get("phase") or {}
        group = self.groups.groups.get(run.get("group_id") or "")
        if phase.get("ask_id") and not phase.get("round_id") and group is not None:
            # Saved before the request was posted: find out whether it was.
            posted = next((item for item in reversed(await asyncio.to_thread(
                self.groups.read_events, run["group_id"], types={"message"}))
                if item.get("autopilot_ask_id") == phase["ask_id"]), None)
            if posted is not None:
                phase["round_id"] = posted.get("round_id")
                run["phase"] = phase
                await self.save(run)
        round_id = phase.get("round_id")
        if round_id and group is not None:
            round_ = group.get("rounds", {}).get(round_id)
            if round_ is None or round_.get("status") != "open":
                self._spawn(self._handle_round(run["id"], round_id))
        elif status in ACTIVE_STATUSES:
            await self._needs_attention(run, "interrupted", "Autopilot was interrupted by a restart. Resume to continue.")

    def _spawn(self, coroutine: Any) -> None:
        task = asyncio.get_running_loop().create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        while True:
            await self.groups.drain()
            pending = [task for task in self._tasks if not task.done()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
                continue
            await asyncio.sleep(0)
            if all(task.done() for task in self._tasks) and all(task.done() for task in self.groups._tasks):
                return

    # -- lookups -----------------------------------------------------------------

    def require(self, run_id: str) -> dict[str, Any]:
        run = self.runs.get(run_id) if RUN_ID_RE.match(run_id or "") else None
        if run is None:
            raise HTTPException(status_code=404, detail="autopilot run not found")
        return run

    @staticmethod
    def reviewer_handle(run: dict[str, Any]) -> str | None:
        return {"planner": "planner", "fresh": "reviewer"}.get(run.get("reviewer"))

    @staticmethod
    def current_task(run: dict[str, Any]) -> dict[str, Any] | None:
        index = run.get("task_index")
        tasks = (run.get("plan") or {}).get("tasks") or []
        return tasks[index] if isinstance(index, int) and 0 <= index < len(tasks) else None

    def public(self, run: dict[str, Any], *, summary: bool = False) -> dict[str, Any]:
        keys = ("id", "title", "status", "approval_kind", "approval", "isolation", "reviewer", "cwd", "workdir",
                "branch", "base_commit", "group_id", "task_index", "replans", "attention", "error",
                "created_at", "updated_at", "finished_at")
        result = {key: run.get(key) for key in keys}
        tasks = (run.get("plan") or {}).get("tasks") or []
        result["task_count"] = len(tasks)
        result["done_count"] = sum(task.get("status") == "done" for task in tasks)
        if summary:
            return result
        result.update({key: run.get(key) for key in (
            "goal", "constraints", "verify_command", "verify_sandbox", "budgets", "plan", "commits", "last_step")})
        return result

    # -- creation ------------------------------------------------------------------

    @staticmethod
    def _participant(role: str, spec: AgentSpec) -> ParticipantSpec:
        fields = spec.model_dump(exclude_none=True)
        writer = role == "executor"
        if spec.backend == "claude" and "claude_permission_mode" not in fields:
            # Planner and reviewer read without asking; the executor may edit.
            fields["claude_permission_mode"] = "acceptEdits" if writer else "dontAsk"
        if spec.backend == "codex":
            fields.setdefault("codex_approval_policy", "never")
            fields.setdefault("codex_sandbox_mode", "workspace-write" if writer else "read-only")
        return ParticipantSpec(handle=ROLE_HANDLES[role], label=f"{role.title()} ({spec.backend})",
                               role=role, **fields)

    async def create(self, req: CreateRunRequest) -> dict[str, Any]:
        goal = req.goal.strip()
        if not goal:
            raise HTTPException(status_code=400, detail="the goal is empty")
        verify_command = (req.verify_command or "").strip() or None
        runner = None
        if verify_command and req.verify_sandbox == "codex":
            runner = shutil.which("codex")
            if runner is None:
                raise HTTPException(status_code=400, detail=(
                    "Sandboxed verification needs the Codex CLI on the server. Install it, or set "
                    "verify_sandbox to none to run the command without a sandbox."))
        cwd = self.groups.runtime.resolve_cwd(req.cwd)
        run_id = new_id("run")
        title = (req.title or "").strip() or goal.splitlines()[0][:80]
        repo = await asyncio.to_thread(repository_root, cwd)
        workdir, tree_root, branch, base = cwd, repo or cwd, None, None
        if req.isolation == "worktree":
            if repo is None:
                raise HTTPException(status_code=400, detail="worktree isolation needs a Git repository with a commit")
            branch = f"hyadex/{run_id}"
            path = self.run_dir(run_id) / "worktree"
            try:
                base = await asyncio.to_thread(create_worktree, repo, path, branch)
            except GitError as exc:
                raise HTTPException(status_code=400, detail=f"could not create the worktree: {exc}") from exc
            # Keep the chosen subdirectory (monorepos): agents work there.
            relative = os.path.relpath(os.path.realpath(cwd), os.path.realpath(repo))
            workdir = str(path if relative == "." else path / relative)
            tree_root = str(path)
        participants = [self._participant("planner", req.planner), self._participant("executor", req.executor)]
        if req.reviewer == "fresh":
            participants.append(self._participant("reviewer", req.reviewer_agent or AgentSpec(backend="claude")))
        try:
            group = await self.groups.create(group_chat.CreateGroupRequest(
                title=f"Autopilot: {title}"[:200], cwd=workdir, participants=participants,
                settings=group_chat.GroupSettings(max_agent_hops=0, delta_char_limit=AUTOPILOT_DELTA_LIMIT),
            ), kind="autopilot", meta={"autopilot_run_id": run_id})
        except BaseException:
            if branch and repo:
                await asyncio.to_thread(remove_worktree, repo, Path(tree_root))
                with suppress(GitError):
                    await asyncio.to_thread(git, repo, "branch", "-D", branch)
            raise
        run: dict[str, Any] = {
            "id": run_id, "title": title, "goal": goal, "constraints": (req.constraints or "").strip(),
            "status": "planning", "approval_kind": None, "approval": req.approval, "isolation": req.isolation,
            "reviewer": req.reviewer, "cwd": cwd, "repo": repo, "workdir": workdir, "tree_root": tree_root,
            "branch": branch, "base_commit": base, "group_id": group["id"], "verify_command": verify_command,
            "verify_sandbox": req.verify_sandbox, "verify_runner": runner,
            "verify_timeout_seconds": req.verify_timeout_seconds, "budgets": req.budgets.model_dump(),
            "plan": None, "task_index": None, "replans": 0, "replan_reason": None, "commits": [], "step": None,
            "last_step": None, "reviews": [], "phase": None, "attention": None, "error": None,
            "briefed": {"executor": False, "reviewer": False},
            "created_at": now(), "updated_at": now(), "finished_at": None,
        }
        self.runs[run_id] = run
        self.by_group[group["id"]] = run_id
        async with self.lock(run_id):
            await self.save(run)
            if branch:
                await self._note(run, f"Working on branch {branch} in a separate worktree (base {base[:12]}). "
                                      "Uncommitted changes in your working directory are not included.")
            await self._guarded(run, self._start_planning(run), "Planning")
            return self.public(run)

    # -- group plumbing -------------------------------------------------------------

    async def _note(self, run: dict[str, Any], text: str) -> None:
        """Post a progress note for the user; agents never receive notes."""

        group_id = run["group_id"]
        async with self.groups.lock(group_id):
            group = self.groups.groups.get(group_id)
            if group is not None:
                await self.groups._append_message(group, author="autopilot", kind="system", text=text,
                                                  round_id=None, mentions=[],
                                                  extra={"audience": NOTE_AUDIENCE, "autopilot_run_id": run["id"]})
                await self.groups.save(group)

    async def _ask(self, run: dict[str, Any], handle: str, text: str, phase: dict[str, Any]) -> None:
        ask_id = new_id("ask")
        # Write ahead: after a crash, recovery finds this request by its id.
        run["phase"] = {**phase, "handle": handle, "ask_id": ask_id, "round_id": None}
        await self.save(run)
        # Every request is self-contained; the member skips other members' replies.
        await self.groups.skip_backlog(run["group_id"], handle)
        result = await self.groups.post_message(
            run["group_id"], PostGroupMessageRequest(text=text, mentions=[handle]), author="autopilot",
            origin="autopilot", kind="orchestrator", audience=[handle],
            meta={"autopilot_run_id": run["id"], "autopilot_ask_id": ask_id})
        run["phase"]["round_id"] = result["round_id"]
        await self.save(run)

    async def _guarded(self, run: dict[str, Any], transition: Any, what: str) -> None:
        """Run a transition; any failure leaves the run in needs_attention, never half-moved."""

        try:
            await transition
        except Exception as exc:
            logger.exception("autopilot transition failed run=%s step=%s", run.get("id"), what)
            detail = getattr(exc, "detail", None) or str(exc) or type(exc).__name__
            await self._needs_attention(run, "error", f"{what} failed: {detail}")

    def _on_group_event(self, event: dict[str, Any]) -> None:
        if event.get("type") != "round_finished":
            return
        run_id = self.by_group.get(str(event.get("group_id") or ""))
        if run_id is None:
            return
        with suppress(RuntimeError):
            self._spawn(self._handle_round(run_id, str(event.get("round_id") or "")))

    def _reply(self, run: dict[str, Any], round_id: str, handle: str) -> str | None:
        replies = [item for item in self.groups.read_events(run["group_id"], types={"message"})
                   if item.get("round_id") == round_id and item.get("author") == handle
                   and item.get("author_kind") == "agent" and not item.get("stopped")]
        return str(replies[-1].get("text") or "") if replies else None

    async def _handle_round(self, run_id: str, round_id: str) -> None:
        async with self.lock(run_id):
            run = self.runs.get(run_id)
            if run is None or run.get("status") in FINAL_STATUSES:
                return
            phase = run.get("phase") or {}
            if not round_id or phase.get("round_id") != round_id:
                return
            if run.get("status") == "paused":
                run["pending_round_id"] = round_id
                await self.save(run)
                return
            phase["round_id"] = None
            run["phase"] = phase
            await self._guarded(run, self._dispatch_reply(run, phase, round_id), "Handling a reply")

    async def _dispatch_reply(self, run: dict[str, Any], phase: dict[str, Any], round_id: str) -> None:
        reply = await asyncio.to_thread(self._reply, run, round_id, phase.get("handle") or "")
        if reply is None:
            await self._needs_attention(run, "no_reply", f"@{phase.get('handle')} did not reply (stopped or failed).")
            return
        name = phase.get("name")
        if name in {"plan", "replan"}:
            await self._on_plan_reply(run, reply, phase)
        elif name == "execute":
            await self._on_execute_reply(run, reply)
        elif name == "review":
            await self._on_review_reply(run, reply, phase)

    # -- planning ------------------------------------------------------------------------

    def _plan_prompt(self, run: dict[str, Any], *, feedback: str | None, replan: str | None,
                     error: str | None) -> str:
        budgets = run["budgets"]
        schema = ('```json\n{"summary": "one paragraph", "tasks": [{"title": "...", "detail": "what to change and how", '
                  '"files": ["path"], "acceptance": ["observable criterion"]}]}\n```')
        if error:
            return (f"@planner Your reply did not contain a valid plan ({error}). Reply again with only the JSON "
                    f"block:\n{schema}")
        reviewed = run["reviewer"] != "none"
        lines = ["@planner You are the planner. Another agent (@executor) implements each task you plan, one at a "
                 "time" + (", and a reviewer checks every step." if reviewed else ".")]
        tasks = (run.get("plan") or {}).get("tasks") or []
        done = [task["title"] for task in tasks if task.get("status") == "done"]
        if replan:
            lines.append("Re-plan the remaining work." + (f" Already done and committed: {'; '.join(done)}." if done else ""))
            lines.append(f"Reason:\n{replan}")
        else:
            lines.append(f"Goal:\n{run['goal']}")
            if run.get("constraints"):
                lines.append(f"Constraints:\n{run['constraints']}")
            if done:
                lines.append(f"Already done and committed: {'; '.join(done)}. Plan only the remaining work.")
        if feedback:
            lines.append(f"The user's feedback on your previous plan:\n{feedback}")
        lines.append(f"Working directory: {run['workdir']} (read it as needed; do not edit files or write code).")
        if run.get("verify_command"):
            lines.append(f"After every task the server runs: `{run['verify_command']}`")
        lines.append(f"Reply with a short rationale and ONE JSON block with 1-{budgets['max_tasks']} small, ordered, "
                     f"independently reviewable tasks:\n{schema}")
        return "\n\n".join(lines)

    async def _start_planning(self, run: dict[str, Any], *, feedback: str | None = None,
                              replan: str | None = None, error: str | None = None, retry: bool = False) -> None:
        run["status"] = "planning"
        run["approval_kind"] = None
        previous = (run.get("phase") or {}).get("name")
        name = "replan" if replan or (error and previous == "replan") else "plan"
        if replan:
            run["replan_reason"] = replan
            run["task_index"] = None
        await self._ask(run, "planner", self._plan_prompt(run, feedback=feedback, replan=replan, error=error),
                        {"name": name, "retry": retry})

    async def _on_plan_reply(self, run: dict[str, Any], reply: str, phase: dict[str, Any]) -> None:
        plan, error = await asyncio.to_thread(parse_plan, reply, int(run["budgets"]["max_tasks"]))
        if plan is None:
            if not phase.get("retry"):
                await self._start_planning(run, error=error, retry=True)
                return
            await self._needs_attention(run, "invalid_plan", f"The planner did not produce a valid plan: {error}.")
            return
        # Finished (committed) tasks always stay; a new plan covers the rest.
        kept = [task for task in ((run.get("plan") or {}).get("tasks") or []) if task.get("status") == "done"]
        tasks = [{**task, "status": "pending", "attempts": 0, "commit": None} for task in plan["tasks"]]
        run["plan"] = {"summary": plan["summary"] or (run.get("plan") or {}).get("summary", ""), "tasks": kept + tasks}
        run["task_index"] = None
        run["replan_reason"] = None
        run["phase"] = {"name": "planned"}  # planning is complete; a retry must not re-plan
        await self._write_file(run, "plan.json", json.dumps(run["plan"], indent=1))
        listing = "\n".join(f"{number}. {task['title']}" + (" (done)" if task.get("status") == "done" else "")
                            for number, task in enumerate(run["plan"]["tasks"], 1))
        await self._note(run, f"Plan ({len(run['plan']['tasks'])} tasks):\n{listing}")
        if run["approval"] in {"plan", "step"}:
            await self._await_approval(run, "plan")
        else:
            await self._start_next_task(run)

    async def _await_approval(self, run: dict[str, Any], kind: str) -> None:
        run["status"] = "awaiting_approval"
        run["approval_kind"] = kind
        await self.save(run)
        await self._note(run, "Waiting for your approval of the plan." if kind == "plan"
                         else "Step approved by review. Waiting for your approval to continue.")

    # -- execution -------------------------------------------------------------------------

    async def _start_next_task(self, run: dict[str, Any]) -> None:
        tasks = run["plan"]["tasks"]
        index = next((number for number, task in enumerate(tasks) if task.get("status") != "done"), None)
        if index is None:
            await self._finish(run)
            return
        await self._start_task(run, index)

    def _context_lines(self, run: dict[str, Any], role: str) -> list[str]:
        """The goal and constraints, once per role."""

        briefed = run.setdefault("briefed", {})
        if briefed.get(role):
            return []
        lines = [f"Overall goal (context):\n{run['goal'][:GOAL_CONTEXT_CHARS]}"]
        if run.get("constraints"):
            lines.append(f"Constraints:\n{run['constraints']}")
        return lines

    async def _start_task(self, run: dict[str, Any], index: int, *, revision: str | None = None) -> None:
        tasks = run["plan"]["tasks"]
        task = tasks[index]
        previous = run.get("step") or {}
        if previous.get("before_tree") and not previous.get("approved"):
            # Unapproved work (a revision, or changes left in place) is
            # reviewed against the last approved state.
            before = previous["before_tree"]
        else:
            before = await asyncio.to_thread(snapshot_tree, run["workdir"])
        if before is None and run["isolation"] == "worktree":
            raise GitError("could not snapshot the worktree")
        run["task_index"] = index
        run["status"] = "executing"
        run["approval_kind"] = None
        task["status"] = "in_progress"
        run["step"] = {"task_index": index, "attempt": int(task.get("attempts") or 0) + 1, "before_tree": before,
                       "approved": False}
        lines = [f"@executor Task {index + 1}/{len(tasks)}: {task['title']}", *self._context_lines(run, "executor")]
        if task.get("detail"):
            lines.append(task["detail"])
        if task.get("files"):
            lines.append("Files: " + ", ".join(task["files"]))
        if task.get("acceptance"):
            lines.append("Acceptance criteria:\n" + "\n".join(f"- {item}" for item in task["acceptance"]))
        if revision:
            lines.append(f"Revision requested:\n{revision}")
        lines.append(f"Implement only this task in {run['workdir']}. Run relevant checks if you can. Do not commit "
                     'or change Git history. End with ONE JSON block: ```json\n{"status": "done" | "blocked", '
                     '"summary": "...", "notes": "anything the reviewer should know"}\n```')
        await self._ask(run, "executor", "\n\n".join(lines), {"name": "execute", "task_index": index})
        run["briefed"]["executor"] = True
        await self.save(run)

    async def _on_execute_reply(self, run: dict[str, Any], reply: str) -> None:
        report = await asyncio.to_thread(parse_step_report, reply)
        run.setdefault("step", {})["report"] = report
        if report["status"] == "blocked":
            await self._needs_attention(run, "blocked", f"The executor is blocked: {report['notes'] or report['summary']}")
            return
        await self._start_verification(run)

    async def _start_verification(self, run: dict[str, Any]) -> None:
        token = new_id("verify")
        run["status"] = "verifying"
        run.setdefault("step", {})["verify_token"] = token
        run.pop("verified_while_paused", None)
        await self.save(run)
        self._spawn(self._verify(run["id"], token))

    def _register_process(self, run_id: str, token: str, process: Any) -> None:
        entry = self._verifying.get(run_id)
        if entry is not None and entry.get("token") == token:
            entry["process"] = process

    def _kill_verification(self, run_id: str) -> None:
        entry = self._verifying.pop(run_id, None)
        process = (entry or {}).get("process")
        if process is not None and process.returncode is None:
            _kill_group(process.pid)

    async def _verify(self, run_id: str, token: str) -> None:
        async with self.lock(run_id):
            run = self.runs.get(run_id)
            if run is None or (run.get("step") or {}).get("verify_token") != token:
                return
            if run.get("status") not in {"verifying", "paused"}:
                return
            step = dict(run["step"])
            workdir, command = run["workdir"], run.get("verify_command")
            timeout, sandbox = int(run["verify_timeout_seconds"]), run.get("verify_sandbox") or "codex"
            runner, tree_root = run.get("verify_runner"), run.get("tree_root") or workdir
            self._verifying[run_id] = {"token": token, "process": None}
        try:
            # Verification runs first, then the snapshot: the reviewer sees, and
            # the checkpoint commits, the tree including anything it changed.
            verification = None
            if command:
                try:
                    argv = verification_argv(command, sandbox=sandbox, runner=runner, writable_root=tree_root)
                    verification = await run_verification(
                        argv, workdir, timeout, register=lambda process: self._register_process(run_id, token, process))
                except GitError as exc:
                    verification = {"command": command, "passed": False, "exit_code": None, "timed_out": False,
                                    "output_tail": str(exc), "seconds": 0.0}
            after = await asyncio.to_thread(snapshot_tree, workdir)
            diff = await asyncio.to_thread(tree_diff, workdir, step.get("before_tree"), after)
        except Exception as exc:
            async with self.lock(run_id):
                run = self.runs.get(run_id)
                if run is not None and (run.get("step") or {}).get("verify_token") == token:
                    await self._needs_attention(run, "error", f"Verification failed to run: {exc}")
            return
        finally:
            entry = self._verifying.get(run_id)
            if entry is not None and entry.get("token") == token:
                self._verifying.pop(run_id, None)
        async with self.lock(run_id):
            run = self.runs.get(run_id)
            if run is None or run.get("status") in FINAL_STATUSES:
                return
            if (run.get("step") or {}).get("verify_token") != token:
                return  # superseded by a newer verification
            step = run["step"]
            name = f"task-{int(step.get('task_index') or 0) + 1}-attempt-{step.get('attempt', 1)}.diff"
            step.update({"after_tree": after, "files": diff["files"], "file_count": diff["file_count"],
                         "stat": diff["stat"], "diff_available": diff["available"], "verification": verification,
                         "diff_path": await self._write_file(run, name, diff["diff"])})
            run["last_step"] = {key: step.get(key) for key in ("task_index", "attempt", "files", "file_count", "stat",
                                                               "diff_path", "verification", "report")}
            if run.get("status") == "paused":
                run["verified_while_paused"] = True
                await self.save(run)
                return
            await self._guarded(run, self._after_verification(run), "Reviewing")

    async def _after_verification(self, run: dict[str, Any]) -> None:
        step = run["step"]
        verification = step.get("verification")
        task = self.current_task(run) or {}
        check = ("" if verification is None else
                 f" Verification {'passed' if verification['passed'] else 'FAILED'}"
                 f" ({'timed out' if verification['timed_out'] else 'exit ' + str(verification['exit_code'])}).")
        await self._note(run, f"Task {int(step.get('task_index') or 0) + 1} \"{task.get('title')}\" attempt "
                              f"{step.get('attempt')}: {step.get('file_count', 0)} file(s) changed.{check}")
        if run["isolation"] == "worktree" and not step.get("after_tree"):
            await self._needs_attention(run, "error", "Could not snapshot the worktree after the step.")
            return
        handle = self.reviewer_handle(run)
        if handle is None:
            passed = verification is None or verification["passed"]
            await self._apply_verdict(run, {
                "verdict": "approve" if passed else "revise",
                "notes": "" if passed else "The verification command failed.",
                "instructions": "" if passed else "The server verification failed. Fix it:\n"
                + verification["output_tail"][-2_000:]})
            return
        await self._ask_review(run, handle)

    async def _ask_review(self, run: dict[str, Any], handle: str, *, error: str | None = None) -> None:
        run["status"] = "reviewing"
        step = run["step"]
        diff_text = ""
        if step.get("diff_available") and step.get("diff_path"):
            def read() -> str:
                with suppress(OSError):
                    with open(step["diff_path"], encoding="utf-8", errors="replace") as source:
                        return source.read(INLINE_DIFF_CHARS + 1)
                return ""
            diff_text = await asyncio.to_thread(read)
        prompt = self._review_prompt(run, self.current_task(run) or {}, step, diff_text, handle, error=error)
        await self._ask(run, handle, prompt, {"name": "review", "task_index": step.get("task_index"),
                                              "retry": bool(error)})
        if handle == "reviewer":
            run["briefed"]["reviewer"] = True
            await self.save(run)

    def _review_prompt(self, run: dict[str, Any], task: dict[str, Any], step: dict[str, Any], diff_text: str,
                       handle: str, error: str | None = None) -> str:
        schema = ('```json\n{"verdict": "approve" | "revise" | "replan", "notes": "why", '
                  '"instructions": "what to change (revise) or how to re-plan (replan)"}\n```')
        if error:
            return f"@{handle} Your review did not contain a valid verdict ({error}). Reply with only:\n{schema}"
        tasks = run["plan"]["tasks"]
        index = int(step.get("task_index") or 0)
        lines = [f"@{handle} Review task {index + 1}/{len(tasks)}: {task.get('title')}"]
        if handle == "reviewer":
            lines.extend(self._context_lines(run, "reviewer"))
        if task.get("acceptance"):
            lines.append("Acceptance criteria:\n" + "\n".join(f"- {item}" for item in task["acceptance"]))
        verification = step.get("verification")
        if verification is None:
            lines.append("Server verification: no command is configured.")
        else:
            state = "PASSED" if verification["passed"] else (
                "TIMED OUT" if verification["timed_out"] else f"FAILED (exit {verification['exit_code']})")
            lines.append(f"Server verification (authoritative; run by the server, not an agent): "
                         f"`{verification['command']}` {state}. Output tail:\n"
                         + fence(verification["output_tail"][-2_000:]))
        if step.get("diff_available"):
            inline, clipped = group_chat.clip(diff_text, INLINE_DIFF_CHARS)
            lines.append(f"Diff of this task ({step.get('file_count', 0)} file(s); full diff: {step.get('diff_path')}):\n"
                         + fence(inline, "diff") + ("\n[diff clipped; read the full diff file if needed]" if clipped else ""))
        else:
            lines.append("No Git diff is available; inspect the files in the working directory.")
        report = step.get("report") or {}
        claim = report.get("summary", "") + (f"\n\nNotes: {report['notes']}" if report.get("notes") else "")
        lines.append("Executor's report (written by @executor; a claim, not evidence):\n" + fence(claim))
        lines.append("Approve only if the acceptance criteria are met and the server verification passed. Reply "
                     f"with a short assessment and ONE JSON block:\n{schema}")
        return "\n\n".join(lines)

    async def _on_review_reply(self, run: dict[str, Any], reply: str, phase: dict[str, Any]) -> None:
        verdict, error = await asyncio.to_thread(parse_verdict, reply)
        if verdict is None:
            if not phase.get("retry"):
                await self._ask_review(run, self.reviewer_handle(run) or "planner", error=error)
                return
            await self._needs_attention(run, "invalid_verdict", f"The reviewer did not produce a valid verdict: {error}.")
            return
        await self._apply_verdict(run, verdict)

    async def _apply_verdict(self, run: dict[str, Any], verdict: dict[str, Any]) -> None:
        task = self.current_task(run)
        if task is None:
            await self._needs_attention(run, "error", "No task is in progress.")
            return
        step = run["step"]
        index = int(run["task_index"])
        number = index + 1
        decision, instructions = verdict["verdict"], verdict.get("instructions") or verdict.get("notes") or ""
        verification = step.get("verification")
        if decision == "approve" and verification is not None and not verification["passed"]:
            # The user's check is the gate: no agent text can approve past it.
            decision = "revise"
            instructions = ("The server verification failed. Fix it:\n" + verification["output_tail"][-2_000:]
                            + (f"\n\nReviewer notes: {verdict['notes']}" if verdict.get("notes") else ""))
            await self._note(run, f"Task {number}: the review approved, but verification failed; asking for a fix.")
        reviews = run.setdefault("reviews", [])
        reviews.append({"task_index": index, "attempt": step.get("attempt"), "verdict": decision,
                        "notes": verdict.get("notes", ""), "at": now()})
        del reviews[:-MAX_RECORDED_REVIEWS]
        if decision == "approve":
            commit = None
            if run["isolation"] == "worktree":
                try:
                    commit = await asyncio.to_thread(commit_tree, run["tree_root"], run["branch"], step["after_tree"],
                                                     f"Autopilot task {number}: {task['title']}")
                except GitError as exc:
                    await self._needs_attention(run, "error", f"Could not commit the checkpoint: {exc}")
                    return
            step["approved"] = True
            task["status"] = "done"
            task["commit"] = commit
            if commit:
                run["commits"].append({"task_index": index, "commit": commit, "title": task["title"]})
            await self._note(run, f"Task {number} approved" + (f" and committed as {commit[:12]}." if commit else ".")
                             + (f" Review: {verdict['notes']}" if verdict.get("notes") else ""))
            remaining = any(item.get("status") != "done" for item in run["plan"]["tasks"])
            if run["approval"] == "step" and remaining:
                await self._await_approval(run, "step")
            else:
                await self._start_next_task(run)
            return
        if decision == "revise":
            task["attempts"] = int(task.get("attempts") or 0) + 1
            if task["attempts"] > int(run["budgets"]["max_revisions"]):
                await self._needs_attention(run, "revision_budget",
                                            f"Task {number} still fails review after {task['attempts']} attempt(s): "
                                            f"{verdict.get('notes') or instructions}", instructions=instructions)
                return
            await self._note(run, f"Task {number} needs changes: {verdict.get('notes') or instructions}")
            await self._start_task(run, index, revision=instructions)
            return
        reason = instructions or "Re-plan the remaining work."
        run["replans"] = int(run.get("replans") or 0) + 1
        if run["replans"] > int(run["budgets"]["max_replans"]):
            run["replan_reason"] = reason
            await self._needs_attention(run, "replan_budget",
                                        f"The reviewer asked to re-plan again (limit {run['budgets']['max_replans']}).")
            return
        await self._abandon_step(run)
        await self._note(run, f"Re-planning after task {number}: {verdict.get('notes') or reason}")
        await self._start_planning(run, replan=reason)

    async def _abandon_step(self, run: dict[str, Any]) -> None:
        """Drop a rejected approach before re-planning."""

        if run["isolation"] == "worktree":
            # The rejected edits must not slip into a later checkpoint.
            await asyncio.to_thread(discard_uncommitted, run["tree_root"])
            run["step"] = None
        # In place, the edits stay and the next task's diff includes them.
        task = self.current_task(run)
        if task is not None and task.get("status") != "done":
            task["status"] = "pending"

    async def _finish(self, run: dict[str, Any]) -> None:
        run["status"] = "done"
        run["finished_at"] = now()
        run["phase"] = None
        await self.save(run)
        count = len(run["plan"]["tasks"])
        if run.get("branch"):
            merge = (f"Branch {run['branch']} has {len(run['commits'])} checkpoint commit(s) on top of "
                     f"{(run.get('base_commit') or '')[:12]}. Review and merge it with: git merge {run['branch']}")
        else:
            merge = "Changes were made directly in the working directory."
        await self._note(run, f"Autopilot finished: all {count} task(s) approved. {merge}")

    async def _needs_attention(self, run: dict[str, Any], kind: str, reason: str, **details: Any) -> None:
        if run.get("status") in FINAL_STATUSES or self.runs.get(run.get("id")) is not run:
            return  # an aborted or deleted run stays that way
        run["status"] = "needs_attention"
        run["attention"] = {"kind": kind, "reason": reason, "at": now(), **details}
        run["error"] = reason
        await self.save(run)
        await self._note(run, f"Needs your attention: {reason}")

    # -- user controls ---------------------------------------------------------------------

    async def approve(self, run_id: str, req: ApproveRequest) -> dict[str, Any]:
        async with self.lock(run_id):
            run = self.require(run_id)
            if run.get("status") != "awaiting_approval":
                raise HTTPException(status_code=409, detail="this run is not waiting for approval")
            if req.tasks is not None:
                if run.get("approval_kind") != "plan":
                    raise HTTPException(status_code=409, detail="tasks can only be edited when approving the plan")
                if len(req.tasks) > int(run["budgets"]["max_tasks"]):
                    raise HTTPException(status_code=400, detail="too many tasks")
                kept = [task for task in run["plan"]["tasks"] if task.get("status") == "done"]
                run["plan"]["tasks"] = kept + [{**task.model_dump(), "status": "pending", "attempts": 0, "commit": None}
                                               for task in req.tasks]
                await self._note(run, "You edited the plan before approving it.")
            await self._guarded(run, self._start_next_task(run), "Starting the next task")
            return self.public(run)

    async def feedback(self, run_id: str, req: FeedbackRequest) -> dict[str, Any]:
        async with self.lock(run_id):
            run = self.require(run_id)
            text = req.text.strip()
            status, kind = run.get("status"), run.get("approval_kind")
            attention_kind = (run.get("attention") or {}).get("kind")
            phase_name = (run.get("phase") or {}).get("name")
            if status == "awaiting_approval" and kind == "plan":
                await self._note(run, f"Your feedback on the plan: {text}")
                await self._guarded(run, self._start_planning(run, feedback=text), "Re-planning")
            elif (status == "awaiting_approval" and kind == "step") or (
                    status == "needs_attention" and self.current_task(run) is not None
                    and phase_name in {"execute", "review"} and attention_kind != "replan_budget"):
                index = int(run["task_index"])
                task = run["plan"]["tasks"][index]
                if task.get("status") == "done":
                    task["commit"] = None  # a follow-up commit will carry the change
                task["attempts"] = 0
                self._clear_attention(run)
                await self._note(run, f"Your feedback on task {index + 1}: {text}")
                await self._guarded(run, self._start_task(run, index, revision=f"The user asks for: {text}"),
                                    "Revising the task")
            elif status == "needs_attention":
                self._clear_attention(run)
                await self._note(run, f"Your feedback: {text}")
                await self._guarded(run, self._start_planning(run, replan=f"The user asks for: {text}")
                                    if (run.get("plan") or {}).get("tasks") else self._start_planning(run, feedback=text),
                                    "Re-planning")
            else:
                raise HTTPException(status_code=409, detail="feedback is accepted while waiting for approval or attention")
            return self.public(run)

    @staticmethod
    def _clear_attention(run: dict[str, Any]) -> None:
        run["attention"] = None
        run["error"] = None

    async def pause(self, run_id: str) -> dict[str, Any]:
        async with self.lock(run_id):
            run = self.require(run_id)
            if run.get("status") not in ACTIVE_STATUSES:
                raise HTTPException(status_code=409, detail="only an active run can be paused")
            run["paused_from"] = run["status"]
            run["status"] = "paused"
            await self.save(run)
            await self._note(run, "Paused. The current step finishes, then the run waits.")
            return self.public(run)

    async def resume(self, run_id: str) -> dict[str, Any]:
        async with self.lock(run_id):
            run = self.require(run_id)
            status = run.get("status")
            if status == "paused":
                previous = run.pop("paused_from", None) or "executing"
                pending = run.pop("pending_round_id", None)
                if previous == "verifying":
                    if run.pop("verified_while_paused", False):
                        await self._guarded(run, self._after_verification(run), "Reviewing")
                    elif run_id in self._verifying:
                        run["status"] = "verifying"  # the in-flight verification continues
                        await self.save(run)
                    else:
                        await self._guarded(run, self._start_verification(run), "Verifying")
                else:
                    run["status"] = previous
                    await self.save(run)
                    if pending:
                        run["phase"] = {**(run.get("phase") or {}), "round_id": pending}
                        self._spawn(self._handle_round(run_id, pending))
            elif status == "needs_attention":
                attention = run.get("attention") or {}
                self._clear_attention(run)
                await self._guarded(run, self._redrive(run, attention), "Resuming")
            else:
                raise HTTPException(status_code=409, detail="only a paused run or one that needs attention can resume")
            return self.public(run)

    async def _redrive(self, run: dict[str, Any], attention: dict[str, Any]) -> None:
        """Continue after needs_attention, according to why the run stopped."""

        kind = attention.get("kind")
        name = (run.get("phase") or {}).get("name")
        task = self.current_task(run)
        unfinished = task is not None and task.get("status") != "done"
        step = run.get("step") or {}
        if kind == "replan_budget":
            run["replans"] = max(0, int(run.get("replans") or 0) - 1)  # allow one more re-plan
            await self._abandon_step(run)
            await self._start_planning(run, replan=run.get("replan_reason") or "Re-plan the remaining work.")
        elif kind == "revision_budget" and unfinished:
            task["attempts"] = 0
            await self._start_task(run, int(run["task_index"]), revision=attention.get("instructions") or None)
        elif not run.get("plan") or name in {"plan", "replan"}:
            if name == "replan":
                await self._start_planning(run, replan=run.get("replan_reason"))
            else:
                await self._start_planning(run)
        elif name == "review" and unfinished and step.get("after_tree") is not None:
            await self._ask_review(run, self.reviewer_handle(run) or "planner")
        elif name == "review" and unfinished:
            await self._start_verification(run)
        elif unfinished:
            await self._start_task(run, int(run["task_index"]))
        else:
            await self._start_next_task(run)

    async def abort(self, run_id: str) -> dict[str, Any]:
        async with self.lock(run_id):
            run = self.require(run_id)
            if run.get("status") in FINAL_STATUSES:
                return self.public(run)
            run["status"] = "aborted"
            run["finished_at"] = now()
            run["phase"] = None
            self._kill_verification(run_id)
            await self.save(run)
        with suppress(HTTPException):
            await self.groups.stop(run["group_id"], reason="autopilot aborted")
        async with self.lock(run_id):
            await self._note(run, "Aborted." + (f" The worktree and branch {run['branch']} are kept." if run.get("branch") else ""))
            return self.public(run)

    async def delete(self, run_id: str, *, remove_worktree_files: bool) -> None:
        run = self.require(run_id)
        if run.get("status") not in FINAL_STATUSES:
            await self.abort(run_id)
        async with self.lock(run_id):
            self._kill_verification(run_id)
            self.by_group.pop(run.get("group_id") or "", None)
            self.runs.pop(run_id, None)
        with suppress(HTTPException):
            await self.groups.delete(run["group_id"])
        if remove_worktree_files and run.get("isolation") == "worktree" and run.get("repo"):
            await asyncio.to_thread(remove_worktree, run["repo"], Path(run.get("tree_root") or run["workdir"]))
        await asyncio.to_thread(shutil.rmtree, self.run_dir(run_id), True)

    def list(self) -> list[dict[str, Any]]:
        runs = sorted(self.runs.values(), key=lambda item: item.get("updated_at") or 0, reverse=True)
        return [self.public(run, summary=True) for run in runs]
