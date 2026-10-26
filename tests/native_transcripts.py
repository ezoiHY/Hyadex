"""Synthetic Claude Code and Codex transcripts for history tests."""
import json
from pathlib import Path


def write_claude_transcript(
    path: Path, *, cwd: str | None, first_user_text: str,
    metadata: dict[str, object] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if cwd is not None:
        lines.append(json.dumps({"type": "user", "cwd": cwd, "message": {"role": "user", "content": first_user_text}, **(metadata or {})}))
    else:
        lines.append(json.dumps({"type": "user", "message": {"role": "user", "content": first_user_text}, **(metadata or {})}))
    path.write_text("\n".join(lines) + "\n")


def write_codex_transcript(
    path: Path, *, session_id: str, cwd: str, first_user_text: str | None,
    metadata: dict[str, object] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({
        "timestamp": "2026-08-23T00:00:00.000Z",
        "type": "session_meta",
        "payload": {"id": session_id, "session_id": session_id, "cwd": cwd, **(metadata or {})}
    })]
    if first_user_text is not None:
        lines.append(json.dumps({
            "timestamp": "2026-08-23T00:00:01.000Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": first_user_text}
        }))
    path.write_text("\n".join(lines) + "\n")
