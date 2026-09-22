from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import replace
from time import monotonic

import pytest

from src.retrieval.build_lexical_index import build_generation, publish_generation
from src.retrieval.contracts import Item, ItemRelations, ProjectionResult, SourceSnapshot
from src.retrieval.lexical_store import (
    BudgetExceededError, GenerationMismatchError, IndexUnavailableError,
    InvalidRequestError, ItemNotFoundError, LexicalStore,
)
from src.retrieval.project_items import stable_item_id
from src.retrieval import public_core
from src.retrieval.public_core import RequestLimits, RetrievalCore
from src.retrieval.text import estimate_evidence_tokens


def parts(scope, unit, bodies, *, role=None, occurrence=1, turn=1):
    path = f"sources/{scope}s/synthetic-archive.md"
    locator = (
        f"synthetic-session/turn:{turn}/{role}" if scope == "conversation"
        else f"{scope}:synthetic-archive/heading:{unit}:occurrence:{occurrence}"
    )
    ids = [stable_item_id(path, locator, role or "heading", n) for n in range(1, len(bodies) + 1)]
    return tuple(Item(
        item_id=ids[index], scope=scope, title="Synthetic heading" if role is None else None,
        source_title="Synthetic source" if role is None else "Codex 会话 · 2025-01-02",
        source_path=path, source_id="synthetic-archive", locator=f"{locator}/part:{index + 1}",
        locator_with_lines=None,
        evidence_role=("user_statement" if role == "human" else "assistant_suggestion")
        if role else ("external_source" if scope == "article" else None),
        provider="synthetic" if role else None, session_id="synthetic-session" if role else None,
        turn_index=turn if role else None, role=role,
        heading_path=(unit,) if role is None else None, occurrence=occurrence if role is None else None,
        part=index + 1, body=body, body_sha256=hashlib.sha256(body.encode()).hexdigest(),
        token_estimate=estimate_evidence_tokens(body),
        relations=ItemRelations((), ids[index - 1] if index else None,
                                ids[index + 1] if index + 1 < len(ids) else None),
    ) for index, body in enumerate(bodies))


def exchange():
    human = parts("conversation", "unit", ("quasar question", "quasar clarification"), role="human")
    assistant = parts("conversation", "unit", ("quasar answer", "quasar detail"), role="assistant")
    return tuple(replace(item, relations=replace(
        item.relations,
        counterpart_item_ids=tuple(other.item_id for other in (assistant if item.role == "human" else human)),
    )) for item in (*human, *assistant))


@pytest.fixture
def open_core(tmp_path):
    stores = []

    def open_items(items, *, timeout_ms=10_000):
        root = tmp_path / str(len(stores))
        sources = {item.source_path: SourceSnapshot(
            item.source_path, "a" * 64, item.source_id, item.scope, 1,
        ) for item in items}
        projection = ProjectionResult(tuple(items), tuple(sources.values()), "sha256:" + "a" * 64)
        generation = build_generation(projection, root)
        publish_generation(root, generation.generation)
        store = LexicalStore.open_current(root)
        stores.append(store)
        return RetrievalCore(store, RequestLimits(timeout_ms))

    yield open_items
    for store in stores:
        store.close()


def read(core, item, **kwargs):
    return core.read_bundle({
        "seed_item_id": item.item_id, "generation": core.store.generation, **kwargs,
    }, request_id="req_synthetic")


def test_exchange_all_seeds_share_search_key_and_read_complete_roles(open_core):
    items = exchange()
    core = open_core(items)
    search = core.search({"queries": ["quasar"], "limit": 20}).payload
    assert len(search["results"]) == 4
    keys = {hit["bundle_key"] for hit in search["results"]}
    assert len(keys) == 1
    for seed in items:
        response = read(core, seed).payload
        assert response["bundle_key"] in keys
        assert response["bundle_status"] == "complete"
        assert response["membership_complete"] is True
        assert response["missing_item_ids"] == []
        assert [item["item_id"] for item in response["items"]] == [item.item_id for item in items]
        assert [item["body"] for item in response["items"]] == [item.body for item in items]
        assert [item["role"] for item in response["items"]] == ["human", "human", "assistant", "assistant"]
        assert all(item["is_truncated"] is False for item in response["items"])


@pytest.mark.parametrize("scope", ["note", "article"])
def test_section_parts_do_not_include_children_neighbors_or_repeated_headings(open_core, scope):
    section = parts(scope, "A", ("# A\nquasar direct", "quasar continuation"))
    child = parts(scope, "A/B", ("## B\nquasar child",))
    repeated = parts(scope, "A", ("# A\nquasar repeated",), occurrence=2)
    core = open_core((*section, *child, *repeated))
    result = read(core, section[1]).payload
    assert [item["item_id"] for item in result["items"]] == [item.item_id for item in section]
    assert all(item["source_title"] == "Synthetic source" for item in result["items"])
    assert all(item["heading_path"] == ["A"] for item in result["items"])
    search = core.search({"queries": ["quasar"], "scopes": [scope]}).payload
    assert search["results"][0]["source_title"] == "Synthetic source"
    assert search["results"][0]["heading_path"] == ["A"]
    assert len({read(core, seed).payload["bundle_key"] for seed in (section[0], child[0], repeated[0])}) == 3
    assert len(read(core, child[0]).payload["items"]) == 1


def test_public_whitelist_and_exact_serialized_usage(open_core):
    items = exchange()
    core = open_core(items)
    for response in (read(core, items[0]), core.search({"queries": ["quasar"]})):
        payload = response.payload
        assert payload["usage"]["estimated_evidence_tokens"] == estimate_evidence_tokens(
            response.json_bytes.decode("utf-8")
        )
        assert payload["usage"]["estimated_evidence_tokens"] > sum(
            estimate_evidence_tokens(item.get("body", item.get("snippet", "")))
            for item in payload.get("items", payload.get("results", []))
        )
        assert len(response.json_bytes) <= public_core.MAX_RESPONSE_BYTES
        for item in payload.get("items", payload.get("results", [])):
            assert not {"provider", "session_id", "part", "source_id", "body_sha256"} & item.keys()
            assert item["source_title"] == "Codex 会话 · 2025-01-02"
            assert item["heading_path"] is None
        payload["generation"] = "changed-local-copy"
        assert response.payload["generation"] == core.store.generation


@pytest.mark.parametrize("constraint", ["tokens", "bytes"])
def test_bundle_budget_centers_on_seed_and_preserves_source_order(open_core, monkeypatch, constraint):
    items = parts("note", "A", tuple("# A\n" + "quasar " * 160 for _ in range(5)))
    core = open_core(items)
    seed = items[2]
    full = read(core, seed)
    if constraint == "tokens":
        partial = read(core, seed, max_estimated_tokens=full.payload["usage"]["estimated_evidence_tokens"] - 300)
    else:
        monkeypatch.setattr(public_core, "MAX_RESPONSE_BYTES", len(full.json_bytes) - 900)
        partial = read(core, seed)
    payload = partial.payload
    count = len(payload["items"])
    assert 1 < count < len(items)
    assert payload["seed_item_id"] == seed.item_id
    assert payload["bundle_status"] == "partial_budget"
    assert payload["membership_complete"] is True
    priority = (items[2], items[3], items[1], items[4], items[0])
    selected = {item.item_id for item in priority[:count]}
    assert [item["item_id"] for item in payload["items"]] == [
        item.item_id for item in items if item.item_id in selected
    ]
    assert payload["missing_item_ids"] == [
        item.item_id for item in items if item.item_id not in selected
    ]
    assert all(item["is_truncated"] is False for item in payload["items"])


@pytest.mark.parametrize("seed_index", [0, 2, 4])
def test_partial_bundle_always_contains_the_requested_seed(open_core, seed_index):
    items = parts("note", "A", tuple("quasar " * 160 for _ in range(5)))
    core = open_core(items)
    full = read(core, items[seed_index]).payload
    partial = read(
        core, items[seed_index],
        max_estimated_tokens=full["usage"]["estimated_evidence_tokens"] - 300,
    ).payload
    assert partial["bundle_status"] == "partial_budget"
    assert partial["seed_item_id"] == items[seed_index].item_id
    assert items[seed_index].item_id in {item["item_id"] for item in partial["items"]}


@pytest.mark.parametrize("constraint", ["tokens", "bytes"])
def test_search_budget_returns_ranked_prefix_and_distinguishes_no_matches(open_core, monkeypatch, constraint):
    core = open_core(exchange())
    request = {"queries": ["quasar"], "limit": 20}
    full = core.search(request, request_id="req_synthetic")
    if constraint == "tokens":
        request["max_estimated_tokens"] = full.payload["usage"]["estimated_evidence_tokens"] - 150
    else:
        monkeypatch.setattr(public_core, "MAX_RESPONSE_BYTES", len(full.json_bytes) - 400)
    partial = core.search(request, request_id="req_synthetic").payload
    assert 0 < len(partial["results"]) < 4
    assert partial["is_truncated"] is True
    assert partial["results"] == full.payload["results"][:len(partial["results"])]
    none = core.search({"queries": ["absentword"]}).payload
    assert none["results"] == [] and none["is_truncated"] is False
    limited = core.search({"queries": ["quasar"], "limit": 1}).payload
    assert len(limited["results"]) == 1 and limited["is_truncated"] is False


def test_seed_must_fit_and_impossible_minimum_fails(open_core, monkeypatch):
    item = parts("note", "A", ("quasar " * 250,))[0]
    core = open_core((item,))
    for operation in (
        lambda: read(core, item, max_estimated_tokens=250),
        lambda: read(core, item, max_estimated_tokens=1),
        lambda: core.search({"queries": ["quasar"], "max_estimated_tokens": 1}),
    ):
        with pytest.raises(BudgetExceededError):
            operation()
    monkeypatch.setattr(public_core, "MAX_RESPONSE_BYTES", 10)
    with pytest.raises(BudgetExceededError):
        read(core, item)


@pytest.mark.parametrize("values", [
    {"queries": ["quasar"], "extra": 1}, {"queries": "quasar"},
    {"queries": ["quasar"], "scopes": None}, {"queries": ["quasar"], "limit": True},
    {"queries": ["Straße", "STRASSE"]}, {"queries": ["quasar"], "generation": None},
    {"queries": ["quasar"], "max_estimated_tokens": 8001},
    {"queries": ["quasar"], "max_estimated_tokens": True},
    {"queries": ["quasar"], "max_estimated_tokens": "100"},
])
def test_public_search_strict_request_validation(open_core, values):
    core = open_core(parts("note", "A", ("quasar",)))
    with pytest.raises(InvalidRequestError):
        core.search(values)


def test_generation_and_exact_seed_validation_precede_lookup(open_core):
    item = parts("note", "A", ("quasar",))[0]
    core = open_core((item,))
    with pytest.raises(GenerationMismatchError):
        core.search({"queries": ["quasar"], "generation": "gen_" + "b" * 20})
    with pytest.raises(GenerationMismatchError):
        core.read_bundle({"seed_item_id": "itm_" + "b" * 32, "generation": "gen_" + "b" * 20})
    with pytest.raises(ItemNotFoundError):
        core.read_bundle({"seed_item_id": "itm_" + "b" * 32, "generation": core.store.generation})
    for invalid in ({"seed_item_id": item.item_id}, {
        "seed_item_id": item.item_id, "generation": core.store.generation, "path": item.source_path,
    }, {"seed_item_id": item.source_path, "generation": core.store.generation}):
        with pytest.raises(InvalidRequestError):
            core.read_bundle(invalid)


@pytest.mark.parametrize("damage", ["cycle", "missing", "cross_unit", "disconnected", "body", "counterpart"])
def test_corrupt_relations_or_body_fail_without_partial_evidence(open_core, monkeypatch, damage):
    items = exchange()
    core = open_core(items)
    original = core.store.read_canonical_item

    def damaged(item_id, generation, **kwargs):
        item = original(item_id, generation, **kwargs)
        if item_id != items[0].item_id:
            return item
        if damage == "cycle":
            return replace(item, relations=replace(item.relations, previous_part_id=item.item_id))
        if damage == "missing":
            return replace(item, relations=replace(item.relations, next_part_id="itm_" + "b" * 32))
        if damage == "cross_unit":
            return replace(item, turn_index=2)
        if damage == "disconnected":
            return replace(item, relations=ItemRelations())
        if damage == "body":
            return replace(item, body="changed synthetic body")
        return replace(item, relations=replace(item.relations, counterpart_item_ids=(items[2].item_id,)))

    monkeypatch.setattr(core.store, "read_canonical_item", damaged)
    with pytest.raises(IndexUnavailableError):
        read(core, items[0])


def test_python_deadline_discards_late_response_and_does_not_poison_store(open_core, monkeypatch):
    import src.retrieval.public_core as module

    item = parts("note", "A", ("quasar",))[0]
    core = open_core((item,))
    original = module._json

    with monkeypatch.context() as patch:
        def expired(_):
            raise BudgetExceededError("synthetic deadline exceeded")

        def late(payload):
            output = original(payload)
            patch.setattr(module, "check_deadline", expired)
            return output

        patch.setattr(module, "_json", late)
        with pytest.raises(BudgetExceededError):
            read(core, item)
    assert read(core, item).payload["bundle_status"] == "complete"


def test_more_than_eight_parts_and_unicode_json_are_preserved(open_core):
    body = '# 虚构章节\n量子舟 "引用" \\ 路径\n🙂'
    items = parts("note", "unicode", (body,) * 12)
    core = open_core(items)
    response = read(core, items[-1])
    payload = response.payload
    assert payload["seed_item_id"] == items[-1].item_id
    assert payload["bundle_status"] == "complete"
    assert len(payload["items"]) == 12
    assert all(item["body"] == body for item in payload["items"])
    assert payload["usage"]["estimated_evidence_tokens"] == estimate_evidence_tokens(
        response.json_bytes.decode("utf-8")
    )


def test_known_missing_list_cannot_be_silently_shortened(open_core):
    items = parts("note", "A", ("quasar",) * 20)
    core = open_core(items)
    with pytest.raises(BudgetExceededError):
        read(core, items[0], max_estimated_tokens=250)


def test_public_budget_does_not_truncate_internal_evaluation_results(open_core, monkeypatch):
    core = open_core(exchange())
    monkeypatch.setattr(public_core, "MAX_RESPONSE_BYTES", 500)
    assert len(core.store.search_lex(["quasar"], limit=20).results) == 4
    public = core.search({"queries": ["quasar"], "limit": 20}).payload
    assert public["is_truncated"] is True
    assert len(public["results"]) < 4


def test_note_evidence_role_is_preserved_from_existing_projection(open_core):
    item = replace(parts("note", "A", ("quasar",))[0], evidence_role="synthetic_annotation")
    core = open_core((item,))
    assert read(core, item).payload["items"][0]["evidence_role"] == "synthetic_annotation"


def test_sqlite_deadline_interrupts_expensive_query_and_cleans_handler(open_core, monkeypatch):
    item = parts("note", "A", ("quasar",))[0]
    core = open_core((item,), timeout_ms=10)

    def expensive(*args):
        try:
            core.store._database().execute(
                "WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<100000000) "
                "SELECT sum(x) FROM n"
            ).fetchone()
        except sqlite3.Error as exc:
            raise IndexUnavailableError("synthetic query failed") from exc
        pytest.fail("expensive query was not interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(core.store, "_fuse", expensive)
        with pytest.raises(BudgetExceededError):
            core.search({"queries": ["quasar"]})
    assert core.store.search_lex(["quasar"]).results


def test_deadline_includes_waiting_for_connection_lock(open_core):
    core = open_core(parts("note", "A", ("quasar",)), timeout_ms=10)
    held, release = threading.Event(), threading.Event()

    def hold():
        with core.store._lock:
            held.set()
            release.wait(2)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert held.wait(1)
        start = monotonic()
        with pytest.raises(BudgetExceededError):
            core.search({"queries": ["quasar"]})
        assert monotonic() - start < 1
    finally:
        release.set()
        thread.join(2)


def test_status_advertises_configured_limits_without_filesystem_details(open_core):
    core = open_core(parts("note", "A", ("quasar",)), timeout_ms=2345)
    status = core.status().payload
    assert status["semantic_search"] is False
    assert status["capabilities"] == {
        "read_bundle": True, "max_estimated_tokens": 8000,
        "max_response_bytes": 65_536, "corpus_timeout_ms": 2345,
    }
    assert "synthetic-archive" not in json.dumps(status)


@pytest.mark.parametrize("value", [0, True, 1.5])
def test_limits_require_explicit_positive_integers(value):
    with pytest.raises(ValueError):
        RequestLimits(value)


@pytest.mark.parametrize("phase", ["snippet", "bundle_validation", "search_encode", "bundle_encode", "status_encode"])
def test_python_processing_allows_another_database_read(open_core, monkeypatch, phase):
    from concurrent.futures import ThreadPoolExecutor
    from src.retrieval import bundle, lexical_store, public_core

    item = parts("note", "A", ("quasar",))[0]
    core = open_core((item,))
    entered, release = threading.Event(), threading.Event()
    target, name = {
        "snippet": (lexical_store, "_make_snippet"),
        "bundle_validation": (bundle, "_validate_item"),
        "search_encode": (core, "_encode"),
        "bundle_encode": (core, "_encode"),
        "status_encode": (public_core, "_json"),
    }[phase]
    original = getattr(target, name)

    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(target, name, paused)
    operation = (lambda: read(core, item)) if phase.startswith("bundle") else (
        core.status if phase == "status_encode" else lambda: core.search({"queries": ["quasar"]})
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(operation)
        try:
            assert entered.wait(1)
            second = pool.submit(core.store.read_canonical_item, item.item_id, core.store.generation,
                                 deadline=monotonic() + 0.5)
            assert second.result(timeout=1).item_id == item.item_id
        finally:
            release.set()
        assert first.result(timeout=1).json_bytes


def test_bundle_reads_share_one_absolute_deadline(open_core, monkeypatch):
    from src.retrieval import bundle, lexical_store, public_core

    items = exchange()
    core = open_core(items, timeout_ms=100)
    clock = [100.0]
    for module in (bundle, lexical_store, public_core):
        monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    original = core.store.read_canonical_item
    deadlines = []

    def advancing(*args, **kwargs):
        deadlines.append(kwargs["deadline"])
        result = original(*args, **kwargs)
        clock[0] += 0.06
        return result

    monkeypatch.setattr(core.store, "read_canonical_item", advancing)
    with pytest.raises(BudgetExceededError):
        read(core, items[0])
    assert len(deadlines) == 2
    assert deadlines[0] == deadlines[1] == 100.1
    assert core.status().payload["generation"] == core.store.generation


def test_prepared_requests_copy_inputs_and_reject_other_core_or_operation(open_core):
    item = parts("note", "A", ("quasar",))[0]
    core = open_core((item,))
    request = {"queries": ["quasar"], "scopes": ["note"]}
    prepared = core.validate_request("search", request)
    request["queries"][0] = "absentword"
    request["scopes"].clear()
    assert core.search(prepared).payload["results"]
    with pytest.raises(InvalidRequestError):
        core.read_bundle(prepared)
    other = RetrievalCore(core.store, core.limits)
    with pytest.raises(InvalidRequestError):
        other.search(prepared)
    with pytest.raises(InvalidRequestError):
        core.search({"queries": []})


def test_close_waits_for_active_database_read(open_core, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    core = open_core(parts("note", "A", ("quasar",)))
    entered, release, closing, closed = (threading.Event() for _ in range(4))
    original = core.store._fuse

    def paused(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)

    def close():
        closing.set()
        core.store.close()
        closed.set()

    monkeypatch.setattr(core.store, "_fuse", paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reading = pool.submit(core.store.search_lex, ["quasar"])
        try:
            assert entered.wait(1)
            shutdown = pool.submit(close)
            assert closing.wait(1)
            assert not closed.wait(0.03)
        finally:
            release.set()
        assert reading.result(timeout=1).results
        shutdown.result(timeout=1)
    with pytest.raises(IndexUnavailableError):
        core.status()
