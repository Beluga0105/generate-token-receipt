#!/usr/bin/env python3
"""Generate an auditable token-usage receipt in selectable layouts.

The generator uses only the Python standard library. It can ingest current Codex
telemetry, an OpenAI API response/usage object, or explicit manual token counts.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import html
import json
import os
import re
import sys
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

MILLION = Decimal("1000000")
UUID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")

BUILTIN_PRICING: dict[str, dict[str, Any]] = {
    "gpt-5.6-luna": {
        "input": Decimal("0.20"),
        "cached_input": Decimal("0.02"),
        "cache_write_input": Decimal("0.25"),
        "output": Decimal("1.20"),
        "as_of": "2026-08-13",
        "source": "https://developers.openai.com/api/docs/models/gpt-5.6-luna",
        "long_context_threshold": 272000,
        "long_input_multiplier": Decimal("2"),
        "long_output_multiplier": Decimal("1.5"),
    },
    "gpt-5.6-terra": {
        "input": Decimal("2.00"),
        "cached_input": Decimal("0.20"),
        "cache_write_input": Decimal("2.50"),
        "output": Decimal("12.00"),
        "as_of": "2026-08-13",
        "source": "https://developers.openai.com/api/docs/models/gpt-5.6-terra",
        "long_context_threshold": 272000,
        "long_input_multiplier": Decimal("2"),
        "long_output_multiplier": Decimal("1.5"),
    },
    "gpt-5.6-sol": {
        "input": Decimal("5.00"),
        "cached_input": Decimal("0.50"),
        "cache_write_input": Decimal("6.25"),
        "output": Decimal("30.00"),
        "as_of": "2026-08-13",
        "source": "https://developers.openai.com/api/docs/models/gpt-5.6-sol",
        "long_context_threshold": 272000,
        "long_input_multiplier": Decimal("2"),
        "long_output_multiplier": Decimal("1.5"),
    }
}


@dataclass
class TokenCall:
    model: str
    input_tokens: int
    cached_input_tokens: int
    cache_write_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    timestamp: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    reasoning_effort: str | None = None
    aggregated: bool = False

    @property
    def fresh_input_tokens(self) -> int:
        return self.input_tokens - self.cached_input_tokens - self.cache_write_input_tokens

    @property
    def visible_output_tokens(self) -> int:
        return self.output_tokens - self.reasoning_output_tokens

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def localize_iso(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is None:
        return value
    return parsed.astimezone().isoformat(timespec="milliseconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def in_time_windows(value: str | None, windows: list[tuple[datetime, datetime]]) -> bool:
    parsed = parse_iso(value)
    return bool(parsed and any(start <= parsed <= end for start, end in windows))


def path_is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.expanduser().resolve().relative_to(root.expanduser().resolve())
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


def as_nonnegative_int(value: Any, field: str, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be a nonnegative integer")
    if value < 0:
        raise ValueError(f"{field} must be a nonnegative integer")
    return value


def nested_int(mapping: dict[str, Any], paths: Iterable[tuple[str, ...]], default: int = 0) -> int:
    for path in paths:
        current: Any = mapping
        for key in path:
            if not isinstance(current, dict) or key not in current:
                break
            current = current[key]
        else:
            if current is not None:
                return as_nonnegative_int(current, ".".join(path))
    return default


def validate_call(call: TokenCall) -> None:
    if call.cached_input_tokens + call.cache_write_input_tokens > call.input_tokens:
        raise ValueError(
            "cached_input_tokens + cache_write_input_tokens cannot exceed input_tokens"
        )
    if call.reasoning_output_tokens > call.output_tokens:
        raise ValueError("reasoning_output_tokens cannot exceed output_tokens")


def session_id_from_path(path: Path) -> str | None:
    match = UUID_RE.search(path.stem)
    return match.group(1) if match else None


def find_rollout(thread_id: str) -> Path:
    codex_root = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
    candidates: list[Path] = []
    sessions = codex_root / "sessions"
    if sessions.is_dir():
        candidates.extend(sessions.glob(f"**/rollout-*{thread_id}.jsonl"))
    archived = codex_root / "archived_sessions"
    if archived.is_dir():
        candidates.extend(archived.glob(f"rollout-*{thread_id}.jsonl"))
    if not candidates:
        raise FileNotFoundError(f"No Codex rollout found for thread {thread_id}")
    return max(candidates, key=lambda item: item.stat().st_mtime)


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL in {path.name} at line {line_number}") from exc
            if isinstance(value, dict):
                yield value


def own_session_meta(path: Path, session_id: str) -> dict[str, Any]:
    for record in iter_jsonl(path):
        if record.get("type") != "session_meta":
            continue
        payload = record.get("payload") or {}
        if payload.get("id") == session_id:
            return {"timestamp": record.get("timestamp"), **payload}
    raise ValueError(f"Session metadata {session_id} not found in {path.name}")


def extract_codex_session(path: Path, session_id: str) -> tuple[list[TokenCall], dict[str, Any]]:
    records = list(iter_jsonl(path))
    model = "unknown"
    effort: str | None = None
    turn_id: str | None = None
    calls: list[TokenCall] = []
    meta: dict[str, Any] = {}
    meta_index: int | None = None
    for index, record in enumerate(records):
        payload = record.get("payload") or {}
        if record.get("type") == "session_meta" and payload.get("id") == session_id:
            meta_index = index
            meta = {"timestamp": record.get("timestamp"), **payload}
            break
    if meta_index is None:
        raise ValueError(f"Session {session_id} has no matching session boundary")
    cwd = str(meta.get("cwd") or "") or None

    boundary = meta_index
    source = meta.get("source")
    is_subagent = isinstance(source, dict) and isinstance(source.get("subagent"), dict)
    if is_subagent:
        marker = next(
            (
                index
                for index, record in enumerate(records[meta_index + 1 :], start=meta_index + 1)
                if record.get("type") == "inter_agent_communication_metadata"
            ),
            None,
        )
        if marker is not None:
            boundary = marker
        else:
            starts = [
                index
                for index, record in enumerate(records[meta_index + 1 :], start=meta_index + 1)
                if record.get("type") == "event_msg"
                and (record.get("payload") or {}).get("type") == "task_started"
            ]
            if starts:
                boundary = starts[1] if len(starts) > 1 else starts[0]

    previous_total: dict[str, int] | None = None
    for record in records[: boundary + 1]:
        payload = record.get("payload") or {}
        if record.get("type") == "turn_context":
            model = str(payload.get("model") or model)
            effort = payload.get("effort") or effort
            turn_id = str(payload.get("turn_id") or turn_id or "") or None
            cwd = str(payload.get("cwd") or cwd or "") or None
        if record.get("type") == "event_msg" and payload.get("type") == "token_count":
            total = ((payload.get("info") or {}).get("total_token_usage") or {})
            if isinstance(total, dict):
                previous_total = {
                    key: as_nonnegative_int(total.get(key), key)
                    for key in (
                        "input_tokens",
                        "cached_input_tokens",
                        "cache_write_input_tokens",
                        "output_tokens",
                        "reasoning_output_tokens",
                    )
                }

    for record in records[boundary + 1 :]:
        record_type = record.get("type")
        payload = record.get("payload") or {}
        if record_type == "turn_context":
            model = str(payload.get("model") or model)
            effort = payload.get("effort") or effort
            turn_id = str(payload.get("turn_id") or turn_id or "") or None
            cwd = str(payload.get("cwd") or cwd or "") or None
            continue
        if record_type != "event_msg" or payload.get("type") != "token_count":
            continue
        info = payload.get("info") or {}
        total = info.get("total_token_usage")
        fallback = info.get("last_token_usage")
        if not isinstance(total, dict) and not isinstance(fallback, dict):
            continue
        current_total = (
            {
                key: as_nonnegative_int(total.get(key), key)
                for key in (
                    "input_tokens",
                    "cached_input_tokens",
                    "cache_write_input_tokens",
                    "output_tokens",
                    "reasoning_output_tokens",
                )
            }
            if isinstance(total, dict)
            else None
        )
        if current_total is not None and previous_total == current_total:
            continue
        usage: dict[str, int]
        if current_total is not None:
            baseline = previous_total or {key: 0 for key in current_total}
            delta = {key: current_total[key] - baseline.get(key, 0) for key in current_total}
            if all(value >= 0 for value in delta.values()):
                usage = delta
            elif isinstance(fallback, dict):
                usage = {
                    key: as_nonnegative_int(fallback.get(key), key)
                    for key in current_total
                }
            else:
                raise ValueError(f"Token counters moved backwards in {path.name}")
            previous_total = current_total
        else:
            usage = {
                key: as_nonnegative_int(fallback.get(key), key)
                for key in (
                    "input_tokens",
                    "cached_input_tokens",
                    "cache_write_input_tokens",
                    "output_tokens",
                    "reasoning_output_tokens",
                )
            }
        call = TokenCall(
            model=model,
            input_tokens=usage["input_tokens"],
            cached_input_tokens=usage["cached_input_tokens"],
            cache_write_input_tokens=usage["cache_write_input_tokens"],
            output_tokens=usage["output_tokens"],
            reasoning_output_tokens=usage["reasoning_output_tokens"],
            timestamp=record.get("timestamp"),
            session_id=session_id,
            turn_id=str(payload.get("turn_id") or turn_id or "") or None,
            reasoning_effort=effort,
        )
        # Used only for local project filtering; dataclass serialization intentionally omits it.
        setattr(call, "_recorded_cwd", cwd)
        validate_call(call)
        calls.append(call)
    return calls, {"model": model, "reasoning_effort": effort, **meta}


def completed_turn_windows(path: Path, session_id: str) -> list[dict[str, str]]:
    records = list(iter_jsonl(path))
    meta_index = next(
        (
            index
            for index, record in enumerate(records)
            if record.get("type") == "session_meta"
            and (record.get("payload") or {}).get("id") == session_id
        ),
        None,
    )
    if meta_index is None:
        raise ValueError(f"Session {session_id} has no matching session boundary")

    order: list[str] = []
    starts: dict[str, str] = {}
    completions: dict[str, str] = {}
    current_turn: str | None = None
    for record in records[meta_index + 1 :]:
        payload = record.get("payload") or {}
        if record.get("type") == "turn_context":
            value = payload.get("turn_id")
            if value:
                current_turn = str(value)
                if current_turn not in starts:
                    starts[current_turn] = str(record.get("timestamp") or "")
                    order.append(current_turn)
            continue
        if record.get("type") != "event_msg" or payload.get("type") != "task_complete":
            continue
        completed_turn = str(payload.get("turn_id") or current_turn or "")
        if completed_turn and completed_turn in starts and record.get("timestamp"):
            completions[completed_turn] = str(record["timestamp"])

    return [
        {"turn_id": turn_id, "started_at": starts[turn_id], "completed_at": completions[turn_id]}
        for turn_id in order
        if turn_id in completions and parse_iso(starts[turn_id]) and parse_iso(completions[turn_id])
    ]


def discover_descendants(root_id: str) -> list[tuple[str, Path, dict[str, Any]]]:
    by_parent: dict[str, list[tuple[str, Path, dict[str, Any]]]] = {}
    for session_id, path in all_session_files():
        if session_id == root_id:
            continue
        try:
            meta = own_session_meta(path, session_id)
        except (OSError, ValueError):
            continue
        source = meta.get("source")
        if not isinstance(source, dict):
            continue
        spawn = ((source.get("subagent") or {}).get("thread_spawn") or {})
        parent_id = spawn.get("parent_thread_id")
        if parent_id:
            by_parent.setdefault(str(parent_id), []).append((session_id, path, meta))

    found: list[tuple[str, Path, dict[str, Any]]] = []
    queue = [root_id]
    seen = {root_id}
    while queue:
        parent = queue.pop(0)
        for child in by_parent.get(parent, []):
            if child[0] in seen:
                continue
            seen.add(child[0])
            found.append(child)
            queue.append(child[0])
    return found


def all_session_files() -> list[tuple[str, Path]]:
    codex_root = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
    candidates: list[Path] = []
    for folder_name in ("sessions", "archived_sessions"):
        folder = codex_root / folder_name
        if folder.is_dir():
            candidates.extend(folder.glob("**/rollout-*.jsonl"))
    newest_by_id: dict[str, Path] = {}
    for path in candidates:
        session_id = session_id_from_path(path)
        if not session_id or not path.is_file():
            continue
        previous = newest_by_id.get(session_id)
        if previous is None or path.stat().st_mtime > previous.stat().st_mtime:
            newest_by_id[session_id] = path
    return sorted(newest_by_id.items(), key=lambda item: str(item[1]))


def summarize_models(calls: list[TokenCall]) -> str:
    models = sorted({call.model for call in calls})
    if not models:
        return "unknown"
    return models[0] if len(models) == 1 else f"multiple ({', '.join(models)})"


def summarize_efforts(calls: list[TokenCall]) -> str | None:
    efforts = sorted({call.reasoning_effort for call in calls if call.reasoning_effort})
    if not efforts:
        return None
    return efforts[0] if len(efforts) == 1 else f"multiple ({', '.join(efforts)})"


def redact_call_sessions(
    calls: list[TokenCall], session_ids: list[str], include_session_ids: bool
) -> None:
    aliases = {session_id: f"session-{index:03d}" for index, session_id in enumerate(session_ids, 1)}
    turn_aliases: dict[tuple[str | None, str], str] = {}
    for call in calls:
        original_session = call.session_id
        if call.turn_id:
            key = (original_session, call.turn_id)
            if key not in turn_aliases:
                turn_aliases[key] = f"turn-{len(turn_aliases) + 1:04d}"
            call.turn_id = turn_aliases[key]
        if call.session_id and not include_session_ids:
            call.session_id = aliases.get(call.session_id, "session")
        if hasattr(call, "_recorded_cwd"):
            delattr(call, "_recorded_cwd")


def ingest_codex_current(
    include_subagents: bool,
    include_session_ids: bool = False,
    last_turns: int | None = None,
    turn_offset: int | None = None,
) -> tuple[list[TokenCall], dict[str, Any], list[str]]:
    thread_id = os.environ.get("CODEX_THREAD_ID")
    if not thread_id:
        raise ValueError("CODEX_THREAD_ID is unavailable; pass an API usage JSON or manual counts")
    if last_turns is not None and turn_offset is not None:
        raise ValueError("Choose either a recent-turn count or one turn offset")

    root_path = find_rollout(thread_id)
    calls, _ = extract_codex_session(root_path, thread_id)
    selected_turns: list[dict[str, str]] | None = None
    query_scope = "current_conversation"
    scope_label = "Current conversation total"
    windows: list[tuple[datetime, datetime]] = []

    if last_turns is not None or turn_offset is not None:
        completed = completed_turn_windows(root_path, thread_id)
        requested = last_turns if last_turns is not None else turn_offset
        assert requested is not None
        if len(completed) < requested:
            raise ValueError(
                f"Only {len(completed)} completed Codex turns are available; requested {requested}"
            )
        if last_turns is not None:
            selected_turns = completed[-last_turns:]
            query_scope = "previous_turns"
            noun = "turn" if last_turns == 1 else "turns"
            scope_label = f"Previous {last_turns} completed {noun}"
        else:
            selected_turns = [completed[-turn_offset]]
            query_scope = "turn_offset"
            scope_label = f"Completed turn {turn_offset} back"

        selected_ids = {item["turn_id"] for item in selected_turns}
        calls = [call for call in calls if call.turn_id in selected_ids]
        for item in selected_turns:
            start = parse_iso(item["started_at"])
            end = parse_iso(item["completed_at"])
            if start and end:
                windows.append((start, end))

    root_turn_count = len({call.turn_id for call in calls if call.turn_id})

    descendants: list[tuple[str, Path, dict[str, Any]]] = []
    contributing_descendants: list[tuple[str, Path, dict[str, Any]]] = []
    if include_subagents:
        descendants = discover_descendants(thread_id)
        for child_id, child_path, child_meta in descendants:
            child_calls, _ = extract_codex_session(child_path, child_id)
            if windows:
                child_calls = [
                    call for call in child_calls if in_time_windows(call.timestamp, windows)
                ]
            if not child_calls:
                continue
            calls.extend(child_calls)
            contributing_descendants.append((child_id, child_path, child_meta))

    if include_session_ids:
        public_thread_id = thread_id
    else:
        public_thread_id = "main"
    public_session_ids = [thread_id] + [
        child_id for child_id, _, _ in contributing_descendants
    ]
    redact_call_sessions(calls, public_session_ids, include_session_ids)
    if not include_session_ids:
        for call in calls:
            if call.session_id == "session-001":
                call.session_id = "main"
            elif call.session_id and call.session_id.startswith("session-"):
                call.session_id = "subagent-" + call.session_id.removeprefix("session-")

    scope_turn_count = (
        len(selected_turns)
        if selected_turns is not None
        else root_turn_count
    )
    source = {
        "kind": "codex",
        "session_id": public_thread_id,
        "request_id": None,
        "model": summarize_models(calls),
        "reasoning_effort": summarize_efforts(calls),
        "service_tier": None,
        "usage_is_exact": True,
        "subscription_plan": "not used for price calculation",
        "subagent_sessions": len(contributing_descendants),
        "session_ids_redacted": not include_session_ids,
        "query_scope": query_scope,
        "scope_label": scope_label,
        "scope_turn_count": scope_turn_count,
        "scope_session_count": 1 + len(contributing_descendants),
        "coverage": "local conversation telemetry through the printed cutoff",
    }
    warnings = [
        "API-equivalent token estimate only; this is not an actual Codex subscription charge.",
        "Tool-call fees, taxes, credits, and subscription allocation are excluded.",
    ]
    if selected_turns is not None:
        warnings.append(
            "Turn queries use completed root-turn boundaries and exclude the active turn."
        )
        if include_subagents:
            warnings.append(
                "Subagent usage is included when its token-event timestamp falls inside a selected root-turn window."
            )
    else:
        warnings.append(
            "Conversation scope includes completed local model calls through the printed usage cutoff."
        )
    return calls, source, warnings


def ingest_codex_project(
    project_dir: Path, include_session_ids: bool = False
) -> tuple[list[TokenCall], dict[str, Any], list[str]]:
    resolved_project = project_dir.expanduser().resolve()
    if not resolved_project.is_dir():
        raise ValueError(f"Codex project path is not a directory: {resolved_project}")

    calls: list[TokenCall] = []
    included_session_ids: list[str] = []
    subagent_sessions = 0
    skipped_logs = 0
    for session_id, path in all_session_files():
        try:
            session_calls, meta = extract_codex_session(path, session_id)
        except (OSError, ValueError):
            skipped_logs += 1
            continue
        session_calls = [
            call
            for call in session_calls
            if getattr(call, "_recorded_cwd", None)
            and path_is_within(Path(str(getattr(call, "_recorded_cwd"))), resolved_project)
        ]
        if not session_calls:
            continue
        calls.extend(session_calls)
        included_session_ids.append(session_id)
        source_meta = meta.get("source")
        if isinstance(source_meta, dict) and isinstance(source_meta.get("subagent"), dict):
            subagent_sessions += 1

    if not calls:
        raise ValueError(f"No completed Codex model usage matched project folder {resolved_project}")

    turn_keys = {
        (call.session_id, call.turn_id)
        for call in calls
        if call.session_id and call.turn_id
    }
    redact_call_sessions(calls, included_session_ids, include_session_ids)
    project_fingerprint = hashlib.sha256(str(resolved_project).encode("utf-8")).hexdigest()
    project_name = resolved_project.name or resolved_project.anchor
    source = {
        "kind": "codex",
        "session_id": "project-ref-" + project_fingerprint[:10],
        "request_id": None,
        "model": summarize_models(calls),
        "reasoning_effort": summarize_efforts(calls),
        "service_tier": None,
        "usage_is_exact": True,
        "subscription_plan": "not used for price calculation",
        "subagent_sessions": subagent_sessions,
        "session_ids_redacted": not include_session_ids,
        "query_scope": "project",
        "scope_label": f"Project total · {project_name}",
        "scope_turn_count": len(turn_keys),
        "scope_session_count": len(included_session_ids),
        "project_name": project_name,
        "project_path_hash": project_fingerprint[:16],
        "project_path_redacted": True,
        "coverage": "local Codex model calls recorded inside the selected project directory",
    }
    warnings = [
        "API-equivalent token estimate only; this is not an actual Codex subscription charge.",
        "Tool-call fees, taxes, credits, and subscription allocation are excluded.",
        "Project scope includes local Codex model calls whose recorded working directory is the selected folder or a descendant; remote, deleted, or unlogged sessions are not included.",
        "The stable project path fingerprint can link receipts generated for the same local path.",
    ]
    if skipped_logs:
        warnings.append(
            f"{skipped_logs} local session log(s) could not be parsed and were excluded."
        )
    return calls, source, warnings


def find_usage_object(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if isinstance(data.get("usage"), dict):
        return data["usage"], data
    response = data.get("response")
    if isinstance(response, dict):
        body = response.get("body")
        if isinstance(body, dict) and isinstance(body.get("usage"), dict):
            return body["usage"], body
    body = data.get("body")
    if isinstance(body, dict) and isinstance(body.get("usage"), dict):
        return body["usage"], body
    usage_keys = {"input_tokens", "prompt_tokens", "output_tokens", "completion_tokens"}
    if usage_keys.intersection(data):
        return data, data
    raise ValueError("No supported usage object found in JSON")


def ingest_usage_json(
    path: Path,
    model_override: str | None,
    include_source_metadata: bool = False,
) -> tuple[list[TokenCall], dict[str, Any], list[str]]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("Usage JSON must contain an object")
    usage, envelope = find_usage_object(data)
    model = str(model_override or envelope.get("model") or data.get("model") or "unknown")

    input_tokens = nested_int(usage, (("input_tokens",), ("prompt_tokens",)))
    cached = nested_int(
        usage,
        (
            ("input_tokens_details", "cached_tokens"),
            ("prompt_tokens_details", "cached_tokens"),
            ("input_cached_tokens",),
            ("cached_input_tokens",),
        ),
    )
    cache_write = nested_int(
        usage,
        (
            ("input_tokens_details", "cache_write_tokens"),
            ("input_tokens_details", "cache_write_input_tokens"),
            ("input_cache_write_tokens",),
            ("cache_write_input_tokens",),
        ),
    )
    output_tokens = nested_int(usage, (("output_tokens",), ("completion_tokens",)))
    reasoning = nested_int(
        usage,
        (
            ("output_tokens_details", "reasoning_tokens"),
            ("completion_tokens_details", "reasoning_tokens"),
            ("reasoning_output_tokens",),
        ),
    )
    aggregated = bool(usage.get("num_model_requests", 0) and usage.get("num_model_requests") != 1)
    call = TokenCall(
        model=model,
        input_tokens=input_tokens,
        cached_input_tokens=cached,
        cache_write_input_tokens=cache_write,
        output_tokens=output_tokens,
        reasoning_output_tokens=reasoning,
        aggregated=aggregated,
    )
    validate_call(call)
    source_kind = "chat_completions" if "prompt_tokens" in usage else "responses"
    request_id = envelope.get("id") or data.get("id")
    source = {
        "kind": source_kind,
        "session_id": None,
        "request_id": request_id if include_source_metadata else None,
        "source_metadata_redacted": not include_source_metadata,
        "model": model,
        "reasoning_effort": None,
        "service_tier": envelope.get("service_tier") or data.get("service_tier"),
        "usage_is_exact": True,
        "subagent_sessions": 0,
        "query_scope": "supplied_usage",
        "scope_label": "Supplied usage object",
        "scope_turn_count": None,
        "scope_session_count": None,
    }
    if include_source_metadata:
        source["input_file"] = path.name
    warnings = [
        "Token cost is an estimate from the supplied usage object and rate card, not an invoice.",
        "Tool fees, taxes, credits, subscription charges, and other non-token items are excluded.",
    ]
    if "total_tokens" in usage:
        declared_total = as_nonnegative_int(usage.get("total_tokens"), "total_tokens")
        if declared_total != call.total_tokens:
            warnings.append(
                f"Declared total_tokens ({declared_total}) differs from input + output ({call.total_tokens})."
            )
    if aggregated:
        warnings.append("Aggregated usage lacks per-request context lengths; long-context modifiers cannot be verified.")
    if source["service_tier"] not in (None, "default", "standard"):
        warnings.append(f"Service tier {source['service_tier']} may use different pricing.")
    if include_source_metadata:
        warnings.append(
            "API request identifiers and the supplied source filename were retained by explicit request."
        )
    return [call], source, warnings


def ingest_manual(args: argparse.Namespace) -> tuple[list[TokenCall], dict[str, Any], list[str]]:
    if not args.model:
        raise ValueError("Manual usage requires --model")
    if args.input_tokens is None and args.output_tokens is None:
        raise ValueError(
            "Choose a Codex query scope, --usage-json, or pass manual token counts"
        )
    call = TokenCall(
        model=args.model,
        input_tokens=as_nonnegative_int(args.input_tokens, "input_tokens"),
        cached_input_tokens=as_nonnegative_int(args.cached_input_tokens, "cached_input_tokens"),
        cache_write_input_tokens=as_nonnegative_int(
            args.cache_write_input_tokens, "cache_write_input_tokens"
        ),
        output_tokens=as_nonnegative_int(args.output_tokens, "output_tokens"),
        reasoning_output_tokens=as_nonnegative_int(args.reasoning_tokens, "reasoning_tokens"),
    )
    validate_call(call)
    source = {
        "kind": "manual",
        "session_id": None,
        "request_id": None,
        "model": args.model,
        "reasoning_effort": None,
        "service_tier": None,
        "usage_is_exact": args.manual_exact,
        "subagent_sessions": 0,
        "query_scope": "manual",
        "scope_label": "Manual token counts",
        "scope_turn_count": None,
        "scope_session_count": None,
    }
    warning = (
        "Token counts were supplied manually from an authoritative usage record."
        if args.manual_exact
        else "Token counts were supplied manually; verify them against the original usage record."
    )
    return [call], source, [warning]


def decimal_arg(value: str) -> Decimal:
    try:
        result = Decimal(value)
    except Exception as exc:
        raise argparse.ArgumentTypeError("rate must be a decimal number") from exc
    if not result.is_finite():
        raise argparse.ArgumentTypeError("rate must be a finite decimal number")
    if result < 0:
        raise argparse.ArgumentTypeError("rate cannot be negative")
    return result


def choose_rates(
    args: argparse.Namespace,
    calls: list[TokenCall],
    allow_approximate: bool = False,
) -> tuple[dict[str, Any] | None, list[str]]:
    explicit = [
        args.input_rate,
        args.cached_input_rate,
        args.cache_write_input_rate,
        args.output_rate,
    ]
    if any(value is not None for value in explicit):
        if not all(value is not None for value in explicit):
            raise ValueError("Explicit pricing requires all four rate options")
        if not args.pricing_as_of or not args.pricing_source:
            raise ValueError(
                "Explicit pricing requires --pricing-as-of and --pricing-source"
            )
        if not re.match(r"^https?://", args.pricing_source, flags=re.IGNORECASE):
            raise ValueError("--pricing-source must be an http(s) URL")
        profile = {
            "input": args.input_rate,
            "cached_input": args.cached_input_rate,
            "cache_write_input": args.cache_write_input_rate,
            "output": args.output_rate,
            "as_of": args.pricing_as_of or "user supplied",
            "source": args.pricing_source or "user supplied",
            "long_context_threshold": None,
            "long_input_multiplier": Decimal("1"),
            "long_output_multiplier": Decimal("1"),
            "origin": "explicit",
        }
        exact_models = {call.model for call in calls}
        notes = []
        if len(exact_models) > 1:
            notes.append("One explicit rate card was applied to multiple models.")
        return profile, notes

    models = sorted({call.model for call in calls})
    unknown_models = [model for model in models if model not in BUILTIN_PRICING]
    if unknown_models and not allow_approximate:
        return None, [
            "No exact verified rate card matched every model; pass explicit rates to calculate a USD token subtotal."
        ]

    fallback_model = "gpt-5.6-terra"
    model_profiles: dict[str, dict[str, Any]] = {}
    for model in models:
        pricing_model = model if model in BUILTIN_PRICING else fallback_model
        model_profiles[model] = {
            **BUILTIN_PRICING[pricing_model],
            "pricing_model": pricing_model,
            "exact_match": model == pricing_model,
        }

    sources = sorted({item["source"] for item in model_profiles.values()})
    as_of_values = sorted({item["as_of"] for item in model_profiles.values()})
    approximate = bool(unknown_models)
    bundle = {
        "model_profiles": model_profiles,
        "as_of": as_of_values[-1],
        "source": (
            sources[0]
            if len(sources) == 1
            else "https://developers.openai.com/api/docs/models"
        ),
        "source_urls": sources,
        "long_context_threshold": 272000,
        "long_input_multiplier": Decimal("2"),
        "long_output_multiplier": Decimal("1.5"),
        "origin": (
            "embedded per-model snapshots with reference-model approximation"
            if approximate
            else "embedded exact per-model snapshots"
        ),
        "approximate": approximate,
        "fallback_reference_model": fallback_model if approximate else None,
        "unknown_models": unknown_models,
    }
    notes: list[str] = []
    if len(models) > 1:
        notes.append(
            "Each recognized model was priced with its own official rate card; mixed-model rows show effective blended rates."
        )
    if approximate:
        notes.append(
            "No exact rate card matched "
            + ", ".join(unknown_models)
            + f"; those calls use {fallback_model} as a clearly labeled reference-model approximation."
        )
    return bundle, notes


def money_decimal(value: Decimal) -> str:
    return format(value, "f")


def call_pricing_profile(profile: dict[str, Any], call: TokenCall) -> dict[str, Any]:
    model_profiles = profile.get("model_profiles")
    if isinstance(model_profiles, dict):
        selected = model_profiles.get(call.model)
        if isinstance(selected, dict):
            return selected
    return profile


def effective_rate(
    amount: Decimal | None,
    tokens: int,
    profiles: list[dict[str, Any]],
    profile_key: str,
) -> str | None:
    if amount is not None and tokens:
        return format(amount * MILLION / Decimal(tokens), "f")
    values = {item[profile_key] for item in profiles if item.get(profile_key) is not None}
    if len(values) == 1:
        return format(next(iter(values)), "f")
    return None


def compute(calls: list[TokenCall], profile: dict[str, Any] | None) -> dict[str, Any]:
    totals = {
        "input_tokens": sum(call.input_tokens for call in calls),
        "cached_input_tokens": sum(call.cached_input_tokens for call in calls),
        "cache_write_input_tokens": sum(call.cache_write_input_tokens for call in calls),
        "fresh_input_tokens": sum(call.fresh_input_tokens for call in calls),
        "output_tokens": sum(call.output_tokens for call in calls),
        "visible_output_tokens": sum(call.visible_output_tokens for call in calls),
        "reasoning_output_tokens": sum(call.reasoning_output_tokens for call in calls),
        "total_tokens": sum(call.total_tokens for call in calls),
        "model_calls": len(calls),
    }
    category_tokens = {
        "fresh_input": totals["fresh_input_tokens"],
        "cached_input": totals["cached_input_tokens"],
        "cache_write_input": totals["cache_write_input_tokens"],
        "visible_output": totals["visible_output_tokens"],
        "reasoning_output": totals["reasoning_output_tokens"],
    }
    amounts: dict[str, Decimal | None] = {key: None for key in category_tokens}
    cache_savings: Decimal | None = None
    long_calls = 0

    if profile:
        amounts = {key: Decimal("0") for key in category_tokens}
        cache_savings = Decimal("0")
        for call in calls:
            call_profile = call_pricing_profile(profile, call)
            long_context = bool(
                not call.aggregated
                and call_profile.get("long_context_threshold")
                and call.input_tokens > int(call_profile["long_context_threshold"])
            )
            input_multiplier = (
                call_profile["long_input_multiplier"] if long_context else Decimal("1")
            )
            output_multiplier = (
                call_profile["long_output_multiplier"] if long_context else Decimal("1")
            )
            if long_context:
                long_calls += 1
            amounts["fresh_input"] += (
                Decimal(call.fresh_input_tokens)
                * call_profile["input"]
                * input_multiplier
                / MILLION
            )
            amounts["cached_input"] += (
                Decimal(call.cached_input_tokens)
                * call_profile["cached_input"]
                * input_multiplier
                / MILLION
            )
            amounts["cache_write_input"] += (
                Decimal(call.cache_write_input_tokens)
                * call_profile["cache_write_input"]
                * input_multiplier
                / MILLION
            )
            amounts["visible_output"] += (
                Decimal(call.visible_output_tokens)
                * call_profile["output"]
                * output_multiplier
                / MILLION
            )
            amounts["reasoning_output"] += (
                Decimal(call.reasoning_output_tokens)
                * call_profile["output"]
                * output_multiplier
                / MILLION
            )
            cache_savings += (
                Decimal(call.cached_input_tokens)
                * (call_profile["input"] - call_profile["cached_input"])
                * input_multiplier
                / MILLION
            )

    subtotal = sum((value for value in amounts.values() if value is not None), Decimal("0")) if profile else None
    cache_hit_rate = (
        Decimal(totals["cached_input_tokens"]) / Decimal(totals["input_tokens"]) * Decimal("100")
        if totals["input_tokens"]
        else Decimal("0")
    )
    used_profiles = [call_pricing_profile(profile, call) for call in calls] if profile else []
    effective_rates = {
        "fresh_input": effective_rate(
            amounts["fresh_input"], category_tokens["fresh_input"], used_profiles, "input"
        ),
        "cached_input": effective_rate(
            amounts["cached_input"],
            category_tokens["cached_input"],
            used_profiles,
            "cached_input",
        ),
        "cache_write_input": effective_rate(
            amounts["cache_write_input"],
            category_tokens["cache_write_input"],
            used_profiles,
            "cache_write_input",
        ),
        "visible_output": effective_rate(
            amounts["visible_output"],
            category_tokens["visible_output"],
            used_profiles,
            "output",
        ),
        "reasoning_output": effective_rate(
            amounts["reasoning_output"],
            category_tokens["reasoning_output"],
            used_profiles,
            "output",
        ),
    }
    output_amount = (
        amounts["visible_output"] + amounts["reasoning_output"]
        if profile
        else None
    )
    effective_rates["output"] = effective_rate(
        output_amount,
        totals["output_tokens"],
        used_profiles,
        "output",
    )
    return {
        **totals,
        "category_tokens": category_tokens,
        "category_amounts_usd": {
            key: money_decimal(value) if value is not None else None for key, value in amounts.items()
        },
        "effective_rates_usd_per_million": effective_rates,
        "known_token_subtotal_usd": money_decimal(subtotal) if subtotal is not None else None,
        "cache_savings_usd": money_decimal(cache_savings) if cache_savings is not None else None,
        "cache_hit_rate_percent": format(
            cache_hit_rate.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f"
        ),
        "long_context_calls": long_calls,
    }


def model_rate_cards_json(profile: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not profile:
        return []
    model_profiles = profile.get("model_profiles")
    if not isinstance(model_profiles, dict):
        return []
    return [
        {
            "observed_model": model,
            "pricing_model": item.get("pricing_model", model),
            "exact_match": bool(item.get("exact_match", True)),
            "source_url": item.get("source"),
            "rates_usd_per_million": {
                "fresh_input": format(item["input"], "f"),
                "cached_input": format(item["cached_input"], "f"),
                "cache_write_input": format(item["cache_write_input"], "f"),
                "output": format(item["output"], "f"),
            },
        }
        for model, item in sorted(model_profiles.items())
    ]


def build_record(
    args: argparse.Namespace,
    calls: list[TokenCall],
    source: dict[str, Any],
    warnings: list[str],
    profile: dict[str, Any] | None,
) -> dict[str, Any]:
    served_at = args.served_at or utc_now_iso()
    source = dict(source)
    source["usage_cutoff"] = localize_iso(
        max((call.timestamp for call in calls if call.timestamp), default=served_at)
    )
    usage_fingerprint = ";".join(
        f"{call.model}:{call.input_tokens}:{call.cached_input_tokens}:"
        f"{call.cache_write_input_tokens}:{call.output_tokens}:{call.reasoning_output_tokens}"
        for call in calls
    )
    seed = (
        f"{source.get('session_id')}|{source.get('request_id')}|{served_at}|"
        f"{source.get('query_scope')}|{source.get('scope_label')}|"
        f"{args.task_label}|{usage_fingerprint}"
    )
    receipt_id = "RCP-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:10].upper()
    computed = compute(calls, profile)
    pricing_status = "unavailable"
    if profile:
        if profile.get("approximate"):
            pricing_status = (
                "approximate_api_equivalent_estimate"
                if source["kind"] == "codex"
                else "approximate_estimate"
            )
        else:
            pricing_status = "api_equivalent_estimate" if source["kind"] == "codex" else "estimated"
    record: dict[str, Any] = {
        "schema_version": "1.0",
        "receipt": {
            "id": receipt_id,
            "generated_at": served_at,
            "title": args.title,
            "task_label": args.task_label,
            "checksum_sha256": None,
        },
        "source": source,
        "usage": {
            key: computed[key]
            for key in (
                "input_tokens",
                "cached_input_tokens",
                "cache_write_input_tokens",
                "output_tokens",
                "reasoning_output_tokens",
                "total_tokens",
                "model_calls",
            )
        }
        | {"subagent_sessions": source.get("subagent_sessions", 0)},
        "pricing": {
            "status": pricing_status,
            "currency": "USD",
            "unit_tokens": 1_000_000,
            "as_of": profile.get("as_of") if profile else args.pricing_as_of,
            "source_url": profile.get("source") if profile else args.pricing_source,
            "source_urls": profile.get("source_urls", []) if profile else [],
            "origin": profile.get("origin") if profile else None,
            "rates_usd_per_million": {
                "fresh_input": computed["effective_rates_usd_per_million"]["fresh_input"],
                "cached_input": computed["effective_rates_usd_per_million"]["cached_input"],
                "cache_write_input": computed["effective_rates_usd_per_million"]["cache_write_input"],
                "output": computed["effective_rates_usd_per_million"]["output"],
            },
            "model_rate_cards": model_rate_cards_json(profile),
            "approximation": (
                {
                    "reference_model": profile.get("fallback_reference_model"),
                    "models_without_exact_rate": profile.get("unknown_models", []),
                }
                if profile and profile.get("approximate")
                else None
            ),
            "modifiers": (
                [
                    {
                        "kind": "long_context",
                        "threshold_input_tokens": profile.get("long_context_threshold"),
                        "input_multiplier": format(profile["long_input_multiplier"], "f"),
                        "output_multiplier": format(profile["long_output_multiplier"], "f"),
                    }
                ]
                if profile and profile.get("long_context_threshold")
                else []
            ),
        },
        "computed": {
            "fresh_input_tokens": computed["fresh_input_tokens"],
            "visible_output_tokens": computed["visible_output_tokens"],
            "category_amounts_usd": computed["category_amounts_usd"],
            "effective_rates_usd_per_million": computed[
                "effective_rates_usd_per_million"
            ],
            "known_token_subtotal_usd": computed["known_token_subtotal_usd"],
            "cache_savings_usd": computed["cache_savings_usd"],
            "cache_hit_rate_percent": computed["cache_hit_rate_percent"],
            "long_context_calls": computed["long_context_calls"],
            "actual_charge_usd": None,
            "warnings": list(dict.fromkeys(warnings)),
        },
        "calls": [asdict(call) | {"fresh_input_tokens": call.fresh_input_tokens} for call in calls],
    }
    checksum_payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    record["receipt"]["checksum_sha256"] = hashlib.sha256(
        checksum_payload.encode("utf-8")
    ).hexdigest()
    return record


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def fmt_int(value: int) -> str:
    return f"{value:,}"


def fmt_rate(value: str | None) -> str:
    if value is None:
        return "—"
    amount = Decimal(value)
    if amount != 0 and abs(amount) < Decimal("0.01"):
        return "<$0.01"
    rounded = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return "$" + format(rounded, ".2f")


def fmt_money(value: str | None) -> str:
    if value is None:
        return "N/A"
    amount = Decimal(value)
    if amount != 0 and abs(amount) < Decimal("0.01"):
        return "<$0.01"
    rounded = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"${rounded:,.2f}"


def load_receipt_record(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        record = json.load(handle)
    if not isinstance(record, dict):
        raise ValueError("Receipt JSON must contain an object")
    if record.get("schema_version") != "1.0":
        raise ValueError("Unsupported receipt schema version")
    for section in ("receipt", "source", "usage", "pricing", "computed"):
        if not isinstance(record.get(section), dict):
            raise ValueError(f"Receipt JSON is missing the {section} section")

    usage = record["usage"]
    for field in (
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
        "model_calls",
        "subagent_sessions",
    ):
        value = usage.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"Receipt usage field {field} must be a nonnegative integer")
    if usage["input_tokens"] < usage["cached_input_tokens"] + usage["cache_write_input_tokens"]:
        raise ValueError("Receipt cached reads plus cache writes exceed input tokens")
    if usage["reasoning_output_tokens"] > usage["output_tokens"]:
        raise ValueError("Receipt reasoning tokens exceed output tokens")
    if usage["total_tokens"] != usage["input_tokens"] + usage["output_tokens"]:
        raise ValueError("Receipt total tokens do not equal input plus output")

    checksum = record["receipt"].get("checksum_sha256")
    if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise ValueError("Receipt checksum is missing or invalid")
    checksum_record = json.loads(json.dumps(record, ensure_ascii=False))
    checksum_record["receipt"]["checksum_sha256"] = None
    checksum_payload = json.dumps(
        checksum_record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    expected = hashlib.sha256(checksum_payload.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(checksum, expected):
        raise ValueError("Receipt checksum does not match its audit data")
    return record


def receipt_mark_data_uri() -> str:
    asset = Path(__file__).resolve().parent.parent / "assets" / "receipt-mark.svg"
    if not asset.is_file():
        raise FileNotFoundError(f"Receipt mark asset not found: {asset}")
    return "data:image/svg+xml;base64," + base64.b64encode(asset.read_bytes()).decode("ascii")


def render_codex_invoice_html(record: dict[str, Any], paper: str) -> str:
    if paper not in {"80mm", "a4"}:
        raise ValueError(f"Unsupported Codex invoice paper size: {paper}")

    receipt = record["receipt"]
    source = record["source"]
    usage = record["usage"]
    pricing = record["pricing"]
    computed = record["computed"]
    rates = pricing["rates_usd_per_million"]
    effective_rates = computed.get("effective_rates_usd_per_million") or {}
    amounts = computed["category_amounts_usd"]
    rows = [
        (
            "Fresh input",
            computed["fresh_input_tokens"],
            effective_rates.get("fresh_input", rates["fresh_input"]),
            amounts["fresh_input"],
        ),
        (
            "Cached input",
            usage["cached_input_tokens"],
            effective_rates.get("cached_input", rates["cached_input"]),
            amounts["cached_input"],
        ),
        (
            "Cache write",
            usage["cache_write_input_tokens"],
            effective_rates.get("cache_write_input", rates["cache_write_input"]),
            amounts["cache_write_input"],
        ),
        (
            "Visible output",
            computed["visible_output_tokens"],
            effective_rates.get("visible_output", rates["output"]),
            amounts["visible_output"],
        ),
        (
            "Reasoning output (included)",
            usage["reasoning_output_tokens"],
            effective_rates.get("reasoning_output", rates["output"]),
            amounts["reasoning_output"],
        ),
    ]
    row_html = "\n".join(
        f"<tr><td>{esc(label)}</td><td>{esc(fmt_int(tokens))}</td>"
        f"<td>{esc(fmt_rate(rate))}</td><td>{esc(fmt_money(amount))}</td></tr>"
        for label, tokens, rate, amount in rows
    )
    warning_html = "".join(f"<li>{esc(item)}</li>" for item in computed["warnings"])
    source_label = source.get("kind", "unknown").replace("_", " ").title()
    scope_label = source.get("scope_label") or "Entire supplied record"
    scope_turns = source.get("scope_turn_count")
    scope_sessions = source.get("scope_session_count")
    scope_turns_text = fmt_int(scope_turns) if isinstance(scope_turns, int) else "-"
    scope_sessions_text = fmt_int(scope_sessions) if isinstance(scope_sessions, int) else "-"
    model = source.get("model") or "unknown"
    effort = source.get("reasoning_effort") or "-"
    price_date = pricing.get("as_of") or "Unavailable"
    source_url = pricing.get("source_url") or "No exact rate source"
    document_title = str(receipt.get("title") or "Token Usage Receipt")
    task_label = str(receipt.get("task_label") or "")
    task_label_html = (
        f"<div><dt>Task label</dt><dd>{esc(task_label)}</dd></div>"
        if task_label
        else ""
    )
    total = fmt_money(computed.get("known_token_subtotal_usd"))
    status = pricing.get("status", "unavailable")
    total_label = {
        "api_equivalent_estimate": "API-equivalent token subtotal",
        "approximate_api_equivalent_estimate": "Approx. API-equivalent subtotal",
        "approximate_estimate": "Approximate token subtotal",
    }.get(status, "Estimated token subtotal")
    if computed.get("known_token_subtotal_usd") is None:
        total_label = "Token subtotal unavailable"
    actual = (
        fmt_money(computed.get("actual_charge_usd"))
        if computed.get("actual_charge_usd") is not None
        else "Unavailable"
    )
    estimate_label = {
        "api_equivalent_estimate": "API-equivalent estimate",
        "approximate_api_equivalent_estimate": "Approx. API-equivalent estimate",
        "approximate_estimate": "Approximate estimate",
        "estimated": "Estimated",
        "actual": "Actual",
        "unavailable": "Unavailable",
    }.get(status, status.replace("_", " ").capitalize())
    embedded = json.dumps(record, ensure_ascii=False, sort_keys=True).replace("</", "<\\/")
    receipt_mark = receipt_mark_data_uri()
    body_class = "paper-a4" if paper == "a4" else "paper-80mm"
    page_size = "A4" if paper == "a4" else "80mm 230mm"
    page_script = ""
    if paper == "80mm":
        page_script = """<script>
(() => {
  const invoice = document.querySelector('.invoice');
  const heightMm = Math.ceil(invoice.scrollHeight * 25.4 / 96) + 2;
  const pageRule = document.createElement('style');
  pageRule.textContent = `@page { size: 80mm ${heightMm}mm; margin: 0; }`;
  document.head.appendChild(pageRule);
})();
</script>"""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(document_title)} · {esc(receipt['id'])}</title>
<style>
:root {{ --ink:#202124; --muted:#66676b; --line:#d9dadd; --line-soft:#ececef; --canvas:#f7f7f8; --luxury-ink:#0a0a0a; }}
* {{ box-sizing:border-box; }}
html,body {{ margin:0; min-height:100%; }}
body {{ display:flex; justify-content:center; align-items:flex-start; color:var(--ink); background:var(--canvas); font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Helvetica Neue","Arial Unicode MS","PingFang SC",sans-serif; font-variant-numeric:tabular-nums; -webkit-print-color-adjust:exact; print-color-adjust:exact; }}
.invoice {{ position:relative; overflow:hidden; background:#fff; box-shadow:0 4px 8px rgba(0,0,0,.11); }}
.paper-80mm {{ padding:32px 12px 60px; color:var(--luxury-ink); background:#ededee; font-family:"Helvetica Neue",Arial,"Arial Unicode MS","PingFang SC",sans-serif; }}
.paper-80mm .invoice {{ width:80mm; padding:10mm 6.5mm 9mm; }}
.paper-a4 {{ padding:36px 18px 60px; }}
.paper-a4 .invoice {{ display:flex; flex-direction:column; width:210mm; min-height:297mm; padding:18mm 22mm 20mm; }}
.brand-row {{ display:flex; align-items:flex-start; justify-content:space-between; gap:24px; padding-bottom:22px; border-bottom:1px solid var(--line); }}
.brand-lockup {{ display:flex; align-items:center; gap:10px; min-width:0; }}
.brand-logo {{ width:38px; height:38px; object-fit:contain; flex:0 0 auto; }}
.brand-name {{ display:block; font-size:17px; line-height:1.15; font-weight:600; letter-spacing:-.01em; }}
.document-id {{ text-align:right; min-width:0; }}
.document-id span {{ display:block; color:var(--muted); font-size:9.5px; line-height:1.35; }}
.document-id strong {{ display:block; margin-top:3px; font-size:11px; line-height:1.35; font-weight:600; overflow-wrap:anywhere; }}
.hero {{ padding:24px 0 26px; border-bottom:1px solid var(--line); }}
h1 {{ margin:0; font-size:28px; line-height:1.14; letter-spacing:-.025em; font-weight:600; text-wrap:balance; }}
.metadata {{ display:grid; grid-template-columns:1fr 1fr; gap:48px; margin-top:24px; }}
.metadata dl {{ margin:0; }}
.metadata div {{ display:block; padding:0 0 12px; }}
.metadata dt {{ color:var(--muted); font-size:9.5px; line-height:1.35; }}
.metadata dd {{ margin:3px 0 0; font-size:11px; line-height:1.4; text-align:left; overflow-wrap:anywhere; }}
.usage-section {{ margin-top:20px; }}
.section-heading {{ display:flex; align-items:baseline; justify-content:space-between; gap:20px; margin-bottom:10px; }}
h2 {{ margin:0; font-size:14px; line-height:1.3; font-weight:600; letter-spacing:-.01em; }}
.section-heading span {{ color:var(--muted); font-size:9.5px; }}
table {{ width:100%; border-collapse:collapse; table-layout:fixed; font-size:11px; }}
th {{ padding:0 0 9px; color:var(--muted); border-bottom:1px solid var(--line); font-size:9.5px; line-height:1.35; font-weight:500; text-align:right; }}
td {{ padding:11px 0; border-bottom:1px solid var(--line-soft); line-height:1.4; text-align:right; vertical-align:top; }}
tbody tr:last-child td {{ border-bottom:0; }}
th:first-child,td:first-child {{ width:43%; padding-right:12px; text-align:left; }}
th:nth-child(2),td:nth-child(2) {{ width:20%; }}
th:nth-child(3),td:nth-child(3) {{ width:15%; }}
th:last-child,td:last-child {{ width:22%; }}
th:not(:first-child),td:not(:first-child) {{ white-space:nowrap; }}
.summary {{ display:grid; grid-template-columns:minmax(0,1fr) 245px; gap:36px; margin-top:26px; }}
.usage-stats {{ border-top:1px solid var(--line); border-bottom:1px solid var(--line); }}
.usage-stat {{ display:flex; justify-content:space-between; gap:18px; padding:7px 0; border-bottom:1px solid var(--line-soft); }}
.usage-stat:last-child {{ border-bottom:0; }}
.usage-stat span {{ color:var(--muted); font-size:9.5px; line-height:1.4; }}
.usage-stat strong {{ font-size:11px; line-height:1.4; font-weight:600; text-align:right; }}
.totals {{ margin:0; }}
.totals div {{ display:flex; justify-content:space-between; gap:24px; padding:7px 0; }}
.totals dt,.totals dd {{ margin:0; font-size:10.5px; line-height:1.45; }}
.totals dt {{ color:var(--muted); }}
.totals dd {{ text-align:right; }}
.totals .grand {{ margin-top:6px; padding-top:13px; border-top:1px solid var(--ink); }}
.totals .grand dt {{ max-width:145px; color:var(--ink); font-size:11px; font-weight:600; }}
.totals .grand dd {{ color:var(--ink); font-size:18px; line-height:1.2; font-weight:600; white-space:nowrap; }}
.notes {{ margin-top:24px; padding-top:16px; border-top:1px solid var(--line); }}
.notes h2 {{ font-size:12px; }}
.notes ul {{ margin:8px 0 0; padding-left:17px; color:var(--muted); font-size:10px; line-height:1.55; }}
.notes li+li {{ margin-top:4px; }}
.rate-source {{ margin:10px 0 0; color:var(--muted); font-size:9px; line-height:1.5; overflow-wrap:anywhere; }}
.footer {{ display:flex; justify-content:space-between; gap:20px; margin-top:24px; padding-top:14px; border-top:1px solid var(--line); color:var(--muted); font-size:8.75px; line-height:1.45; }}
.paper-a4 .footer {{ display:grid; gap:5px; margin-top:auto; }}
.footer .checksum {{ font:500 7.75px/1.45 "SFMono-Regular",Menlo,Monaco,monospace; overflow-wrap:anywhere; }}
.paper-80mm .brand-row {{ display:block; padding-bottom:15px; border-color:#111; text-align:center; }}
.paper-80mm .brand-lockup {{ display:block; }}
.paper-80mm .brand-logo {{ width:32px; height:32px; filter:grayscale(1) contrast(1.45); }}
.paper-80mm .brand-name {{ margin-top:8px; font-size:15px; line-height:1; font-weight:600; letter-spacing:.18em; text-transform:uppercase; }}
.paper-80mm .document-id {{ margin-top:12px; text-align:center; }}
.paper-80mm .document-id span {{ color:#333; font-size:6.2px; letter-spacing:.12em; text-transform:uppercase; }}
.paper-80mm .document-id strong {{ margin-top:3px; font:500 7.3px/1.35 "SFMono-Regular",Menlo,Monaco,monospace; letter-spacing:.05em; }}
.paper-80mm .hero {{ padding:11px 0 13px; border-color:#111; text-align:center; }}
.paper-80mm h1 {{ font-size:8.5px; line-height:1.35; font-weight:600; letter-spacing:.16em; text-transform:uppercase; }}
.paper-80mm .metadata {{ display:block; margin-top:14px; }}
.paper-80mm .metadata dl+dl {{ margin-top:0; }}
.paper-80mm .metadata div {{ display:grid; grid-template-columns:26mm minmax(0,1fr); gap:7px; padding:3px 0; }}
.paper-80mm .metadata dt {{ color:#333; font-size:6.2px; line-height:1.4; letter-spacing:.07em; text-transform:uppercase; }}
.paper-80mm .metadata dd {{ margin:0; font:500 7.15px/1.4 "SFMono-Regular",Menlo,Monaco,monospace; text-align:right; }}
.paper-80mm .usage-section {{ margin-top:16px; }}
.paper-80mm .section-heading {{ margin-bottom:6px; }}
.paper-80mm h2 {{ font-size:7.2px; line-height:1.3; font-weight:600; letter-spacing:.12em; text-transform:uppercase; }}
.paper-80mm .section-heading span {{ font-size:5.8px; }}
.paper-80mm table {{ font:500 6.55px/1.35 "SFMono-Regular",Menlo,Monaco,monospace; }}
.paper-80mm th {{ padding:5px 0; color:#222; border-top:1px solid #111; border-bottom:1px solid #111; font-size:5.8px; font-weight:500; letter-spacing:.03em; text-transform:uppercase; }}
.paper-80mm td {{ padding:6px 0; border-bottom:.5px solid #888; }}
.paper-80mm tbody tr:last-child td {{ border-bottom:.5px solid #111; }}
.paper-80mm th:first-child,.paper-80mm td:first-child {{ width:37%; padding-right:5px; }}
.paper-80mm th:nth-child(2),.paper-80mm td:nth-child(2) {{ width:23%; }}
.paper-80mm th:nth-child(3),.paper-80mm td:nth-child(3) {{ width:17%; }}
.paper-80mm th:last-child,.paper-80mm td:last-child {{ width:23%; }}
.paper-80mm .summary {{ display:block; margin-top:14px; }}
.paper-80mm .usage-stats {{ border-color:#111; }}
.paper-80mm .usage-stat {{ padding:4.5px 0; border-color:#aaa; }}
.paper-80mm .usage-stat span {{ color:#333; font-size:6.2px; letter-spacing:.06em; text-transform:uppercase; }}
.paper-80mm .usage-stat strong {{ font:500 7.3px/1.4 "SFMono-Regular",Menlo,Monaco,monospace; }}
.paper-80mm .totals {{ margin-top:13px; }}
.paper-80mm .totals div {{ padding:4px 0; }}
.paper-80mm .totals dt,.paper-80mm .totals dd {{ font-size:7px; }}
.paper-80mm .totals .grand {{ margin-top:6px; padding-top:10px; border-top:1.2px solid #000; }}
.paper-80mm .totals .grand dt {{ max-width:38mm; font-size:8px; line-height:1.35; }}
.paper-80mm .totals .grand dd {{ font-size:15px; line-height:1; }}
.paper-80mm .notes {{ margin-top:16px; padding-top:12px; border-color:#111; }}
.paper-80mm .notes h2 {{ font-size:7.4px; letter-spacing:.1em; text-transform:uppercase; }}
.paper-80mm .notes ul {{ margin-top:7px; padding-left:12px; color:#333; font-size:6.35px; line-height:1.5; }}
.paper-80mm .rate-source {{ margin-top:8px; color:#333; font-size:5.8px; }}
.paper-80mm .footer {{ display:block; margin-top:14px; padding-top:10px; border-color:#111; color:#333; font-size:6px; text-align:center; }}
.paper-80mm .footer span {{ display:block; }}
.paper-80mm .footer .checksum {{ margin-top:6px; font:500 4.8px/1.35 "SFMono-Regular",Menlo,Monaco,monospace; letter-spacing:0; text-align:center; overflow-wrap:anywhere; word-break:break-all; }}
@media screen and (max-width:760px) {{
  .paper-a4 {{ padding:14px; }}
  .paper-a4 .invoice {{ width:100%; min-height:0; padding:28px 24px; }}
  .paper-a4 .metadata,.paper-a4 .summary {{ grid-template-columns:1fr; gap:20px; }}
  .paper-a4 .footer {{ margin-top:24px; }}
}}
@page {{ size:{page_size}; margin:0; }}
@media print {{
  html,body {{ min-height:0; background:#fff; }}
  body.paper-80mm,body.paper-a4 {{ display:block; padding:0; }}
  .invoice {{ box-shadow:none; }}
  .paper-80mm .invoice {{ width:80mm; color:#000; background:#fff; }}
  .paper-80mm .brand-logo {{ filter:grayscale(1) contrast(1.6); }}
  .paper-a4 .invoice {{ width:210mm; height:296mm; min-height:0; }}
}}
</style>
</head>
<body class="{body_class}">
<main class="invoice">
  <header class="brand-row">
    <div class="brand-lockup">
      <img class="brand-logo" src="{receipt_mark}" width="40" height="40" alt="Token receipt mark">
      <div><span class="brand-name">Codex</span></div>
    </div>
    <div class="document-id"><span>Receipt number</span><strong>{esc(receipt['id'])}</strong></div>
  </header>
  <section class="hero" aria-labelledby="invoice-title">
    <div>
      <h1 id="invoice-title">{esc(document_title)}</h1>
    </div>
  </section>
  <section class="metadata" aria-label="Receipt details">
    <dl>
      <div><dt>Issued at</dt><dd>{esc(receipt['generated_at'])}</dd></div>
      <div><dt>Usage through</dt><dd>{esc(source.get('usage_cutoff'))}</dd></div>
      <div><dt>Query scope</dt><dd>{esc(scope_label)}</dd></div>
      {task_label_html}
      <div><dt>Usage source</dt><dd>{esc(source_label)}</dd></div>
      <div><dt>Model</dt><dd>{esc(model)}</dd></div>
    </dl>
    <dl>
      <div><dt>Reasoning effort</dt><dd>{esc(effort)}</dd></div>
      <div><dt>Model calls</dt><dd>{esc(fmt_int(usage['model_calls']))}</dd></div>
      <div><dt>Turns matched</dt><dd>{esc(scope_turns_text)}</dd></div>
      <div><dt>Sessions matched</dt><dd>{esc(scope_sessions_text)}</dd></div>
      <div><dt>Pricing status</dt><dd>{esc(estimate_label)}</dd></div>
    </dl>
  </section>
  <section class="usage-section" aria-labelledby="usage-heading">
    <div class="section-heading"><h2 id="usage-heading">Usage details</h2><span>Rates in USD per 1M tokens</span></div>
    <table aria-label="Token usage cost breakdown">
      <thead><tr><th>Description</th><th>Quantity</th><th>Rate</th><th>Amount</th></tr></thead>
      <tbody>{row_html}</tbody>
    </table>
  </section>
  <section class="summary" aria-label="Usage summary">
    <div class="usage-stats">
      <div class="usage-stat"><span>Total tokens</span><strong>{esc(fmt_int(usage['total_tokens']))}</strong></div>
      <div class="usage-stat"><span>Cache hit</span><strong>{esc(computed['cache_hit_rate_percent'])}%</strong></div>
      <div class="usage-stat"><span>Input tokens</span><strong>{esc(fmt_int(usage['input_tokens']))}</strong></div>
      <div class="usage-stat"><span>Output tokens</span><strong>{esc(fmt_int(usage['output_tokens']))}</strong></div>
    </div>
    <dl class="totals">
      <div><dt>Cache savings vs fresh</dt><dd>{esc(fmt_money(computed.get('cache_savings_usd')))}</dd></div>
      <div><dt>Actual charge</dt><dd>{esc(actual)}</dd></div>
      <div class="grand"><dt>{esc(total_label)}</dt><dd>{esc(total)}</dd></div>
    </dl>
  </section>
  <section class="notes" aria-labelledby="notes-heading">
    <h2 id="notes-heading">Important information</h2>
    <ul>{warning_html}</ul>
    <p class="rate-source">Rate snapshot: {esc(price_date)}<br>{esc(source_url)}</p>
  </section>
  <footer class="footer">
    <span>Generated locally · Not an OpenAI invoice</span>
    <span class="checksum">SHA-256 {esc(receipt['checksum_sha256'])}</span>
  </footer>
</main>
<script type="application/json" id="receipt-data">{embedded}</script>
{page_script}
</body>
</html>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate an itemized electronic token-usage receipt as HTML and JSON."
    )
    source_group = parser.add_mutually_exclusive_group()
    source_group.add_argument(
        "--codex-current",
        action="store_true",
        help="Read the current Codex conversation total through the issuance cutoff",
    )
    source_group.add_argument(
        "--codex-last-turns",
        type=positive_int,
        metavar="N",
        help="Read the previous N completed turns in the current Codex conversation",
    )
    source_group.add_argument(
        "--codex-turn",
        type=positive_int,
        metavar="N",
        help="Read only the completed turn N turns back (1 means the previous turn)",
    )
    source_group.add_argument(
        "--codex-project",
        type=Path,
        metavar="DIR",
        help="Aggregate local Codex sessions recorded inside a project folder",
    )
    source_group.add_argument("--usage-json", type=Path, help="OpenAI response or usage JSON")
    source_group.add_argument(
        "--receipt-json",
        type=Path,
        help="Existing audit sidecar to render without recollecting or repricing usage",
    )
    parser.add_argument("--include-subagents", action="store_true", help="Include descendant Codex sessions")
    parser.add_argument(
        "--include-session-ids",
        action="store_true",
        help="Keep raw Codex session UUIDs in the audit JSON (redacted by default)",
    )
    parser.add_argument(
        "--include-source-metadata",
        action="store_true",
        help="Keep an API request ID and supplied usage filename (redacted by default)",
    )
    parser.add_argument("--model", help="Exact model ID; required for manual counts or to override JSON")
    parser.add_argument("--input-tokens", type=int)
    parser.add_argument("--cached-input-tokens", type=int, default=0)
    parser.add_argument("--cache-write-input-tokens", type=int, default=0)
    parser.add_argument("--output-tokens", type=int, default=0)
    parser.add_argument("--reasoning-tokens", type=int, default=0)
    parser.add_argument(
        "--manual-exact",
        action="store_true",
        help="Attest that manual counts were copied exactly from an authoritative usage record",
    )
    parser.add_argument("--input-rate", type=decimal_arg, help="USD per 1M fresh input tokens")
    parser.add_argument("--cached-input-rate", type=decimal_arg, help="USD per 1M cached input tokens")
    parser.add_argument("--cache-write-input-rate", type=decimal_arg, help="USD per 1M cache-write tokens")
    parser.add_argument("--output-rate", type=decimal_arg, help="USD per 1M output tokens")
    parser.add_argument("--pricing-as-of", help="Rate-card date or version")
    parser.add_argument("--pricing-source", help="Rate-card source URL")
    parser.add_argument("--title")
    parser.add_argument("--task-label", default="")
    parser.add_argument("--served-at", help="Receipt timestamp; defaults to local current time")
    parser.add_argument(
        "--style",
        choices=("codex-invoice",),
        default="codex-invoice",
        help="Compatibility layout flag; Codex Invoice is the only supported style",
    )
    parser.add_argument(
        "--paper",
        choices=("80mm", "a4"),
        default="80mm",
        help="Paper profile; A4 is available for the Codex invoice style",
    )
    parser.add_argument("--output", type=Path, required=True, help="Output .html path")
    parser.add_argument("--no-json", action="store_true", help="Do not write the audit JSON sidecar")
    args = parser.parse_args()
    codex_turn_source = bool(
        args.codex_current or args.codex_last_turns is not None or args.codex_turn is not None
    )
    codex_source = bool(codex_turn_source or args.codex_project is not None)
    if args.include_subagents and not codex_turn_source:
        parser.error(
            "--include-subagents requires --codex-current, --codex-last-turns, or --codex-turn; project scope already scans matching subagent sessions"
        )
    if args.include_session_ids and not codex_source:
        parser.error("--include-session-ids requires a Codex telemetry source")
    if args.include_source_metadata and not args.usage_json:
        parser.error("--include-source-metadata requires --usage-json")
    if args.manual_exact and (codex_source or args.usage_json or args.receipt_json):
        parser.error("--manual-exact only applies to manual token counts")
    if args.receipt_json and any(
        (
            args.model is not None,
            args.input_tokens is not None,
            args.output_tokens != 0,
            args.cached_input_tokens != 0,
            args.cache_write_input_tokens != 0,
            args.reasoning_tokens != 0,
            args.input_rate is not None,
            args.cached_input_rate is not None,
            args.cache_write_input_rate is not None,
            args.output_rate is not None,
            args.pricing_as_of is not None,
            args.pricing_source is not None,
            args.title is not None,
            bool(args.task_label),
            args.served_at is not None,
            args.include_source_metadata,
        )
    ):
        parser.error("--receipt-json cannot be combined with usage, pricing, title, or time overrides")
    if args.output.suffix.lower() not in ("", ".html", ".htm"):
        parser.error("--output must be an HTML path")
    if args.title is None:
        args.title = "Token Usage Receipt"
    return args


def main() -> int:
    args = parse_args()
    if args.receipt_json:
        record = load_receipt_record(args.receipt_json.expanduser().resolve())
    else:
        if args.codex_current:
            calls, source, warnings = ingest_codex_current(
                args.include_subagents, args.include_session_ids
            )
        elif args.codex_last_turns is not None:
            calls, source, warnings = ingest_codex_current(
                args.include_subagents,
                args.include_session_ids,
                last_turns=args.codex_last_turns,
            )
        elif args.codex_turn is not None:
            calls, source, warnings = ingest_codex_current(
                args.include_subagents,
                args.include_session_ids,
                turn_offset=args.codex_turn,
            )
        elif args.codex_project is not None:
            calls, source, warnings = ingest_codex_project(
                args.codex_project, args.include_session_ids
            )
        elif args.usage_json:
            calls, source, warnings = ingest_usage_json(
                args.usage_json.expanduser().resolve(),
                args.model,
                args.include_source_metadata,
            )
        else:
            calls, source, warnings = ingest_manual(args)
        if not calls:
            raise ValueError("No completed model usage was found before receipt issuance")

        profile, pricing_warnings = choose_rates(
            args,
            calls,
            allow_approximate=source.get("kind") == "codex",
        )
        warnings.extend(pricing_warnings)
        record = build_record(args, calls, source, warnings, profile)
    output = args.output.expanduser().resolve()
    if not output.suffix:
        output = output.with_suffix(".html")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_codex_invoice_html(record, args.paper), encoding="utf-8")
    print(f"HTML: {output}")
    if not args.no_json:
        sidecar = output.with_suffix(".json")
        sidecar.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"JSON: {sidecar}")
    print(f"Receipt: {record['receipt']['id']}")
    print(f"Query scope: {record['source'].get('scope_label', 'Entire supplied record')}")
    print(f"Usage cutoff: {record['source']['usage_cutoff']}")
    print(f"Generated at: {record['receipt']['generated_at']}")
    print(f"Total tokens: {record['usage']['total_tokens']:,}")
    subtotal = record["computed"]["known_token_subtotal_usd"]
    print(f"Known token subtotal: {fmt_money(subtotal)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
