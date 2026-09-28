"""固定来源批次的准备、阻断与正式提交边界。"""

from __future__ import annotations

import json
import tempfile
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from src.corpus.ingest_log import SourceChangeTracker
from src.corpus.manifest import load_manifest, save_manifest
from src.corpus.paths import REPO_ROOT
from src.corpus.storage import atomic_copy_file, atomic_write_text, sha256_file


@dataclass(frozen=True)
class InputSnapshot:
    path: Path
    digest: str


@dataclass(frozen=True)
class PlannedFile:
    path: Path
    text: str | None = None
    copy_from: Path | None = None

    def __post_init__(self) -> None:
        if (self.text is None) == (self.copy_from is None):
            raise ValueError("planned file must contain text or a source file")


@dataclass(frozen=True)
class PlannedSource:
    source_id: str
    manifest_record: dict[str, Any]
    files: tuple[PlannedFile, ...]


@dataclass
class ProviderPlan:
    provider: str
    importer: str
    input_root: Path
    output_root: Path
    stats: dict[str, Any]
    selected_paths: list[Path] = field(default_factory=list)
    snapshots: list[InputSnapshot] = field(default_factory=list)
    inventory_records: list[dict[str, Any]] = field(default_factory=list)
    sources: list[PlannedSource] = field(default_factory=list)
    blocking_reasons: list[str] = field(default_factory=list)


class SourceStrategy(Protocol):
    """来源只负责分析并构造候选结果，不直接改写正式产物。"""

    def prepare(self, manifest: dict[str, Any]) -> ProviderPlan: ...


def validate_snapshots(plans: list[ProviderPlan]) -> None:
    """在提交前确认原文和附件仍是准备阶段读取的版本。"""

    seen: dict[Path, str] = {}
    for plan in plans:
        for snapshot in plan.snapshots:
            path = snapshot.path.resolve()
            previous = seen.setdefault(path, snapshot.digest)
            if previous != snapshot.digest or not path.is_file() or sha256_file(path) != snapshot.digest:
                raise ValueError(f"source changed during import preparation: {path}")


def _manifest_digest(path: Path) -> str | None:
    return sha256_file(path) if path.is_file() else None


def validate_plan_targets(plans: list[ProviderPlan], manifest: dict[str, Any]) -> None:
    """提交前确认候选来源没有重复身份、路径或已有来源冲突。"""

    planned_ids: set[str] = set()
    planned_paths: set[Path] = set()
    for plan in plans:
        for source in plan.sources:
            if source.source_id in planned_ids:
                raise ValueError(f"duplicate planned source ID: {source.source_id}")
            planned_ids.add(source.source_id)
            current = manifest["sources"].get(source.source_id)
            if current is not None and current.get("ingest_status") == "ready":
                expected = source.manifest_record.get("source_path")
                if current.get("source_path") != expected:
                    raise ValueError(f"source ID collision during commit: {source.source_id}")
            for item in source.files:
                resolved = item.path.resolve()
                if resolved in planned_paths:
                    raise ValueError(f"duplicate planned output path: {item.path}")
                planned_paths.add(resolved)


def mark_target_conflicts(plans: list[ProviderPlan], manifest: dict[str, Any]) -> None:
    """将来源身份、输出路径或未登记文件冲突写入审核结果。"""

    candidates = [(plan, source) for plan in plans for source in plan.sources]
    ids: dict[str, list[tuple[ProviderPlan, PlannedSource]]] = {}
    paths: dict[Path, list[tuple[ProviderPlan, PlannedSource]]] = {}
    registered: dict[Path, str] = {}
    for source_id, current in manifest["sources"].items():
        if current.get("ingest_status") != "ready":
            continue
        output = str(current.get("output_path") or "")
        if output:
            registered[(REPO_ROOT / output).resolve()] = source_id
        for asset in current.get("assets") or []:
            stored = str(asset.get("stored_path") or "")
            if stored:
                registered[(REPO_ROOT / stored).resolve()] = source_id
    for pair in candidates:
        plan, source = pair
        ids.setdefault(source.source_id, []).append(pair)
        for item in source.files:
            paths.setdefault(item.path.resolve(), []).append(pair)
    blocked: set[tuple[int, str]] = set()
    for plan, source in candidates:
        current = manifest["sources"].get(source.source_id, {})
        reason = ""
        if len(ids[source.source_id]) > 1:
            reason = "source_id_collision_review"
        elif current.get("ingest_status") == "ready" and current.get("source_path") != source.manifest_record.get("source_path"):
            reason = "source_id_collision_review"
        else:
            for item in source.files:
                path = item.path.resolve()
                owner = registered.get(path)
                if len(paths[path]) > 1 or (owner is not None and owner != source.source_id) or (path.exists() and owner is None):
                    reason = "output_path_collision_review"
                    break
        if not reason:
            continue
        key = (id(plan), source.source_id)
        if key in blocked:
            continue
        blocked.add(key)
        plan.blocking_reasons.append(reason)
        plan.stats["blocked"] = True
        plan.stats["imported"] -= 1
        plan.stats["skipped"] += 1
        reasons = plan.stats["skip_reasons"]
        reasons[reason] = reasons.get(reason, 0) + 1
        for record in plan.inventory_records:
            if record.get("source_id") == source.source_id and not record.get("skip_reason"):
                record["parse_status"] = "review"
                record["skip_reason"] = reason
                break
        else:
            raise ValueError(f"planned source is missing inventory record: {source.source_id}")
    for plan in plans:
        plan.sources = [source for source in plan.sources if (id(plan), source.source_id) not in blocked]


def mark_inventory_collisions(plans: list[ProviderPlan], inventory_path: Path) -> None:
    """将本批与现有未选输入之间的来源 ID 冲突转为审核问题。"""

    existing: dict[str, Any] = {}
    if inventory_path.is_file():
        existing = json.loads(inventory_path.read_text(encoding="utf-8"))
        if existing.get("version") != 3 or not isinstance(existing.get("inputs"), list):
            raise ValueError("existing source inventory has an unsupported format")
    selected = {str(path.resolve()) for plan in plans for path in plan.selected_paths}
    prior: dict[tuple[str, str], set[str]] = {}
    for source_input in existing.get("inputs", []):
        provider = str(source_input.get("provider") or "")
        for record in source_input.get("units", []):
            path = str(record.get("raw_source_path") or "")
            source_id = str(record.get("source_id") or "")
            if path and source_id and path not in selected:
                prior.setdefault((provider, source_id), set()).add(path)
    for plan in plans:
        for record in plan.inventory_records:
            source_id = str(record.get("source_id") or "")
            path = str(record.get("raw_source_path") or "")
            if source_id and path:
                prior.setdefault((plan.provider, source_id), set()).add(path)
    for plan in plans:
        for record in plan.inventory_records:
            source_id = str(record.get("source_id") or "")
            path = str(record.get("raw_source_path") or "")
            if not source_id or len(prior.get((plan.provider, source_id), ())) < 2:
                continue
            old_reason = record.get("skip_reason")
            if old_reason is None:
                if record.get("parse_status") == "imported":
                    plan.stats["unchanged"] -= 1
                elif record.get("parse_status") == "ready":
                    plan.stats["imported"] -= 1
                plan.stats["skipped"] += 1
                reasons = plan.stats["skip_reasons"]
                reasons["source_id_collision_review"] = reasons.get("source_id_collision_review", 0) + 1
            record["parse_status"] = "review"
            record["skip_reason"] = "source_id_collision_review"
            details = list(record.get("review_details") or [])
            details.append("多个原始文件解析为同一来源 ID")
            record["review_details"] = details
            review_reasons = list(record.get("review_reasons") or [])
            if "source_id_collision_review" not in review_reasons:
                review_reasons.insert(0, "source_id_collision_review")
            record["review_reasons"] = review_reasons
            plan.blocking_reasons.append("source_id_collision_review")
            plan.sources = [source for source in plan.sources if source.source_id != source_id]
            plan.stats["blocked"] = True


def merge_inventory(
    existing: dict[str, Any] | None,
    plans: list[ProviderPlan],
    manifest: dict[str, Any],
    *,
    scanned_at: str | None = None,
) -> dict[str, Any]:
    """仅替换本批明确选择的原始文件对应的盘点单元。"""

    if existing is not None:
        inventory = deepcopy(existing)
        if inventory.get("version") != 3 or not isinstance(inventory.get("inputs"), list):
            raise ValueError("existing source inventory has an unsupported format")
    else:
        inventory = {"version": 3, "inputs": []}
    timestamp = scanned_at or datetime.now(ZoneInfo("Asia/Hong_Kong")).isoformat(timespec="seconds")
    inventory["updated_at"] = timestamp
    grouped: dict[str, list[ProviderPlan]] = {}
    for plan in plans:
        grouped.setdefault(plan.provider, []).append(plan)
    for provider, provider_plans in grouped.items():
        positions = [index for index, item in enumerate(inventory["inputs"]) if item.get("provider") == provider]
        if len(positions) > 1:
            raise ValueError(f"duplicate inventory provider: {provider}")
        if positions:
            current = inventory["inputs"][positions[0]]
            if not isinstance(current.get("units"), list):
                raise ValueError(f"existing {provider} inventory is missing unit records")
        else:
            current = {"provider": provider, "units": []}
        selected = {str(path.resolve()) for plan in provider_plans for path in plan.selected_paths}
        records = [
            deepcopy(record)
            for record in current["units"]
            if str(record.get("raw_source_path") or "") not in selected
        ]
        for plan in provider_plans:
            records.extend(deepcopy(plan.inventory_records))
        record_keys: set[tuple[str, str, str]] = set()
        unique_records: list[dict[str, Any]] = []
        for record in records:
            key = (
                str(record.get("source_id") or ""),
                str(record.get("raw_source_path") or ""),
                str(record.get("raw_source_locator") or ""),
            )
            if key not in record_keys:
                record_keys.add(key)
                unique_records.append(record)
        records = unique_records
        records.sort(key=lambda record: (
            str(record.get("raw_source_path") or ""),
            str(record.get("raw_source_locator") or ""),
            str(record.get("source_id") or ""),
        ))
        reasons = Counter(str(item["skip_reason"]) for item in records if item.get("skip_reason"))
        current.update({
            "input_path": str(provider_plans[0].input_root.resolve()),
            "available": True,
            "discovered": len(records),
            "retained": sum(not item.get("skip_reason") for item in records),
            "skipped": sum(bool(item.get("skip_reason")) for item in records),
            "skip_reasons": dict(sorted(reasons.items())),
            "currently_standardized": sum(
                item.get("ingest_status") == "ready"
                and item.get("origin") == {
                    "notes": "personal-notes",
                    "articles": "external-articles",
                    "web-chat": "web-chat-export",
                }.get(provider, provider)
                for item in manifest["sources"].values()
            ),
            "units": records,
            "scanned_at": timestamp,
        })
        if provider in {"claude-export", "codex", "web-chat"}:
            known_dates = sorted(
                str(item["created"])
                for item in records
                if item.get("created") and item.get("created") != "unknown"
            )
            current["date_range"] = {
                "from": known_dates[0] if known_dates else None,
                "to": known_dates[-1] if known_dates else None,
            }
        if provider in {"claude-export", "codex"}:
            threads = Counter(
                str(item.get("thread_kind") or "unknown")
                for item in records if item.get("unit_kind") == "session"
            )
            current["thread_counts"] = {
                "main": threads["main"],
                "subagent": threads["subagent"],
                "unknown": threads["unknown"],
            }
        if provider == "claude-export":
            kinds = Counter(str(item.get("unit_kind") or "unknown") for item in records)
            documents = Counter(
                str(item.get("document_kind") or "unknown")
                for item in records if item.get("unit_kind") == "document"
            )
            current["unit_counts"] = {"session": kinds["session"], "document": kinds["document"]}
            current["document_counts"] = dict(sorted(documents.items()))
        if provider == "web-chat":
            providers = Counter(str(item.get("provider") or "unknown") for item in records)
            current["provider_counts"] = dict(sorted(providers.items()))
        if positions:
            inventory["inputs"][positions[0]] = current
        else:
            inventory["inputs"].append(current)
    return inventory


def update_auxiliary_state(plans: list[ProviderPlan], manifest_path: Path) -> None:
    """在正式提交前记录本批分析结果及审核问题。"""

    from src.ingestion.build_source_inventory import save_inventory
    from src.maintenance.rebuild_review_queue import build_queue

    inventory_path = manifest_path.parent / "source-inventory.json"
    queue_path = manifest_path.parent / "source-review-queue.md"
    existing = json.loads(inventory_path.read_text(encoding="utf-8")) if inventory_path.is_file() else None
    inventory = merge_inventory(existing, plans, load_manifest(manifest_path))
    selected = [path for plan in plans for path in plan.selected_paths]
    with tempfile.TemporaryDirectory() as temporary:
        pending_inventory = Path(temporary) / "source-inventory.json"
        save_inventory(inventory, pending_inventory)
        queue = build_queue(
            manifest_path,
            queue_path,
            pending_inventory,
            dry_run=True,
            source_paths=selected if selected and queue_path.is_file() else None,
        )
    save_inventory(inventory, inventory_path)
    atomic_write_text(queue_path, queue)


def execute_batch(
    strategies: list[SourceStrategy],
    manifest_path: Path,
    *,
    dry_run: bool = False,
) -> list[ProviderPlan]:
    """先完成所有来源分析，再决定是否提交本批。"""

    manifest = load_manifest(manifest_path)
    manifest_digest = _manifest_digest(manifest_path)
    plans = [strategy.prepare(manifest) for strategy in strategies]
    effective_plans = [plan for plan in plans if plan.selected_paths or plan.inventory_records or plan.sources]
    mark_inventory_collisions(effective_plans, manifest_path.parent / "source-inventory.json")
    mark_target_conflicts(effective_plans, manifest)
    validate_plan_targets(effective_plans, manifest)
    if dry_run:
        return plans
    if not effective_plans:
        return plans
    validate_snapshots(effective_plans)
    if _manifest_digest(manifest_path) != manifest_digest:
        raise ValueError("manifest changed during import preparation")
    update_auxiliary_state(effective_plans, manifest_path)
    if any(plan.blocking_reasons for plan in plans):
        for plan in plans:
            plan.stats["deferred_by_batch"] = plan.stats["imported"]
            plan.stats["imported"] = 0
            plan.stats["blocked"] = True
        return plans
    commit_plans(effective_plans, manifest_path, expected_manifest_digest=manifest_digest)
    return plans


def commit_plans(
    plans: list[ProviderPlan],
    manifest_path: Path,
    *,
    expected_manifest_digest: str | None = None,
) -> None:
    """只提交全部准备完毕且未被阻断的来源批次。"""

    if any(plan.blocking_reasons for plan in plans):
        raise ValueError("cannot commit a batch with blocked sources")
    validate_snapshots(plans)
    if _manifest_digest(manifest_path) != expected_manifest_digest:
        raise ValueError("manifest changed during import preparation")
    manifest = load_manifest(manifest_path)
    validate_plan_targets(plans, manifest)
    selected_paths = {str(path.resolve()) for plan in plans for path in plan.selected_paths}
    removed_legacy = False
    for source_id, item in list(manifest["sources"].items()):
        if item.get("ingest_status") == "ready":
            continue
        if str(item.get("source_path") or "") in selected_paths:
            manifest["sources"].pop(source_id)
            removed_legacy = True
    trackers: list[tuple[ProviderPlan, SourceChangeTracker]] = []
    for plan in plans:
        tracker = SourceChangeTracker.for_output(plan.output_root)
        trackers.append((plan, tracker))
        for source in plan.sources:
            for item in source.files:
                tracker.observe(item.path)
                if item.copy_from is not None:
                    atomic_copy_file(item.copy_from, item.path)
                else:
                    atomic_write_text(item.path, item.text or "")
            manifest["sources"][source.source_id] = source.manifest_record
    if any(plan.sources for plan in plans) or removed_legacy:
        manifest["version"] = max(int(manifest.get("version", 1)), 2)
        save_manifest(manifest, manifest_path)
    for plan, tracker in trackers:
        tracker.append(plan.importer)
