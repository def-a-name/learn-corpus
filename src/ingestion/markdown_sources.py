#!/usr/bin/env python3
"""个人笔记和外部文章共用的只读 Markdown 导入核心。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit

import yaml

from src.corpus.document import yaml_document
from src.corpus.manifest import utc_now
from src.corpus.paths import MANIFEST_PATH, REPO_ROOT, relative_to_repo
from src.corpus.storage import (
    registered_assets_are_current,
    sha256_file,
    sha256_text,
)
from src.ingestion.batch import (
    InputSnapshot,
    PlannedFile,
    PlannedSource,
    ProviderPlan,
    execute_batch,
)
from src.ingestion.common import (
    MARKDOWN_SOURCE_IMPORTER_VERSION,
    redact_secrets,
    source_needs_redaction,
)


_FENCE_START = re.compile(r"^ {0,3}(?P<marker>`{3,}|~{3,})")
_ATX_HEADING = re.compile(r"^ {0,3}(?P<level>#{1,6})[ \t]+(?P<title>.*?)[ \t]*#*[ \t]*$")
_MARKDOWN_IMAGE = re.compile(
    r"!\[(?P<alt>[^\]\n]*)\]\(\s*(?:<(?P<angled>[^>\n]+)>|(?P<plain>[^\s)\n]+))"
    r"(?:\s+(?P<title>\"[^\"\n]*\"|'[^'\n]*'|\([^\n)]*\)))?\s*\)"
)
_MARKDOWN_LINK = re.compile(
    r"(?<!!)\[(?P<label>[^\]\n]+)\]\(\s*(?:<(?P<angled>[^>\n]+)>|(?P<plain>[^\s)\n]+))"
    r"(?:\s+(?:\"[^\"\n]*\"|'[^'\n]*'|\([^\n)]*\)))?\s*\)"
)
_HTML_IMAGE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_HTML_LINK = re.compile(r"<a\b[^>]*>.*?</a\s*>", re.IGNORECASE)
_HTML_ATTRIBUTE = re.compile(
    r"\b(?P<name>src|alt|title|href)\s*=\s*(?:(?P<quote>[\"'])(?P<quoted>.*?)(?P=quote)|(?P<bare>[^\s>]+))",
    re.IGNORECASE,
)
_BARE_URL = re.compile(r"https?://[^\s<>\]\[\"']+")
_SECRET_METADATA_KEY = re.compile(r"(?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|密码|口令)", re.IGNORECASE)
_SUPPORTED_ASSET_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}


@dataclass(frozen=True)
class MarkdownPolicy:
    layer: str
    origin: str
    output_dir: Path
    asset_root: Path
    evidence_role: str | None = None


@dataclass(frozen=True)
class ImageReference:
    line: int
    target: str
    alt: str
    title: str
    target_span: tuple[int, int]


@dataclass(frozen=True)
class MarkdownScan:
    first_h1: str
    heading_count: int
    image_references: tuple[ImageReference, ...]
    link_count: int
    unresolved_link_count: int


@dataclass(frozen=True)
class LocalAsset:
    source_path: Path
    source_target: str
    stored_path: Path
    digest: str
    reference_count: int


@dataclass
class PreparedSource:
    path: Path
    relative_path: str
    source_id: str
    source_hash: str
    content_fingerprint: str
    input_metadata: dict[str, Any]
    body: str
    title: str
    locator: str
    scan: MarkdownScan
    rendered_body: str
    assets: tuple[LocalAsset, ...]
    remote_image_count: int
    external_image_count: int
    redaction_count: int


class MarkdownImportError(ValueError):
    """表示当前文件不能安全生成标准化副本。"""


def _normalize_body(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized:
        return ""
    return normalized.rstrip("\n") + "\n"


def split_frontmatter(raw: bytes, path: Path) -> tuple[dict[str, Any], str]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise MarkdownImportError(f"invalid_utf8: {path}:{exc.start}") from exc
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith("---\n"):
        return {}, normalized
    lines = normalized.splitlines(keepends=True)
    closing = next((index for index, line in enumerate(lines[1:], start=1) if line.rstrip("\n") == "---"), None)
    if closing is None:
        raise MarkdownImportError(f"frontmatter_unclosed: {path}")
    frontmatter_text = "".join(lines[1:closing])
    try:
        metadata = yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError as exc:
        raise MarkdownImportError(f"frontmatter_invalid: {path}") from exc
    if not isinstance(metadata, dict):
        raise MarkdownImportError(f"frontmatter_not_mapping: {path}")
    return metadata, "".join(lines[closing + 1 :])


def content_fingerprint(raw: bytes, path: Path = Path("<memory>")) -> str:
    _, body = split_frontmatter(raw, path)
    return sha256_text(_normalize_body(body))


def _scalar_text(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _sanitize_metadata(value: Any, key: str = "") -> tuple[Any, int]:
    if key and _SECRET_METADATA_KEY.search(key):
        return "[REDACTED]", 1
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        redactions = 0
        for child_key, child_value in value.items():
            rendered, count = _sanitize_metadata(child_value, str(child_key))
            result[str(child_key)] = rendered
            redactions += count
        return result, redactions
    if isinstance(value, list):
        result_list: list[Any] = []
        redactions = 0
        for item in value:
            rendered, count = _sanitize_metadata(item, key)
            result_list.append(rendered)
            redactions += count
        return result_list, redactions
    if isinstance(value, (date, datetime)):
        value = value.isoformat()
    if isinstance(value, str):
        return redact_secrets(value)
    return value, 0


def _html_attributes(tag: str) -> dict[str, tuple[str, tuple[int, int]]]:
    attributes: dict[str, tuple[str, tuple[int, int]]] = {}
    for match in _HTML_ATTRIBUTE.finditer(tag):
        value_group = "quoted" if match.group("quote") else "bare"
        attributes[match.group("name").lower()] = (match.group(value_group), match.span(value_group))
    return attributes


def _heading_slug(value: str) -> str:
    value = re.sub(r"[`*_~]", "", value.strip().lower())
    value = re.sub(r"[^\w\u4e00-\u9fff -]", "", value)
    return re.sub(r"[\s-]+", "-", value).strip("-")


def _target_status(target: str, path: Path, heading_slugs: set[str]) -> bool:
    if target.startswith("#"):
        return unquote(target[1:]).lower() in heading_slugs
    parsed = urlsplit(target)
    if parsed.scheme:
        return parsed.scheme not in {"http", "https"} or bool(parsed.netloc)
    resolved = (path.parent / unquote(parsed.path)).resolve()
    return resolved.exists()


def scan_markdown(body: str, path: Path) -> MarkdownScan:
    first_h1 = ""
    heading_count = 0
    headings: list[str] = []
    images: list[ImageReference] = []
    links: list[tuple[str, int]] = []
    fence_marker = ""
    lines = body.splitlines(keepends=True)
    for line_number, line in enumerate(lines, start=1):
        fence = _FENCE_START.match(line)
        if fence:
            marker = fence.group("marker")
            if not fence_marker:
                fence_marker = marker
            elif (
                marker[0] == fence_marker[0]
                and len(marker) >= len(fence_marker)
                and not line[fence.end() :].strip()
            ):
                fence_marker = ""
            continue
        if fence_marker:
            continue
        heading = _ATX_HEADING.match(line.rstrip("\n"))
        if heading:
            title = heading.group("title").strip()
            heading_count += 1
            headings.append(_heading_slug(title))
            if len(heading.group("level")) == 1 and not first_h1:
                first_h1 = title
        occupied: list[tuple[int, int]] = []
        for match in _MARKDOWN_IMAGE.finditer(line):
            target_group = "angled" if match.group("angled") is not None else "plain"
            title = (match.group("title") or "").strip("\"'()")
            images.append(
                ImageReference(
                    line_number,
                    match.group(target_group),
                    match.group("alt"),
                    title,
                    match.span(target_group),
                )
            )
            occupied.append(match.span())
        for tag_match in _HTML_IMAGE.finditer(line):
            attributes = _html_attributes(tag_match.group(0))
            if "src" not in attributes:
                raise MarkdownImportError(f"image_missing_src: {path}:{line_number}")
            target, span = attributes["src"]
            images.append(
                ImageReference(
                    line_number,
                    target,
                    attributes.get("alt", ("", (0, 0)))[0],
                    attributes.get("title", ("", (0, 0)))[0],
                    (tag_match.start() + span[0], tag_match.start() + span[1]),
                )
            )
            occupied.append(tag_match.span())
        for match in _MARKDOWN_LINK.finditer(line):
            target_group = "angled" if match.group("angled") is not None else "plain"
            links.append((match.group(target_group), line_number))
            occupied.append(match.span())
        for tag_match in _HTML_LINK.finditer(line):
            attributes = _html_attributes(tag_match.group(0))
            if "href" in attributes:
                links.append((attributes["href"][0], line_number))
                occupied.append(tag_match.span())
        for match in _BARE_URL.finditer(line):
            if not any(start <= match.start() < end for start, end in occupied):
                links.append((match.group(0).rstrip(".,;:)"), line_number))
    if fence_marker:
        raise MarkdownImportError(f"fenced_code_unclosed: {path}")
    heading_slugs = {value for value in headings if value}
    unresolved = sum(not _target_status(target, path, heading_slugs) for target, _ in links)
    return MarkdownScan(first_h1, heading_count, tuple(images), len(links), unresolved)


def _stable_id(path: Path, input_root: Path, policy: MarkdownPolicy, relative_path: str) -> str:
    if policy.origin == "claude-export":
        from src.ingestion.import_claude import iter_export_units

        matched = next(
            (unit for unit in iter_export_units(input_root, kind="document", includes=[relative_path]) if unit.source_path.resolve() == path.resolve()),
            None,
        )
        if matched is not None:
            return matched.source_id
    identity = "\0".join((policy.origin, str(input_root.resolve()), relative_path))
    return f"{policy.layer}-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:20]}"


def discover_markdown(input_root: Path, includes: Iterable[str] | None = None) -> list[Path]:
    if not input_root.is_dir():
        raise FileNotFoundError(f"Markdown input directory does not exist: {input_root}")
    if includes:
        paths: list[Path] = []
        root = input_root.resolve()
        for value in includes:
            path = (input_root / value).resolve()
            try:
                path.relative_to(root)
            except ValueError as exc:
                raise ValueError(f"included path escapes the input directory: {value}") from exc
            if not path.is_file() or path.suffix.lower() != ".md":
                raise FileNotFoundError(f"included Markdown file does not exist: {value}")
            paths.append(path)
        return sorted(set(paths), key=lambda item: item.relative_to(root).as_posix())
    return [
        path
        for path in sorted(input_root.rglob("*.md"))
        if not any(part.startswith(".") for part in path.relative_to(input_root).parts)
    ]


def _resolve_assets(
    body: str,
    scan: MarkdownScan,
    path: Path,
    input_root: Path,
    policy: MarkdownPolicy,
    source_id: str,
) -> tuple[str, tuple[LocalAsset, ...], int, int]:
    root = input_root.resolve()
    replacements_by_line: dict[int, list[tuple[int, int, str]]] = {}
    references: dict[Path, list[ImageReference]] = {}
    remote_count = 0
    external_count = 0
    for reference in scan.image_references:
        parsed = urlsplit(reference.target)
        if parsed.scheme in {"http", "https"}:
            remote_count += 1
            continue
        if parsed.scheme or reference.target.startswith("/"):
            external_count += 1
            continue
        source_path = (path.parent / unquote(parsed.path)).resolve()
        try:
            source_path.relative_to(root)
        except ValueError as exc:
            raise MarkdownImportError(f"asset_path_escape: {path}:{reference.line}") from exc
        if not source_path.is_file():
            raise MarkdownImportError(f"asset_missing: {path}:{reference.line}:{reference.target}")
        if source_path.suffix.lower() not in _SUPPORTED_ASSET_SUFFIXES:
            raise MarkdownImportError(f"asset_type_unsupported: {path}:{reference.line}:{reference.target}")
        references.setdefault(source_path, []).append(reference)

    assets: list[LocalAsset] = []
    for source_path, source_references in sorted(references.items(), key=lambda item: str(item[0])):
        digest = sha256_file(source_path)
        safe_name, filename_redactions = redact_secrets(source_path.name)
        if filename_redactions:
            safe_name = f"asset-{digest[:12]}{source_path.suffix.lower()}"
        stored_path = policy.asset_root / policy.layer / source_id / f"{digest[:12]}-{safe_name}"
        rendered_target = Path("..", "assets", policy.layer, source_id, stored_path.name).as_posix()
        assets.append(
            LocalAsset(source_path, source_references[0].target, stored_path, digest, len(source_references))
        )
        for reference in source_references:
            replacements_by_line.setdefault(reference.line, []).append(
                (reference.target_span[0], reference.target_span[1], rendered_target)
            )

    lines = body.splitlines(keepends=True)
    for line_number, replacements in replacements_by_line.items():
        line = lines[line_number - 1]
        for start, end, value in sorted(replacements, reverse=True):
            line = line[:start] + value + line[end:]
        lines[line_number - 1] = line
    return "".join(lines), tuple(assets), remote_count, external_count


def prepare_source(path: Path, input_root: Path, policy: MarkdownPolicy) -> PreparedSource:
    raw = path.read_bytes()
    raw_metadata, body = split_frontmatter(raw, path)
    normalized_body = _normalize_body(body)
    relative_path = path.resolve().relative_to(input_root.resolve()).as_posix()
    source_id = _stable_id(path, input_root, policy, relative_path)
    scan = scan_markdown(normalized_body, path)
    title_value = _scalar_text(raw_metadata.get("title")) or scan.first_h1 or path.stem
    title, title_redactions = redact_secrets(title_value.strip())
    safe_metadata, metadata_redactions = _sanitize_metadata(raw_metadata)
    rendered_body, assets, remote_count, external_count = _resolve_assets(
        normalized_body, scan, path, input_root, policy, source_id
    )
    rendered_body, body_redactions = redact_secrets(rendered_body)
    line_count = max(1, len(raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n").splitlines()))
    return PreparedSource(
        path=path,
        relative_path=relative_path,
        source_id=source_id,
        source_hash=hashlib.sha256(raw).hexdigest(),
        content_fingerprint=sha256_text(normalized_body),
        input_metadata=safe_metadata,
        body=normalized_body,
        title=title,
        locator=f"{relative_path}@L1-L{line_count}",
        scan=scan,
        rendered_body=rendered_body,
        assets=assets,
        remote_image_count=remote_count,
        external_image_count=external_count,
        redaction_count=title_redactions + metadata_redactions + body_redactions,
    )


def _asset_records(prepared: PreparedSource) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for asset in prepared.assets:
        source_target, _ = redact_secrets(asset.source_target)
        records.append(
            {
                "source_path": str(asset.source_path.resolve()),
                "source_target": source_target,
                "stored_path": relative_to_repo(asset.stored_path),
                "asset_hash": asset.digest,
                "reference_count": asset.reference_count,
            }
        )
    return records


def _article_metadata(prepared: PreparedSource) -> dict[str, Any]:
    metadata = prepared.input_metadata
    source_url = _scalar_text(metadata.get("source"))
    tags = metadata.get("tags") or []
    if not isinstance(tags, list):
        tags = [str(tags)]
    return {
        "source_url": source_url or "unknown",
        "publication_source_status": "known" if source_url else "missing",
        "author": _scalar_text(metadata.get("author")) or "unknown",
        "published": _scalar_text(metadata.get("published")) or "unknown",
        "captured": _scalar_text(metadata.get("created")) or "unknown",
        "description": _scalar_text(metadata.get("description")) or "unknown",
        "tags": [str(item) for item in tags if str(item).strip()],
    }


def _render_document(prepared: PreparedSource, policy: MarkdownPolicy) -> str:
    asset_records = _asset_records(prepared)
    metadata: dict[str, Any] = {
        "id": prepared.source_id,
        "type": policy.layer,
        "origin": policy.origin,
        "source_kind": "document",
        "layer": policy.layer,
        "title": prepared.title,
        "imported": utc_now()[:10],
        "source_path": str(prepared.path.resolve()),
        "source_hash": f"sha256:{prepared.source_hash}",
        "content_fingerprint": f"sha256:{prepared.content_fingerprint}",
        "relative_path": prepared.relative_path,
        "source_locator": prepared.locator,
        "heading_count": prepared.scan.heading_count,
        "image_count": len(prepared.scan.image_references),
        "asset_count": len(prepared.assets),
        "remote_image_count": prepared.remote_image_count,
        "external_image_count": prepared.external_image_count,
        "link_count": prepared.scan.link_count,
        "unresolved_link_count": prepared.scan.unresolved_link_count,
        "redaction_count": prepared.redaction_count,
        "importer_version": MARKDOWN_SOURCE_IMPORTER_VERSION,
    }
    if policy.evidence_role:
        metadata["evidence_role"] = policy.evidence_role
    if policy.layer == "article":
        metadata.update(_article_metadata(prepared))
    if prepared.input_metadata:
        metadata["input_frontmatter"] = prepared.input_metadata
    if asset_records:
        metadata["assets"] = asset_records
    return yaml_document(metadata, prepared.rendered_body)


def _manifest_record(prepared: PreparedSource, policy: MarkdownPolicy, output_path: Path, current: dict[str, Any]) -> dict[str, Any]:
    record: dict[str, Any] = {
        "origin": policy.origin,
        "source_kind": "document",
        "layer": policy.layer,
        "source_path": str(prepared.path.resolve()),
        "source_locator": prepared.locator,
        "relative_path": prepared.relative_path,
        "source_hash": prepared.source_hash,
        "content_fingerprint": prepared.content_fingerprint,
        "output_path": relative_to_repo(output_path),
        "ingest_status": "ready",
        "curation_status": current.get("curation_status", "unassessed"),
        "title": prepared.title,
        "redaction_count": prepared.redaction_count,
        "assets": _asset_records(prepared),
        "image_count": len(prepared.scan.image_references),
        "link_count": prepared.scan.link_count,
        "unresolved_link_count": prepared.scan.unresolved_link_count,
        "imported_at": utc_now(),
        "importer_version": MARKDOWN_SOURCE_IMPORTER_VERSION,
    }
    if policy.evidence_role:
        record["evidence_role"] = policy.evidence_role
    if policy.layer == "article":
        record.update(_article_metadata(prepared))
    return record


class MarkdownStrategy:
    """将 note/article 的差异限制在 MarkdownPolicy，返回统一候选计划。"""

    def __init__(
        self,
        input_root: Path,
        policy: MarkdownPolicy,
        includes: Iterable[str] | None,
        limit: int | None,
    ) -> None:
        self.input_root = input_root
        self.policy = policy
        self.includes = includes
        self.limit = limit

    def _inventory_record(
        self,
        path: Path,
        source_id: str,
        digest: str,
        locator: str,
        title: str,
        status: str,
        reason: str,
        detail: str,
    ) -> dict[str, Any]:
        if self.policy.origin == "claude-export":
            from src.ingestion.import_claude import iter_export_units, unit_inventory_record

            relative_path = path.resolve().relative_to(self.input_root.resolve()).as_posix()
            unit = next(iter(iter_export_units(
                self.input_root, kind="document", includes=[relative_path]
            )), None)
            if unit is not None:
                record = unit_inventory_record(unit, status, reason)
                if detail:
                    record["review_details"] = [detail]
                return record
        record = {
            "source_id": source_id,
            "unit_kind": "document",
            "document_kind": self.policy.layer,
            "title": title,
            "raw_source_path": str(path.resolve()),
            "raw_source_hash": f"sha256:{digest}",
            "raw_source_locator": locator,
            "parse_status": status,
            "skip_reason": reason or None,
        }
        if detail:
            record["review_details"] = [detail]
        return record

    def prepare(self, manifest: dict[str, Any]) -> ProviderPlan:
        paths = discover_markdown(self.input_root, self.includes)
        stats: dict[str, Any] = {
            "discovered": len(paths), "imported": 0, "unchanged": 0,
            "skipped": 0, "skip_reasons": {}, "redactions": 0,
            "assets": 0, "images": 0, "links": 0,
        }
        plan = ProviderPlan(
            provider="claude-export" if self.policy.origin == "claude-export" else f"{self.policy.layer}s",
            importer=f"{self.policy.layer}s",
            input_root=self.input_root,
            output_root=self.policy.output_dir,
            stats=stats,
            selected_paths=paths,
        )
        canonical_by_fingerprint: dict[str, str] = {}
        for source_id, item in manifest["sources"].items():
            if item.get("ingest_status") == "ready" and item.get("layer") == self.policy.layer:
                fingerprint = str(item.get("content_fingerprint") or "")
                if fingerprint:
                    canonical_by_fingerprint.setdefault(fingerprint, source_id)

        for path in paths:
            relative_path = path.resolve().relative_to(self.input_root.resolve()).as_posix()
            source_id = _stable_id(path, self.input_root, self.policy, relative_path)
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            plan.snapshots.append(InputSnapshot(path, digest))
            locator = f"{relative_path}@L1-L{max(1, len(raw.splitlines()))}"
            reason = ""
            detail = ""
            status = "ready"
            prepared: PreparedSource | None = None
            try:
                prepared = prepare_source(path, self.input_root, self.policy)
            except (OSError, MarkdownImportError, ValueError) as exc:
                detail = str(exc)
                reason = detail.split(":", 1)[0]
            if prepared is not None:
                source_id = prepared.source_id
                locator = prepared.locator
                current = manifest["sources"].get(source_id, {})
                if prepared.source_hash != digest:
                    reason = "source_changed_during_analysis_review"
                elif current and (
                    current.get("origin") != self.policy.origin
                    or Path(str(current.get("source_path") or "")).resolve() != path.resolve()
                ):
                    reason = "source_id_collision_review"
                else:
                    canonical = canonical_by_fingerprint.get(prepared.content_fingerprint)
                    if canonical and canonical != source_id:
                        reason = "exact_duplicate"
                if not reason:
                    output_path = self.policy.output_dir / f"{source_id}.md"
                    is_unchanged = bool(
                        current.get("ingest_status") == "ready"
                        and current.get("source_hash") == prepared.source_hash
                        and current.get("content_fingerprint") == prepared.content_fingerprint
                        and current.get("importer_version") == MARKDOWN_SOURCE_IMPORTER_VERSION
                        and output_path.is_file()
                        and not source_needs_redaction(output_path)
                        and registered_assets_are_current(current.get("assets", []))
                    )
                    if is_unchanged:
                        stats["unchanged"] += 1
                        status = "imported"
                        canonical_by_fingerprint[prepared.content_fingerprint] = source_id
                    elif self.limit is not None and stats["imported"] >= self.limit:
                        reason = "limit_reached"
                    else:
                        document = _render_document(prepared, self.policy)
                        files = tuple(
                            PlannedFile(asset.stored_path, copy_from=asset.source_path)
                            for asset in prepared.assets
                        ) + (PlannedFile(output_path, text=document),)
                        plan.sources.append(PlannedSource(
                            source_id,
                            _manifest_record(prepared, self.policy, output_path, current),
                            files,
                        ))
                        canonical_by_fingerprint[prepared.content_fingerprint] = source_id
                        plan.snapshots.extend(
                            InputSnapshot(asset.source_path, asset.digest)
                            for asset in prepared.assets
                        )
                        stats["imported"] += 1
                        stats["redactions"] += prepared.redaction_count
                        stats["assets"] += len(prepared.assets)
                        stats["images"] += len(prepared.scan.image_references)
                        stats["links"] += prepared.scan.link_count
            if reason:
                status = "excluded" if reason in {"exact_duplicate", "limit_reached"} else "review"
                stats["skipped"] += 1
                reasons = stats["skip_reasons"]
                reasons[reason] = reasons.get(reason, 0) + 1
                if reason not in {"exact_duplicate", "limit_reached"}:
                    plan.blocking_reasons.append(reason)
            plan.inventory_records.append(self._inventory_record(
                path, source_id, digest, locator,
                prepared.title if prepared else path.stem,
                status, reason, detail,
            ))
        if stats["discovered"] != stats["imported"] + stats["unchanged"] + stats["skipped"]:
            raise AssertionError("Markdown import discovered count is inconsistent")
        stats["skip_reasons"] = dict(sorted(stats["skip_reasons"].items()))
        stats["blocked"] = bool(plan.blocking_reasons)
        return plan


def import_markdown_sources(
    input_root: Path,
    policy: MarkdownPolicy,
    manifest_path: Path = MANIFEST_PATH,
    *,
    includes: Iterable[str] | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    strategy = MarkdownStrategy(input_root, policy, includes, limit)
    return execute_batch([strategy], manifest_path, dry_run=dry_run)[0].stats


def print_stats(label: str, stats: dict[str, Any], dry_run: bool) -> None:
    reasons = ", ".join(f"{key}={value}" for key, value in stats["skip_reasons"].items()) or "none"
    state = "dry-run" if dry_run else "blocked" if stats.get("blocked") else "written"
    print(
        f"{label} ({state}): "
        f"discovered={stats['discovered']}, imported={stats['imported']}, "
        f"unchanged={stats['unchanged']}, skipped={stats['skipped']}, "
        f"assets={stats['assets']}, images={stats['images']}, links={stats['links']}, "
        f"redactions={stats['redactions']}, skip_reasons={reasons}"
        + (f", deferred_by_batch={stats['deferred_by_batch']}" if stats.get("blocked") else "")
    )
