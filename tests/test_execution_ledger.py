from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/learn-corpus-retrieval/scripts/execution_ledger.py"
GENERATION = "gen_" + "1" * 20
ITEM_A = "itm_" + "a" * 32
ITEM_B = "itm_" + "b" * 32
BUNDLE = "bnd_" + "1" * 40


def run(*args: str, value: object | None = None, ok: bool = True):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *args], cwd=ROOT,
        input=None if value is None else json.dumps(value, ensure_ascii=False),
        text=True, capture_output=True,
    )
    assert (result.returncode == 0) is ok, result.stderr
    return json.loads(result.stdout) if ok else result.stderr


def new_ledger(tmp_path: Path, **limits: int) -> str:
    args = ["init", "--directory", str(tmp_path)]
    for name, value in limits.items():
        args.extend(["--" + name.replace("_", "-"), str(value)])
    return run(*args)["ledger_path"]


def search_response(*, usage: int = 300, generation: str = GENERATION,
                    item_ids: tuple[str, ...] = (ITEM_A,)) -> dict:
    return {
        "request_id": "req_synthetic",
        "generation": generation,
        "is_truncated": False,
        "results": [{
            "item_id": item_id,
            "bundle_key": BUNDLE,
            "source_type": "conversation",
            "title": "Sensitive synthetic title",
            "path": "sources/conversations/synthetic.md",
            "locator": "synthetic/turn:1/user",
            "role": "human" if item_id == ITEM_A else "assistant",
            "evidence_role": "user_statement" if item_id == ITEM_A else "assistant_suggestion",
            "turn_index": 1,
            "snippet": "Synthetic secret snippet",
            "truncated_before": False,
            "truncated_after": False,
        } for item_id in item_ids],
        "usage": {"estimated_evidence_tokens": usage, "estimator_version": "synthetic-v1"},
    }


def read_response(*, status: str, usage: int, items: list[dict], missing: list[str],
                  seed_item_id: str = ITEM_A) -> dict:
    return {
        "request_id": "req_synthetic_read",
        "generation": GENERATION,
        "seed_item_id": seed_item_id,
        "bundle_key": BUNDLE,
        "bundle_status": status,
        "membership_complete": True,
        "missing_item_ids": missing,
        "items": items,
        "usage": {"estimated_evidence_tokens": usage, "estimator_version": "synthetic-v1"},
    }


def bundle_item(item_id: str, role: str) -> dict:
    return {
        "item_id": item_id,
        "source_type": "conversation",
        "title": "Sensitive synthetic title",
        "path": "sources/conversations/synthetic.md",
        "locator": f"synthetic/{role}",
        "role": role,
        "evidence_role": "user_statement" if role == "human" else "assistant_suggestion",
        "turn_index": 1,
        "body": "Synthetic private body",
        "is_truncated": False,
        "relations": {"counterpart_item_ids": [], "previous_part_id": None, "next_part_id": None},
    }


def record_search(ledger: str, *, cap: int = 1000, usage: int = 300,
                  item_ids: tuple[str, ...] = (ITEM_A,)) -> None:
    pending = run(
        "begin", ledger, "search",
        value={"queries": ["Synthetic Anchor"], "scopes": ["conversation"],
               "limit": 5, "max_estimated_tokens": cap},
    )
    run("complete", ledger, pending["call_id"],
        value=search_response(usage=usage, item_ids=item_ids))


def test_records_counts_usage_and_discards_evidence_text(tmp_path: Path) -> None:
    ledger = new_ledger(tmp_path)
    assert Path(ledger).stat().st_mode & 0o777 == 0o600
    record_search(ledger)
    pending = run(
        "begin", ledger, "read_bundle",
        value={"seed_item_id": ITEM_A, "generation": GENERATION, "max_estimated_tokens": 2000},
    )
    summary = run(
        "complete", ledger, pending["call_id"],
        value=read_response(status="complete", usage=700,
                            items=[bundle_item(ITEM_A, "human"), bundle_item(ITEM_B, "assistant")],
                            missing=[]),
    )
    assert summary["calls"] == {"search": 1, "read_bundle": 1, "pending": []}
    assert summary["usage"] == {"estimated_evidence_tokens": 1000, "remaining_soft_tokens": 7000}
    assert summary["evidence_items"] == 2 and summary["partial_bundles"] == []
    raw = Path(ledger).read_text()
    assert "Synthetic secret snippet" not in raw
    assert "Synthetic private body" not in raw
    assert "Sensitive synthetic title" not in raw
    assert "sources/conversations/synthetic.md" in raw


def test_failed_call_counts_and_pending_blocks_next_call(tmp_path: Path) -> None:
    ledger = new_ledger(tmp_path, max_search_calls=2)
    pending = run("begin", ledger, "search", value={"queries": ["alpha"], "max_estimated_tokens": 500})
    error = run(
        "begin", ledger, "search",
        value={"queries": ["beta"], "max_estimated_tokens": 500}, ok=False,
    )
    assert "pending call" in error
    summary = run("fail", ledger, pending["call_id"], "service_error")
    assert summary["calls"]["search"] == 1 and summary["usage"]["estimated_evidence_tokens"] == 0
    run("begin", ledger, "search", value={"queries": ["beta"], "max_estimated_tokens": 500})
    error = run("begin", ledger, "search", value={"queries": ["gamma"]}, ok=False)
    assert "pending call" in error


def test_generation_deduplication_and_soft_budget(tmp_path: Path) -> None:
    ledger = new_ledger(tmp_path, max_evidence_tokens=1000)
    record_search(ledger, cap=600, usage=400)
    duplicate = run(
        "begin", ledger, "search",
        value={"queries": ["  synthetic   anchor "], "scopes": ["conversation"],
               "generation": GENERATION, "max_estimated_tokens": 500}, ok=False,
    )
    assert "duplicate search" in duplicate
    mismatch = run(
        "begin", ledger, "search",
        value={"queries": ["different"], "generation": "gen_" + "2" * 20,
               "max_estimated_tokens": 500}, ok=False,
    )
    assert "pinned generation" in mismatch
    over_budget = run(
        "begin", ledger, "search",
        value={"queries": ["different"], "generation": GENERATION,
               "max_estimated_tokens": 601}, ok=False,
    )
    assert "remaining soft budget" in over_budget


def test_requires_explicit_per_request_token_cap(tmp_path: Path) -> None:
    ledger = new_ledger(tmp_path)
    search = run("begin", ledger, "search", value={"queries": ["alpha"]}, ok=False)
    assert "missing a required field" in search


def test_partial_window_allows_an_unreturned_candidate_seed(tmp_path: Path) -> None:
    ledger = new_ledger(tmp_path)
    record_search(ledger, usage=200, item_ids=(ITEM_A, ITEM_B))
    first = run(
        "begin", ledger, "read_bundle",
        value={"seed_item_id": ITEM_A, "generation": GENERATION, "max_estimated_tokens": 800},
    )
    run(
        "complete", ledger, first["call_id"],
        value=read_response(status="partial_budget", usage=600,
                            items=[bundle_item(ITEM_A, "human")], missing=[ITEM_B]),
    )
    repeated = run(
        "begin", ledger, "read_bundle",
        value={"seed_item_id": ITEM_A, "generation": GENERATION, "max_estimated_tokens": 800},
        ok=False,
    )
    assert "already been attempted" in repeated
    second = run(
        "begin", ledger, "read_bundle",
        value={"seed_item_id": ITEM_B, "generation": GENERATION, "max_estimated_tokens": 800},
    )
    summary = run(
        "complete", ledger, second["call_id"],
        value=read_response(status="partial_budget", usage=600,
                            items=[bundle_item(ITEM_B, "assistant")], missing=[ITEM_A],
                            seed_item_id=ITEM_B),
    )
    assert summary["partial_bundles"] == [] and summary["evidence_items"] == 2


def test_failed_second_window_preserves_evidence_and_cannot_repeat_seed(tmp_path: Path) -> None:
    ledger = new_ledger(tmp_path)
    record_search(ledger, usage=200, item_ids=(ITEM_A, ITEM_B))
    first = run(
        "begin", ledger, "read_bundle",
        value={"seed_item_id": ITEM_A, "generation": GENERATION, "max_estimated_tokens": 800},
    )
    run(
        "complete", ledger, first["call_id"],
        value=read_response(status="partial_budget", usage=600,
                            items=[bundle_item(ITEM_A, "human")], missing=[ITEM_B]),
    )
    second = run(
        "begin", ledger, "read_bundle",
        value={"seed_item_id": ITEM_B, "generation": GENERATION, "max_estimated_tokens": 800},
    )
    summary = run("fail", ledger, second["call_id"], "service_error")
    assert summary["evidence_items"] == 1 and summary["partial_bundles"] == [BUNDLE]
    retry = run(
        "begin", ledger, "read_bundle",
        value={"seed_item_id": ITEM_B, "generation": GENERATION, "max_estimated_tokens": 800},
        ok=False,
    )
    assert "already been attempted" in retry


def test_completion_is_idempotent_but_cannot_be_replaced(tmp_path: Path) -> None:
    ledger = new_ledger(tmp_path)
    pending = run("begin", ledger, "search", value={"queries": ["alpha"], "max_estimated_tokens": 500})
    response = search_response(usage=200)
    first = run("complete", ledger, pending["call_id"], value=response)
    second = run("complete", ledger, pending["call_id"], value=response)
    assert second == first
    changed = search_response(usage=201)
    error = run("complete", ledger, pending["call_id"], value=changed, ok=False)
    assert "cannot be changed" in error
