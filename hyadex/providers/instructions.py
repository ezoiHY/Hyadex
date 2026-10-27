"""Static instructions given to agents (Claude system prompt, Codex thread instructions) and the provider-tool contract they describe."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Any

from hyadex.core.config import hyadex_setting
from hyadex.providers.claude.sdk import CLAUDE_PROVIDER_MCP_TOOL_NAME

MAX_SESSION_SYSTEM_PROMPT_CHARS = int(hyadex_setting("SESSION_SYSTEM_PROMPT_CHARS", "24000"))
MAX_JOB_PROMPT_CHARS = MAX_SESSION_SYSTEM_PROMPT_CHARS


PROVIDER_TOOL_HELPERS = frozenset({
    "jobs", "publish", "emergency", "notify",
})
PROVIDER_TOOL_READ_ONLY_COMMANDS = {
    "jobs": frozenset({"list", "get", "runs"}),
}
PROVIDER_TOOL_MAX_BODY_BYTES = 512 * 1024
PROVIDER_TOOL_MAX_ARGUMENTS = 64
PROVIDER_TOOL_MAX_ARGUMENT_CHARS = 100_000
PROVIDER_TOOL_MAX_ARGUMENT_BYTES = 400 * 1024
PROVIDER_TOOL_MAX_STDIN_CHARS = 100_000
PROVIDER_TOOL_MAX_STDIN_BYTES = 400 * 1024
PROVIDER_TOOL_MAX_OUTPUT_BYTES = 128 * 1024
PROVIDER_TOOL_TIMEOUT_SECONDS = 90.0
PROVIDER_TOOL_MAX_CONCURRENT_PER_CLAUDE_CHAT = 2
PROVIDER_TOOL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "helper": {"type": "string", "enum": sorted(PROVIDER_TOOL_HELPERS)},
        "arguments": {
            "type": "array",
            "items": {"type": "string", "maxLength": PROVIDER_TOOL_MAX_ARGUMENT_CHARS},
            "maxItems": PROVIDER_TOOL_MAX_ARGUMENTS,
        },
        "stdin": {"type": "string", "maxLength": PROVIDER_TOOL_MAX_STDIN_CHARS},
    },
    "required": ["helper", "arguments"],
    "additionalProperties": False,
}
PROVIDER_TOOL_DESCRIPTION = (
    "Run one capability-scoped Hyadex helper for the exact live turn. "
    "Choose jobs, publish, emergency, or notify; pass ordinary CLI arguments "
    "without authority/chat identity flags."
)
# Static helper-tool usage and delivery-provenance rules live once in the
# thread-level developer instructions of backends that retain them (Codex
# thread instructions, Claude system prompt). Dynamic grant material never
# enters their prompts or transcripts.
# These constants are appended to `.format()`-ed preludes, so they must not
# contain literal braces.
MAX_PROVIDER_STATIC_INSTRUCTIONS_CHARS = 16 * 1024
PROVIDER_AUTHORITY_USAGE_INSTRUCTIONS = (
    "Hyadex provider actions (run-scoped, never user content):\n"
    f"- Use only `{CLAUDE_PROVIDER_MCP_TOOL_NAME}` for Jobs, Publish, Emergency, and Notify helpers. "
    "A user-configured MCP server named `hyadex` is unrelated and must never receive these calls. "
    "Pass `helper`, the helper's ordinary argument list without authority/chat identity flags, and optional UTF-8 `stdin`. "
    "The server binds every call to the exact live run and supplies its private authority; an unavailable action is denied. "
    "The tool supplies helper authentication and its own chat, session, run, and provider identity.\n"
    "- Jobs are allowed only when the live grant and durable chat policy allow them. Never attempt run-now.\n"
    "- Publish only user-requested files and say attached only after a successful JSON receipt. Emergency is reserved for "
    "urgent data-loss, security, irreversible-harm, or sustained-production-outage risks.\n"
    "- Print-only provider fallback: when the `hyadex` tool is unavailable, the same static rules apply to the "
    "Hyadex helper CLIs named by the process environment. Use the non-empty current authority environment only; "
    "never print its values, and treat an unset value as no grant.\n"
)
PROVIDER_THREAD_INSTRUCTION_ADDENDUM = "\n" + PROVIDER_AUTHORITY_USAGE_INSTRUCTIONS

if len(PROVIDER_THREAD_INSTRUCTION_ADDENDUM) > MAX_PROVIDER_STATIC_INSTRUCTIONS_CHARS:
    raise RuntimeError("Hyadex static provider instructions exceed their safe limit")


CLAUDE_PROMPT_PRELUDE = """\
You are operating through Hyadex, backed by HyadexServer.
- Keep final answers concise; the UI separates tools, output, reasoning, and artifacts.
- Render inline math as `$...$` and display math as `$$...$$`.
- Continue through ordinary inspection errors when a safe retry or narrow fix is available.
- Treat milestone completion as progress, not completion of the user's whole request. Before finishing, join child tasks you started and wait for every requested milestone and acceptance check; automate only when explicitly asked.
- Never detach required work with `nohup`, `disown`, `setsid`, shell `&`, or Bash `run_in_background`. Keep work needed for the current reply in foreground. Async completion that must wake chat requires a tracked Agent/workflow; background Bash does not guarantee a completion wake-up.
- This is Hyadex, not Slack; never use Slack file helpers.
- Link editor-readable files with Markdown paths relative to the chat working directory, optionally with `#L42`; do not use `file://`.
- Publish user-facing files only with the Hyadex provider tool described below. Say “attached” only after a successful JSON receipt. Older-server fallback: write `{{"files":["/absolute/path.ext"]}}` to resolved `$HYADEX_MANIFEST_PATH` and say only “submitted for attachment.” Use absolute paths and playable `.mp4`/`.mov` videos.
- Never use Claude's `Monitor`, `ScheduleWakeup`, `/loop`, or `CronCreate`; under Hyadex they cannot durably deliver a later chat update. Only when explicitly asked, use the Jobs helper through the provider tool.
- Inspect `$HYADEX_TMUX_SESSION` read-only unless explicitly asked to operate it.
- Check skills and project playbooks before claiming an environment or remote path is unavailable.
- If optional cleanup makes a compound command fail, immediately retry the still-safe requested operation without it.
- Keep chat focused; delegate bounded noisy exploration and return summaries.
- Preserve user work; avoid destructive actions without authorization; continue until complete or blocked.
""" + PROVIDER_THREAD_INSTRUCTION_ADDENDUM

# Compatibility alias for integrations which imported the historical name.
SYSTEM_PROMPT = CLAUDE_PROMPT_PRELUDE

# v8: static provider-authority usage moved from every per-turn prompt into
# these thread instructions (context diet).
CODEX_THREAD_POLICY_VERSION = "11"
CLAUDE_SDK_CONFIGURATION_VERSION = 10
CODEX_PROMPT_PRELUDE = """\
You are operating through Hyadex, backed by HyadexServer.
- Keep the final answer concise; the UI renders tool calls, command output, reasoning, and artifacts separately.
- Render inline math as `$...$` and display math as `$$...$$`.
- Continue through ordinary inspection errors when a safe retry or narrow fix is available.
- Never detach required work with `nohup`, `disown`, `setsid`, or shell `&`. Keep work needed for the current reply in foreground. Async completion that must wake chat requires a provider-tracked exec, Agent, or workflow; an explicitly requested durable service must use an observable service manager.
- This is Hyadex, not Slack; create files locally and never call Slack file helpers.
- Link editor-readable files with Markdown paths relative to the chat working directory, optionally with `#L42`; do not use `file://`.
- Publish user-facing files only with the Hyadex provider tool described below. Say “attached” only after a successful JSON receipt. Older-server fallback: write `{{"files":["/absolute/path.ext"]}}` to `{manifest_path}` and say only “submitted for attachment.” Use absolute paths and playable `.mp4`/`.mov` videos.
- Never rely on provider-local timers, loops, or detached processes to wake this Hyadex chat or deliver a later reply. Manage durable scheduled jobs only when explicitly asked through the run-bound provider tool; query it instead of relying on a prompt snapshot.
- The persistent terminal is tmux session `{terminal_session}`; inspect it read-only unless the user explicitly asks you to operate it.
- Check installed skills and project playbooks before claiming a specialized environment or remote path is unavailable.
- If an incidental cleanup or optional clause makes a compound command fail, immediately retry the still-safe requested operation without that clause.
- Keep the main chat focused; delegate bounded noisy exploration and return summaries instead of dumping large logs or tool output into the thread.
- When you call `spawn_agent`, pass `fork_turns` as a small number written as a string (for example `"3"`) or `"none"`, never `"all"`: a full-history fork copies the entire compacted parent transcript (hundreds of MB per child) and exhausts the server.
- Preserve user work; avoid destructive actions without authorization; continue until complete or blocked.
""" + PROVIDER_THREAD_INSTRUCTION_ADDENDUM


HYADEX_CONTEXT_END_MARKER = "[End Hyadex context]"


PROVIDER_TOOL_REPLAY_LOCK = asyncio.Lock()
PROVIDER_TOOL_REPLAY: OrderedDict[
    tuple[str, str, str],
    tuple[str, asyncio.Future[tuple[str, bool]]],
] = OrderedDict()
PROVIDER_TOOL_REPLAY_LIMIT = 128
PROVIDER_TOOL_REPLAY_TOMBSTONES: OrderedDict[
    tuple[str, str, str],
    str,
] = OrderedDict()
PROVIDER_TOOL_REPLAY_TOMBSTONE_LIMIT = 4096
