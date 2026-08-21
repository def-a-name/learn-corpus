#!/usr/bin/env python3
"""把 Codex rollout JSONL 导入为标准化 Markdown 来源。"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from wiki_core import (
    CODEX_IMPORTER_VERSION,
    MANIFEST_PATH,
    REPO_ROOT,
    build_conversation_document,
    clean_message,
    conversation_skip_reason,
    derive_title,
    discover_jsonl,
    extract_text,
    is_noise_message,
    load_manifest,
    relative_to_repo,
    redact_secrets,
    save_manifest,
    sha256_file,
    source_needs_redaction,
    utc_now,
)


DEFAULT_INPUT = Path.home() / ".codex" / "sessions"
DEFAULT_OUTPUT = REPO_ROOT / "sources" / "conversations" / "codex"


def parse_session(path: Path) -> dict[str, Any]:
    from wiki_core import read_jsonl

    records, invalid_lines = read_jsonl(path)
    session_meta: dict[str, Any] = {}
    timestamp = ""
    messages: list[dict[str, str]] = []
    awaiting_protocol_answer = False

    for record in records:
        timestamp = timestamp or str(record.get("timestamp", ""))
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if record.get("type") == "session_meta":
            session_meta = payload
            continue
        if record.get("type") != "response_item" or payload.get("type") != "message":
            continue

        role = payload.get("role")
        if role == "user":
            text = extract_text(payload.get("content"), {"input_text"})
        elif role == "assistant" and payload.get("phase") in ("final_answer", None):
            text = extract_text(payload.get("content"), {"output_text"})
        else:
            continue
        text = clean_message(text)
        if role == "user" and is_noise_message(text):
            awaiting_protocol_answer = True
            continue
        if role == "assistant" and awaiting_protocol_answer:
            awaiting_protocol_answer = False
            continue
        if role == "user":
            awaiting_protocol_answer = False
        if text and (not messages or messages[-1] != {"role": role, "text": text}):
            messages.append({"role": role, "text": text})

    session_id = str(session_meta.get("id") or path.stem.removeprefix("rollout-"))
    created = str(session_meta.get("timestamp") or timestamp)[:10]
    cwd = str(session_meta.get("cwd") or "")
    project = Path(cwd).name if cwd else "global"
    return {
        "id": session_id,
        "created": created,
        "cwd": cwd,
        "project": project,
        "cli_version": session_meta.get("cli_version", ""),
        "messages": messages,
        "invalid_lines": invalid_lines,
    }


def import_sessions(
    input_dir: Path,
    output_dir: Path,
    manifest_path: Path,
    *,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    stats: dict[str, Any] = {
        "discovered": 0,
        "imported": 0,
        "unchanged": 0,
        "skipped": 0,
        "skip_reasons": {},
        "invalid_lines": 0,
    }

    for path in discover_jsonl(input_dir):
        digest = sha256_file(path)
        parsed = parse_session(path)
        source_id = f"codex-{parsed['id']}"
        key = source_id
        current = manifest["sources"].get(key, {})
        current_output = str(current.get("output_path") or "")
        current_output_path = Path(REPO_ROOT / current_output) if current_output else Path()
        if (
            current.get("source_hash") == digest
            and current.get("importer_version") == CODEX_IMPORTER_VERSION
            and current_output_path.is_file()
            and not source_needs_redaction(current_output_path)
        ):
            stats["discovered"] += 1
            stats["unchanged"] += 1
            stats["invalid_lines"] += parsed["invalid_lines"]
            continue
        messages = parsed["messages"]
        skip_reason = conversation_skip_reason(messages)
        if skip_reason:
            stats["discovered"] += 1
            stats["skipped"] += 1
            stats["invalid_lines"] += parsed["invalid_lines"]
            reasons = stats["skip_reasons"]
            reasons[skip_reason] = reasons.get(skip_reason, 0) + 1
            if not dry_run and current:
                old_output = Path(REPO_ROOT / current.get("output_path", ""))
                if old_output.is_file() and old_output.parent.resolve() == output_dir.resolve():
                    old_output.unlink()
                manifest["sources"].pop(key, None)
            continue
        if limit is not None and stats["imported"] >= limit:
            break

        stats["discovered"] += 1
        title, title_redactions = redact_secrets(derive_title("codex", messages))
        date = parsed["created"] if parsed["created"] != "unknown" else "undated"
        output_path = output_dir / f"{date}-{parsed['id']}.md"
        document, redactions = build_conversation_document(
            source_id=source_id,
            origin="codex",
            title=title,
            created=parsed["created"],
            imported=utc_now()[:10],
            project=parsed["project"],
            source_path=path,
            source_hash=digest,
            messages=messages,
            extra_metadata={
                "cwd": parsed["cwd"],
                "codex_version": parsed["cli_version"],
                "invalid_jsonl_lines": parsed["invalid_lines"],
            },
            initial_redactions=title_redactions,
        )
        stats["invalid_lines"] += parsed["invalid_lines"]
        stats["imported"] += 1
        if dry_run:
            continue
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(document, encoding="utf-8")
        manifest["sources"][key] = {
            "origin": "codex",
            "source_path": str(path.resolve()),
            "source_hash": digest,
            "output_path": relative_to_repo(output_path),
            "status": "pending" if current.get("source_hash") != digest else current.get("status", "pending"),
            "title": title,
            "created": parsed["created"],
            "redaction_count": redactions,
            "imported_at": utc_now(),
            "importer_version": CODEX_IMPORTER_VERSION,
        }

    if not dry_run:
        save_manifest(manifest, manifest_path)
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    stats = import_sessions(args.input, args.output, args.manifest, limit=args.limit, dry_run=args.dry_run)
    print("Codex 导入结果:", ", ".join(f"{key}={value}" for key, value in stats.items()))


if __name__ == "__main__":
    main()
