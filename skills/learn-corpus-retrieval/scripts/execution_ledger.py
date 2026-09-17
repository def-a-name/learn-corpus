#!/usr/bin/env python3
"""维护单次 Learn Corpus 检索任务的临时执行账本。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from datetime import datetime, timezone
import unicodedata


SCHEMA_VERSION = 2
DEFAULT_LIMITS = {"search_calls": 4, "read_calls": 8, "evidence_tokens": 8000}
OPERATIONS = {"search", "read_bundle"}
SCOPES = {"conversation", "note", "article"}
ERROR_CATEGORIES = {
    "service_error", "generation_mismatch", "authentication", "rate_limit",
    "budget_error", "integrity_error", "invalid_request", "unknown",
}
GENERATION_PATTERN = re.compile(r"gen_[0-9a-f]{20}")
ITEM_PATTERN = re.compile(r"itm_[a-z2-7]{32}")
BUNDLE_PATTERN = re.compile(r"bnd_[0-9a-f]{40}")


class LedgerError(Exception):
    """表示账本输入或状态违反约束。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json_input(inline: str | None, input_path: str | None) -> object:
    if inline is not None:
        raw = inline
    elif input_path is not None:
        raw = Path(input_path).read_text(encoding="utf-8")
    else:
        raw = os.fdopen(os.dup(0), encoding="utf-8").read()
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise LedgerError("input must be valid JSON") from exc


def _load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LedgerError("ledger file does not exist") from exc
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        raise LedgerError("ledger file is unreadable or invalid") from exc
    if type(value) is not dict or value.get("schema_version") != SCHEMA_VERSION:
        raise LedgerError("ledger schema is unsupported")
    return value


def _save(path: Path, ledger: dict) -> None:
    raw = json.dumps(ledger, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _integer(value: object, name: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise LedgerError(f"{name} must be an integer of at least {minimum}")
    return value


def _string(value: object, name: str, pattern: re.Pattern[str] | None = None) -> str:
    if type(value) is not str or not value:
        raise LedgerError(f"{name} must be a non-empty string")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise LedgerError(f"{name} has an invalid format")
    return value


def _usage(response: dict) -> tuple[int, str]:
    value = response.get("usage")
    if type(value) is not dict:
        raise LedgerError("response usage is missing")
    tokens = _integer(value.get("estimated_evidence_tokens"), "response usage", 0)
    estimator = _string(value.get("estimator_version"), "estimator version")
    return tokens, estimator


def _normalize_query(query: str) -> str:
    return " ".join(unicodedata.normalize("NFC", query).casefold().split())


def _canonical_scopes(value: object) -> list[str]:
    if value is None:
        return sorted(SCOPES)
    if type(value) is not list or not value or any(type(item) is not str for item in value):
        raise LedgerError("search scopes must be a non-empty array")
    if len(set(value)) != len(value) or not set(value) <= SCOPES:
        raise LedgerError("search scopes contain invalid or duplicate values")
    return sorted(value)


def _validate_request(operation: str, request: object, ledger: dict) -> dict:
    if type(request) is not dict:
        raise LedgerError("request must be a JSON object")
    if operation == "search":
        allowed = {"queries", "scopes", "limit", "generation", "max_estimated_tokens"}
        if request.keys() - allowed:
            raise LedgerError("search request contains unknown fields")
        if not {"queries", "max_estimated_tokens"} <= request.keys():
            raise LedgerError("search request is missing a required field")
        queries = request.get("queries")
        if type(queries) is not list or not 1 <= len(queries) <= 6 or any(
            type(query) is not str or not query.strip() for query in queries
        ):
            raise LedgerError("search queries must contain 1 to 6 non-empty strings")
        normalized = [_normalize_query(query) for query in queries]
        if len(set(normalized)) != len(normalized):
            raise LedgerError("search queries contain normalized duplicates")
        scopes = _canonical_scopes(request.get("scopes"))
        if "limit" in request:
            limit = _integer(request["limit"], "search limit", 1)
            if limit > 20:
                raise LedgerError("search limit exceeds the service maximum")
        generation = request.get("generation")
        if generation is not None:
            _string(generation, "request generation", GENERATION_PATTERN)
        pinned = ledger["generation"]
        if pinned is None and generation is not None:
            raise LedgerError("first search must omit generation")
        if pinned is not None and generation != pinned:
            raise LedgerError("search request must use the pinned generation")
        key = {"queries": sorted(normalized), "scopes": scopes}
        for call in ledger["calls"]:
            if call["operation"] == "search" and call.get("search_key") == key:
                raise LedgerError("duplicate search query and scope set")
        cleaned = {"queries": list(queries), "scopes": scopes}
        if "limit" in request:
            cleaned["limit"] = request["limit"]
        if generation is not None:
            cleaned["generation"] = generation
        cleaned["max_estimated_tokens"] = request["max_estimated_tokens"]
        return cleaned

    allowed = {"seed_item_id", "generation", "max_estimated_tokens"}
    if request.keys() - allowed:
        raise LedgerError("read request contains unknown fields")
    if not allowed <= request.keys():
        raise LedgerError("read request is missing a required field")
    seed = _string(request.get("seed_item_id"), "seed item ID", ITEM_PATTERN)
    generation = _string(request.get("generation"), "request generation", GENERATION_PATTERN)
    if ledger["generation"] is None or generation != ledger["generation"]:
        raise LedgerError("read request must use the pinned generation")
    candidate = ledger["candidate_items"].get(seed)
    if candidate is None:
        raise LedgerError("read seed must come from a recorded search response")
    cap = _integer(request["max_estimated_tokens"], "request token cap", 1)
    if any(
        call["operation"] == "read_bundle"
        and call["request"]["seed_item_id"] == seed
        for call in ledger["calls"]
    ):
        raise LedgerError("read seed has already been attempted")
    return {"seed_item_id": seed, "generation": generation, "max_estimated_tokens": cap}


def _summary(ledger: dict) -> dict:
    used = ledger["usage"]["estimated_evidence_tokens"]
    search_calls = sum(call["operation"] == "search" for call in ledger["calls"])
    read_calls = sum(call["operation"] == "read_bundle" for call in ledger["calls"])
    pending = [call["id"] for call in ledger["calls"] if call["status"] == "pending"]
    partial = sorted(key for key, value in ledger["bundles"].items() if (
        not value.get("membership_complete")
        or bool(set(value.get("missing_item_ids", ())) - set(value["returned_item_ids"]))
    ))
    return {
        "ledger_path": ledger["ledger_path"],
        "generation": ledger["generation"],
        "calls": {
            "search": search_calls,
            "read_bundle": read_calls,
            "pending": pending,
        },
        "usage": {
            "estimated_evidence_tokens": used,
            "remaining_soft_tokens": max(0, ledger["limits"]["evidence_tokens"] - used),
        },
        "recorded_bundles": len(ledger["bundles"]),
        "evidence_items": len(ledger["evidence_items"]),
        "partial_bundles": partial,
    }


def command_init(args: argparse.Namespace) -> dict:
    directory = Path(args.directory) if args.directory else Path(tempfile.gettempdir())
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix="learn-corpus-ledger-", suffix=".json", dir=directory)
    os.close(descriptor)
    path = Path(name).resolve()
    limits = {
        "search_calls": args.max_search_calls,
        "read_calls": args.max_read_calls,
        "evidence_tokens": args.max_evidence_tokens,
    }
    ledger = {
        "schema_version": SCHEMA_VERSION,
        "ledger_path": str(path),
        "created_at": _now(),
        "generation": None,
        "limits": limits,
        "usage": {"estimated_evidence_tokens": 0, "estimator_versions": []},
        "calls": [],
        "candidate_items": {},
        "bundles": {},
        "evidence_items": {},
    }
    _save(path, ledger)
    return _summary(ledger)


def command_begin(args: argparse.Namespace) -> dict:
    path = Path(args.ledger).resolve()
    ledger = _load(path)
    if any(call["status"] == "pending" for call in ledger["calls"]):
        raise LedgerError("complete or fail the pending call before beginning another")
    if args.operation not in OPERATIONS:
        raise LedgerError("operation must be search or read_bundle")
    request = _validate_request(args.operation, _read_json_input(args.json, args.input), ledger)
    cap = _integer(request.get("max_estimated_tokens", 8000), "request token cap", 1)
    if cap > 8000:
        raise LedgerError("request token cap exceeds the service maximum")
    remaining = ledger["limits"]["evidence_tokens"] - ledger["usage"]["estimated_evidence_tokens"]
    if cap > remaining:
        raise LedgerError("request token cap exceeds the remaining soft budget")
    count_key = "search_calls" if args.operation == "search" else "read_calls"
    attempts = sum(call["operation"] == args.operation for call in ledger["calls"])
    if attempts >= ledger["limits"][count_key]:
        raise LedgerError(f"{args.operation} call limit has been reached")
    call_id = f"call_{len(ledger['calls']) + 1:03d}"
    call = {
        "id": call_id,
        "operation": args.operation,
        "status": "pending",
        "started_at": _now(),
        "request": request,
    }
    if args.operation == "search":
        call["search_key"] = {
            "queries": sorted(_normalize_query(query) for query in request["queries"]),
            "scopes": request["scopes"],
        }
    ledger["calls"].append(call)
    _save(path, ledger)
    result = _summary(ledger)
    result["call_id"] = call_id
    result["request"] = request
    return result


def _minimal_metadata(item: dict, *, bundle: str | None = None) -> dict:
    item_id = _string(item.get("item_id"), "item ID", ITEM_PATTERN)
    value = {"item_id": item_id}
    if bundle is not None:
        value["bundle_key"] = bundle
    for key in ("source_type", "path", "locator", "role", "evidence_role", "turn_index"):
        if key in item:
            value[key] = item[key]
    return value


def _complete_search(ledger: dict, call: dict, response: dict) -> None:
    generation = _string(response.get("generation"), "response generation", GENERATION_PATTERN)
    if ledger["generation"] is None:
        ledger["generation"] = generation
    elif ledger["generation"] != generation:
        raise LedgerError("response generation does not match the pinned generation")
    results = response.get("results")
    if type(results) is not list:
        raise LedgerError("search response results must be an array")
    for item in results:
        if type(item) is not dict:
            raise LedgerError("search result must be an object")
        key = _string(item.get("bundle_key"), "bundle key", BUNDLE_PATTERN)
        metadata = _minimal_metadata(item, bundle=key)
        ledger["candidate_items"][metadata["item_id"]] = metadata
    call["result_count"] = len(results)
    call["is_truncated"] = response.get("is_truncated") is True


def _complete_read(ledger: dict, call: dict, response: dict) -> None:
    generation = _string(response.get("generation"), "response generation", GENERATION_PATTERN)
    if generation != ledger["generation"]:
        raise LedgerError("response generation does not match the pinned generation")
    key = _string(response.get("bundle_key"), "bundle key", BUNDLE_PATTERN)
    seed = call["request"]["seed_item_id"]
    if response.get("seed_item_id") != seed:
        raise LedgerError("read response seed does not match the request")
    candidate = ledger["candidate_items"][seed]
    if key != candidate["bundle_key"]:
        raise LedgerError("read response bundle does not match the seed candidate")
    status = response.get("bundle_status")
    if status not in {"complete", "partial_budget", "partial_error"}:
        raise LedgerError("read response bundle status is invalid")
    membership = response.get("membership_complete")
    if type(membership) is not bool:
        raise LedgerError("read response membership flag is invalid")
    missing = response.get("missing_item_ids")
    items = response.get("items")
    if type(missing) is not list or type(items) is not list:
        raise LedgerError("read response item arrays are invalid")
    if any(type(item_id) is not str or ITEM_PATTERN.fullmatch(item_id) is None for item_id in missing):
        raise LedgerError("read response contains an invalid missing item ID")
    returned = []
    for item in items:
        if type(item) is not dict:
            raise LedgerError("bundle item must be an object")
        metadata = _minimal_metadata(item)
        returned.append(metadata["item_id"])
        ledger["evidence_items"][metadata["item_id"]] = metadata
    if seed not in returned:
        raise LedgerError("read response does not contain the seed item")
    bundle = ledger["bundles"].setdefault(key, {
        "attempts": [],
        "returned_item_ids": [],
        "missing_item_ids": [],
    })
    bundle["attempts"].append({
        "call_id": call["id"], "status": "succeeded",
        "seed_item_id": seed,
        "requested_token_cap": call["request"]["max_estimated_tokens"],
    })
    bundle["bundle_status"] = status
    bundle["membership_complete"] = membership
    bundle["returned_item_ids"] = sorted(set(bundle["returned_item_ids"]) | set(returned))
    bundle["missing_item_ids"] = list(dict.fromkeys(missing))


def command_complete(args: argparse.Namespace) -> dict:
    path = Path(args.ledger).resolve()
    ledger = _load(path)
    call = next((item for item in ledger["calls"] if item["id"] == args.call_id), None)
    if call is None:
        raise LedgerError("call ID does not exist")
    response = _read_json_input(args.json, args.input)
    if type(response) is not dict:
        raise LedgerError("response must be a JSON object")
    encoded = json.dumps(response, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    if call["status"] != "pending":
        if call.get("response_sha256") == digest:
            return _summary(ledger)
        raise LedgerError("completed call cannot be changed")
    tokens, estimator = _usage(response)
    projected = ledger["usage"]["estimated_evidence_tokens"] + tokens
    if projected > ledger["limits"]["evidence_tokens"]:
        raise LedgerError("response usage exceeds the task soft budget")
    if call["operation"] == "search":
        _complete_search(ledger, call, response)
    else:
        _complete_read(ledger, call, response)
    call["status"] = "succeeded"
    call["completed_at"] = _now()
    call["response_sha256"] = digest
    call["request_id"] = response.get("request_id") if type(response.get("request_id")) is str else None
    call["usage"] = {"estimated_evidence_tokens": tokens, "estimator_version": estimator}
    ledger["usage"]["estimated_evidence_tokens"] = projected
    versions = ledger["usage"]["estimator_versions"]
    if estimator not in versions:
        versions.append(estimator)
    _save(path, ledger)
    return _summary(ledger)


def command_fail(args: argparse.Namespace) -> dict:
    path = Path(args.ledger).resolve()
    ledger = _load(path)
    call = next((item for item in ledger["calls"] if item["id"] == args.call_id), None)
    if call is None:
        raise LedgerError("call ID does not exist")
    if call["status"] != "pending":
        if call["status"] == "failed" and call.get("error_category") == args.category:
            return _summary(ledger)
        raise LedgerError("completed call cannot be changed")
    if args.category not in ERROR_CATEGORIES:
        raise LedgerError("error category is invalid")
    call["status"] = "failed"
    call["completed_at"] = _now()
    call["error_category"] = args.category
    if call["operation"] == "read_bundle":
        seed = call["request"]["seed_item_id"]
        key = ledger["candidate_items"][seed]["bundle_key"]
        bundle = ledger["bundles"].setdefault(key, {
            "attempts": [], "returned_item_ids": [], "missing_item_ids": [],
        })
        bundle["attempts"].append({
            "call_id": call["id"], "status": "failed",
            "seed_item_id": seed,
            "requested_token_cap": call["request"]["max_estimated_tokens"],
            "error_category": args.category,
        })
    _save(path, ledger)
    return _summary(ledger)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Maintain a temporary Learn Corpus execution ledger.")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create one temporary task ledger.")
    init.add_argument("--directory")
    init.add_argument("--max-search-calls", type=int, default=DEFAULT_LIMITS["search_calls"])
    init.add_argument("--max-read-calls", type=int, default=DEFAULT_LIMITS["read_calls"])
    init.add_argument("--max-evidence-tokens", type=int, default=DEFAULT_LIMITS["evidence_tokens"])
    init.set_defaults(handler=command_init)

    begin = commands.add_parser("begin", help="Validate and record one pending tool call.")
    begin.add_argument("ledger")
    begin.add_argument("operation", choices=sorted(OPERATIONS))
    begin.add_argument("--json", help="Inline request JSON; prefer --input for large values.")
    begin.add_argument("--input", help="Read request JSON from this file instead of stdin.")
    begin.set_defaults(handler=command_begin)

    complete = commands.add_parser("complete", help="Record a successful structuredContent response.")
    complete.add_argument("ledger")
    complete.add_argument("call_id")
    complete.add_argument("--json", help="Inline response JSON; prefer --input for large values.")
    complete.add_argument("--input", help="Read response JSON from this file instead of stdin.")
    complete.set_defaults(handler=command_complete)

    fail = commands.add_parser("fail", help="Record a failed tool call without raw error text.")
    fail.add_argument("ledger")
    fail.add_argument("call_id")
    fail.add_argument("category", choices=sorted(ERROR_CATEGORIES))
    fail.set_defaults(handler=command_fail)

    show = commands.add_parser("show", help="Print a compact task summary.")
    show.add_argument("ledger")
    show.set_defaults(handler=lambda args: _summary(_load(Path(args.ledger).resolve())))
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    for name in ("max_search_calls", "max_read_calls", "max_evidence_tokens"):
        if hasattr(args, name) and getattr(args, name) < 1:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    if getattr(args, "json", None) is not None and getattr(args, "input", None) is not None:
        parser.error("--json and --input are mutually exclusive")
    try:
        result = args.handler(args)
    except (LedgerError, OSError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
