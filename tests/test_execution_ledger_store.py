from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.service.execution_ledger_store import _DDL, ExecutionLedgerStore, LedgerFailure
from src.service.ledger_config import LedgerConfig, TaskLimitsConfig


GENERATION = "gen_" + "1" * 20
ITEM_A = "itm_" + "a" * 32
ITEM_B = "itm_" + "b" * 32
BUNDLE = "bnd_" + "1" * 40


@pytest.fixture
def ledger_config(tmp_path):
    directory = tmp_path / "ledger"
    directory.mkdir(mode=0o700)
    return LedgerConfig(directory / "execution.sqlite3", max_tasks=20, max_mb=8)


@pytest.fixture
def store(ledger_config):
    return ExecutionLedgerStore.open(ledger_config)


def item(item_id=ITEM_A, role="human"):
    return {
        "item_id": item_id,
        "source_type": "conversation",
        "title": "Synthetic title must not be stored",
        "source_title": None,
        "heading_path": None,
        "path": "sources/conversations/synthetic.md",
        "locator": f"synthetic/turn:1/{role}",
        "role": role,
        "evidence_role": "user_statement" if role == "human" else "assistant_suggestion",
        "turn_index": 1,
    }


def search_response(call_id, *, usage=300, items=(ITEM_A,)):
    return {
        "request_id": call_id,
        "generation": GENERATION,
        "is_truncated": False,
        "results": [{**item(value, "human" if value == ITEM_A else "assistant"),
                     "bundle_key": BUNDLE, "rank": rank,
                     "snippet": "Synthetic snippet must not be stored",
                     "truncated_before": False, "truncated_after": False}
                    for rank, value in enumerate(items, start=1)],
        "usage": {"estimated_evidence_tokens": usage,
                  "estimator_version": "synthetic-estimator-v1"},
    }


def read_response(call_id, *, usage=500, seed=ITEM_A, items=(ITEM_A,), missing=()):
    return {
        "request_id": call_id,
        "generation": GENERATION,
        "seed_item_id": seed,
        "bundle_key": BUNDLE,
        "bundle_status": "partial_budget" if missing else "complete",
        "membership_complete": True,
        "missing_item_ids": list(missing),
        "items": [{**item(value, "human" if value == ITEM_A else "assistant"),
                   "body": "Synthetic body must not be stored", "is_truncated": False,
                   "relations": {"counterpart_item_ids": [], "previous_part_id": None,
                                 "next_part_id": None}}
                  for value in items],
        "usage": {"estimated_evidence_tokens": usage,
                  "estimator_version": "synthetic-estimator-v1"},
    }


def admit_search(store, task_id, call_id="req_synthetic_search", cap=1000):
    return store.admit_search(
        "synthetic_owner", task_id, call_id,
        queries=("Synthetic Anchor",), query_keys=("synthetic anchor",),
        scopes=("conversation",), result_limit=8, cap=cap,
        current_generation=GENERATION,
    )


def test_schema_permissions_and_unknown_version_fail_closed(ledger_config):
    store = ExecutionLedgerStore.open(ledger_config)
    assert ledger_config.path.stat().st_mode & 0o777 == 0o600
    with store._connect() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    ledger_config.path.unlink()
    connection = sqlite3.connect(ledger_config.path)
    connection.execute("PRAGMA user_version = 99")
    connection.close()
    ledger_config.path.chmod(0o600)
    with pytest.raises(ValueError, match="schema version"):
        ExecutionLedgerStore.open(ledger_config)


def test_schema_v1_is_migrated_without_rebuilding_tasks(ledger_config):
    connection = sqlite3.connect(ledger_config.path)
    legacy = _DDL.replace(
        "    source_title TEXT,\n    heading_path_json TEXT,\n", "",
    )
    for statement in legacy.split(";"):
        if statement.strip():
            connection.execute(statement)
    connection.execute("PRAGMA user_version = 1")
    connection.commit()
    connection.close()
    ledger_config.path.chmod(0o600)

    migrated = ExecutionLedgerStore.open(ledger_config)
    with migrated._connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        columns = {row[1] for row in connection.execute("PRAGMA table_info(task_items)")}
    assert {"source_title", "heading_path_json"} <= columns


def test_note_source_title_and_heading_path_are_retained_for_citations(store):
    task_id = store.create_task("synthetic_owner")["task_id"]
    call_id = "req_note_search"
    store.admit_search(
        "synthetic_owner", task_id, call_id,
        queries=("Synthetic",), query_keys=("synthetic",), scopes=("note",),
        result_limit=8, cap=1000, current_generation=GENERATION,
    )
    note = {
        "item_id": ITEM_A,
        "source_type": "note",
        "title": "Synthetic section",
        "source_title": "Synthetic source",
        "heading_path": ["Parent", "Synthetic section"],
        "path": "sources/notes/synthetic.md",
        "locator": "note:synthetic/heading:parent/synthetic-section/part:1",
        "role": None,
        "evidence_role": None,
        "turn_index": None,
    }
    response = search_response(call_id)
    response["results"] = [{
        **note, "bundle_key": BUNDLE, "rank": 1, "snippet": "Synthetic snippet",
        "truncated_before": False, "truncated_after": False,
    }]
    store.finalize_success(call_id, response)
    read_id = "req_note_read"
    store.admit_read(
        "synthetic_owner", task_id, read_id, seed_item_id=ITEM_A,
        cap=1000, current_generation=GENERATION,
    )
    read = read_response(read_id)
    read["items"] = [{
        **note, "body": "Synthetic body", "is_truncated": False,
        "relations": {"counterpart_item_ids": [], "previous_part_id": None,
                      "next_part_id": None},
    }]
    store.finalize_success(read_id, read)

    citation = store.get_task("synthetic_owner", task_id)["server_returned_items"][0]
    assert citation["source_title"] == "Synthetic source"
    assert citation["heading_path"] == ["Parent", "Synthetic section"]
    assert citation["locator"] == note["locator"]


def test_successful_calls_derive_usage_and_store_no_evidence_text(store, ledger_config):
    task = store.create_task("synthetic_owner")
    task_id = task["task_id"]
    search_id = "req_synthetic_search"
    admit_search(store, task_id, search_id)
    summary = store.finalize_success(search_id, search_response(search_id, items=(ITEM_A, ITEM_B)))
    assert summary["generation"] == GENERATION
    assert summary["search_calls"] == 1 and summary["estimated_evidence_tokens"] == 300

    read_id = "req_synthetic_read"
    admission = store.admit_read(
        "synthetic_owner", task_id, read_id, seed_item_id=ITEM_A,
        cap=1000, current_generation=GENERATION,
    )
    assert admission == {"generation": GENERATION, "bundle_key": BUNDLE}
    summary = store.finalize_success(
        read_id, read_response(read_id, items=(ITEM_A, ITEM_B)),
    )
    assert summary["read_calls"] == 1
    assert summary["estimated_evidence_tokens"] == 800
    assert summary["reserved_estimated_tokens"] == 0
    detail = store.get_task("synthetic_owner", task_id)
    assert len(detail["calls"]) == 2
    assert {value["item_id"] for value in detail["server_returned_items"]} == {ITEM_A, ITEM_B}
    raw = ledger_config.path.read_bytes()
    assert b"Synthetic body" not in raw
    assert b"Synthetic snippet" not in raw
    assert b"Synthetic title" not in raw
    assert b"sources/conversations/synthetic.md" in raw


def test_task_detail_reports_complete_counts_before_response_truncation(store):
    task_id = store.create_task("synthetic_owner")["task_id"]
    admit_search(store, task_id)
    store.finalize_success("req_synthetic_search", search_response("req_synthetic_search"))
    store.admit_read(
        "synthetic_owner", task_id, "req_many_items", seed_item_id=ITEM_A,
        cap=7000, current_generation=GENERATION,
    )
    alphabet = "bcdefghijklmnopqrstuv"
    item_ids = (ITEM_A, *("itm_" + "a" * 31 + value for value in alphabet))
    store.finalize_success(
        "req_many_items", read_response("req_many_items", usage=6000, items=item_ids),
    )
    detail = store.get_task("synthetic_owner", task_id)
    assert len(detail["server_returned_items"]) == 22
    assert detail["items_total"] == 22
    assert detail["items_truncated"] is False
    assert detail["calls_total"] == 2
    assert detail["calls_truncated"] is False


def test_task_limits_are_snapshotted_and_enforced(store):
    limits = TaskLimitsConfig(
        search_calls=1, read_calls=2, estimated_evidence_tokens=500,
    )
    task = store.create_task("synthetic_owner", limits)
    assert task["limits"] == {
        "search_calls": 1, "read_calls": 2, "estimated_evidence_tokens": 500,
    }
    task_id = task["task_id"]
    admit_search(store, task_id, cap=300)
    store.finalize_success(
        "req_synthetic_search", search_response("req_synthetic_search", usage=300),
    )
    with pytest.raises(LedgerFailure) as failure:
        store.admit_search(
            "synthetic_owner", task_id, "req_over_call_limit",
            queries=("Other",), query_keys=("other",), scopes=("note",),
            result_limit=8, cap=100, current_generation=GENERATION,
        )
    assert failure.value.code == "task_call_limit_exceeded"
    assert failure.value.details == {"operation": "search", "used": 1, "limit": 1}
    with pytest.raises(LedgerFailure) as failure:
        store.admit_read(
            "synthetic_owner", task_id, "req_over_task_budget", seed_item_id=ITEM_A,
            cap=201, current_generation=GENERATION,
        )
    assert failure.value.code == "task_budget_exceeded"
    assert failure.value.details == {
        "requested_estimated_tokens": 201,
        "available_estimated_tokens": 200,
        "task_limit": 500,
    }
    assert store.get_task("synthetic_owner", task_id)["limits"] == task["limits"]


def test_owner_duplicate_pending_seed_and_budget_rules(store):
    task_id = store.create_task("synthetic_owner")["task_id"]
    with pytest.raises(LedgerFailure) as failure:
        store.get_task("other_owner", task_id)
    assert failure.value.code == "task_not_found"

    admit_search(store, task_id)
    with pytest.raises(LedgerFailure) as failure:
        store.admit_search(
            "synthetic_owner", task_id, "req_second", queries=("Other",),
            query_keys=("other",), scopes=("conversation",), result_limit=8,
            cap=1000, current_generation=GENERATION,
        )
    assert failure.value.code == "task_busy"
    store.finalize_success("req_synthetic_search", search_response("req_synthetic_search"))
    with pytest.raises(LedgerFailure) as failure:
        admit_search(store, task_id, "req_duplicate")
    assert failure.value.code == "search_already_attempted"

    store.admit_read(
        "synthetic_owner", task_id, "req_read", seed_item_id=ITEM_A,
        cap=1000, current_generation=GENERATION,
    )
    store.finalize_failure("req_read", "budget_exceeded")
    with pytest.raises(LedgerFailure) as failure:
        store.admit_read(
            "synthetic_owner", task_id, "req_read_again", seed_item_id=ITEM_A,
            cap=1000, current_generation=GENERATION,
        )
    assert failure.value.code == "seed_already_attempted"
    with pytest.raises(LedgerFailure) as failure:
        store.admit_search(
            "synthetic_owner", task_id, "req_over_budget", queries=("Different",),
            query_keys=("different",), scopes=("conversation",), result_limit=8,
            cap=7701, current_generation=GENERATION,
        )
    assert failure.value.code == "task_budget_exceeded"


def test_same_task_concurrency_allows_only_one_pending(store):
    task_id = store.create_task("synthetic_owner")["task_id"]

    def attempt(index):
        try:
            store.admit_search(
                "synthetic_owner", task_id, f"req_parallel_{index}",
                queries=(f"Synthetic {index}",), query_keys=(f"synthetic {index}",),
                scopes=("note",), result_limit=8, cap=500,
                current_generation=GENERATION,
            )
            return "ok"
        except LedgerFailure as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert sorted(results) == ["ok", "task_busy"]


def test_second_store_process_can_use_existing_task(ledger_config):
    first = ExecutionLedgerStore.open(ledger_config)
    second = ExecutionLedgerStore.open(ledger_config)
    task_id = first.create_task("synthetic_owner")["task_id"]
    admit_search(second, task_id)
    summary = first.finalize_success(
        "req_synthetic_search", search_response("req_synthetic_search"),
    )
    assert summary["search_calls"] == 1 and summary["generation"] == GENERATION


def test_uncertain_preserves_reservation_and_blocks_task(store):
    task_id = store.create_task("synthetic_owner")["task_id"]
    admit_search(store, task_id, cap=700)
    store.mark_uncertain("req_synthetic_search", "worker_interrupted")
    detail = store.get_task("synthetic_owner", task_id)
    assert detail["execution"]["reserved_estimated_tokens"] == 700
    assert detail["execution"]["unresolved_calls"] == 1
    with pytest.raises(LedgerFailure) as failure:
        store.admit_search(
            "synthetic_owner", task_id, "req_after_uncertain", queries=("Other",),
            query_keys=("other",), scopes=("note",), result_limit=8, cap=100,
            current_generation=GENERATION,
        )
    assert failure.value.code == "task_busy"


def test_metadata_conflict_records_usage_and_blocks(store):
    task_id = store.create_task("synthetic_owner")["task_id"]
    admit_search(store, task_id)
    store.finalize_success("req_synthetic_search", search_response("req_synthetic_search"))
    store.admit_read(
        "synthetic_owner", task_id, "req_conflict", seed_item_id=ITEM_A,
        cap=1000, current_generation=GENERATION,
    )
    response = read_response("req_conflict")
    response["items"][0]["locator"] = "synthetic/conflicting-locator"
    with pytest.raises(LedgerFailure) as failure:
        store.finalize_success("req_conflict", response)
    assert failure.value.code == "task_blocked"
    assert failure.value.details == {
        "blocked_category": "ledger_integrity_error", "retryable": False,
    }
    detail = store.get_task("synthetic_owner", task_id)
    assert detail["task_state"] == "blocked"
    assert detail["execution"]["estimated_evidence_tokens"] == 800
    assert detail["calls"][-1]["error_category"] == "ledger_integrity_error"


def test_empty_search_pins_generation_and_generation_change_blocks(store):
    task_id = store.create_task("synthetic_owner")["task_id"]
    admit_search(store, task_id)
    store.finalize_success(
        "req_synthetic_search", search_response("req_synthetic_search", items=()),
    )
    detail = store.get_task("synthetic_owner", task_id)
    assert detail["generation"] == GENERATION

    with pytest.raises(LedgerFailure) as failure:
        store.admit_search(
            "synthetic_owner", task_id, "req_new_generation",
            queries=("Other",), query_keys=("other",), scopes=("note",),
            result_limit=8, cap=500, current_generation="gen_" + "2" * 20,
        )
    assert failure.value.code == "task_blocked"
    assert failure.value.details == {
        "blocked_category": "generation_mismatch", "retryable": False,
    }
    detail = store.get_task("synthetic_owner", task_id)
    assert detail["task_state"] == "blocked"
    assert detail["generation"] == GENERATION
    assert detail["execution"]["search_calls"] == 1


def test_settlement_is_not_overwritten(store):
    task_id = store.create_task("synthetic_owner")["task_id"]
    admit_search(store, task_id)
    store.finalize_success("req_synthetic_search", search_response("req_synthetic_search"))
    changed = search_response("req_synthetic_search", usage=900, items=())
    with pytest.raises(LedgerFailure) as failure:
        store.finalize_success("req_synthetic_search", changed)
    assert failure.value.code == "ledger_unavailable"
    detail = store.get_task("synthetic_owner", task_id)
    assert detail["task_state"] == "active"
    assert detail["execution"]["estimated_evidence_tokens"] == 300
    assert detail["calls"][0]["state"] == "succeeded"


def test_capacity_and_cross_task_foreign_keys(ledger_config):
    limited = ExecutionLedgerStore.open(
        LedgerConfig(ledger_config.path, max_tasks=1, max_mb=8),
    )
    first = limited.create_task("synthetic_owner")["task_id"]
    with pytest.raises(LedgerFailure) as failure:
        limited.create_task("synthetic_owner")
    assert failure.value.code == "ledger_capacity_exceeded"

    # 复合外键拒绝把一个任务的调用关联到另一个任务的 item。
    other_path = ledger_config.path.parent / "other.sqlite3"
    other = ExecutionLedgerStore.open(LedgerConfig(other_path, max_tasks=2, max_mb=8))
    task_a = other.create_task("synthetic_owner")["task_id"]
    task_b = other.create_task("synthetic_owner")["task_id"]
    admit_search(other, task_a)
    other.finalize_success("req_synthetic_search", search_response("req_synthetic_search"))
    with other._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO calls (call_id, task_id, sequence_number, operation, cap, state, started_at) "
            "VALUES ('req_other_task', ?, 1, 'search', 1, 'succeeded', 'synthetic')",
            (task_b,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO call_items VALUES ('req_other_task', ?, ?)", (ITEM_A, task_a),
            )
        connection.rollback()
