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

from scripts.common.ingest_log import SourceChangeTracker
from scripts.common.corpus_core import (
    MANIFEST_PATH,
    MARKDOWN_SOURCE_IMPORTER_VERSION,
    REPO_ROOT,
    load_manifest,
    redact_secrets,
    relative_to_repo,
    save_manifest,
    sha256_file,
    sha256_text,
    source_needs_redaction,
    utc_now,
    yaml_document,
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
        from scripts.ingest.import_claude import iter_export_units

        matched = next(
            (unit for unit in iter_export_units(input_root, kind="document") if unit.source_path.resolve() == path.resolve()),
            None,
        )
        if matched is not None:
            return matched.source_id
    identity = "\0".join((policy.origin, str(input_root.resolve()), relative_path))
    return f"{policy.layer}-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:20]}"


def discover_markdown(input_root: Path, includes: Iterable[str] | None = None) -> list[Path]:
    if not input_root.is_dir():
        raise FileNotFoundError(f"Markdown 输入目录不存在: {input_root}")
    if includes:
        paths: list[Path] = []
        root = input_root.resolve()
        for value in includes:
            path = (input_root / value).resolve()
            try:
                path.relative_to(root)
            except ValueError as exc:
                raise ValueError(f"include 越过输入目录: {value}") from exc
            if not path.is_file() or path.suffix.lower() != ".md":
                raise FileNotFoundError(f"include Markdown 不存在: {value}")
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
        source_hash=sha256_file(path),
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


def _atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def _atomic_copy(asset: LocalAsset) -> None:
    asset.stored_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = asset.stored_path.with_suffix(asset.stored_path.suffix + ".tmp")
    temporary.write_bytes(asset.source_path.read_bytes())
    temporary.replace(asset.stored_path)


def _assets_are_current(records: Any) -> bool:
    if not isinstance(records, list):
        return not records
    for item in records:
        if not isinstance(item, dict):
            return False
        path = REPO_ROOT / str(item.get("stored_path") or "")
        if not path.is_file() or sha256_file(path) != item.get("asset_hash"):
            return False
    return True


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


def import_markdown_sources(
    input_root: Path,
    policy: MarkdownPolicy,
    manifest_path: Path = MANIFEST_PATH,
    *,
    includes: Iterable[str] | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    paths = discover_markdown(input_root, includes)
    manifest = load_manifest(manifest_path)
    change_tracker = SourceChangeTracker.for_output(policy.output_dir)
    stats: dict[str, Any] = {
        "discovered": len(paths),
        "imported": 0,
        "unchanged": 0,
        "skipped": 0,
        "skip_reasons": {},
        "redactions": 0,
        "assets": 0,
        "images": 0,
        "links": 0,
    }
    prepared_by_path: dict[Path, PreparedSource] = {}
    errors_by_path: dict[Path, str] = {}
    for path in paths:
        try:
            prepared_by_path[path] = prepare_source(path, input_root, policy)
        except (OSError, MarkdownImportError, ValueError) as exc:
            errors_by_path[path] = str(exc)

    canonical_by_fingerprint: dict[str, str] = {}
    for source_id, item in manifest["sources"].items():
        if item.get("ingest_status") == "ready" and item.get("layer") == policy.layer:
            fingerprint = str(item.get("content_fingerprint") or "")
            if fingerprint:
                canonical_by_fingerprint.setdefault(fingerprint, source_id)

    for path in paths:
        relative_path = path.resolve().relative_to(input_root.resolve()).as_posix()
        fallback_id = _stable_id(path, input_root, policy, relative_path)
        current = manifest["sources"].get(fallback_id, {})
        if path in errors_by_path:
            stats["skipped"] += 1
            reason = errors_by_path[path].split(":", 1)[0]
            stats["skip_reasons"][reason] = stats["skip_reasons"].get(reason, 0) + 1
            if not dry_run:
                review = dict(current)
                review.update(
                    {
                        "origin": policy.origin,
                        "source_kind": "document",
                        "layer": policy.layer,
                        "source_path": str(path.resolve()),
                        "source_locator": f"{relative_path}@L1-L1",
                        "relative_path": relative_path,
                        "ingest_status": "review",
                        "curation_status": current.get("curation_status", "unassessed"),
                        "title": path.stem,
                        "ingest_issue": errors_by_path[path],
                        "importer_version": MARKDOWN_SOURCE_IMPORTER_VERSION,
                    }
                )
                manifest["sources"][fallback_id] = review
            continue

        prepared = prepared_by_path[path]
        current = manifest["sources"].get(prepared.source_id, {})
        if current and (
            current.get("origin") != policy.origin
            or Path(str(current.get("source_path") or "")).resolve() != prepared.path.resolve()
        ):
            stats["skipped"] += 1
            stats["skip_reasons"]["source_id_collision"] = stats["skip_reasons"].get("source_id_collision", 0) + 1
            continue
        canonical = canonical_by_fingerprint.get(prepared.content_fingerprint)
        if canonical and canonical != prepared.source_id:
            stats["skipped"] += 1
            stats["skip_reasons"]["exact_duplicate"] = stats["skip_reasons"].get("exact_duplicate", 0) + 1
            if not dry_run:
                manifest["sources"][prepared.source_id] = {
                    "origin": policy.origin,
                    "source_kind": "document",
                    "layer": policy.layer,
                    "source_path": str(prepared.path.resolve()),
                    "source_locator": prepared.locator,
                    "relative_path": prepared.relative_path,
                    "source_hash": prepared.source_hash,
                    "content_fingerprint": prepared.content_fingerprint,
                    "ingest_status": "skipped",
                    "curation_status": current.get("curation_status", "unassessed"),
                    "skip_reason": "exact_duplicate",
                    "duplicate_of": canonical,
                    "title": prepared.title,
                    "imported_at": utc_now(),
                    "importer_version": MARKDOWN_SOURCE_IMPORTER_VERSION,
                }
            continue
        canonical_by_fingerprint[prepared.content_fingerprint] = prepared.source_id
        output_path = policy.output_dir / f"{prepared.source_id}.md"
        is_unchanged = bool(
            current.get("ingest_status") == "ready"
            and current.get("source_hash") == prepared.source_hash
            and current.get("content_fingerprint") == prepared.content_fingerprint
            and current.get("importer_version") == MARKDOWN_SOURCE_IMPORTER_VERSION
            and output_path.is_file()
            and not source_needs_redaction(output_path)
            and _assets_are_current(current.get("assets", []))
        )
        if is_unchanged:
            stats["unchanged"] += 1
            continue
        if limit is not None and stats["imported"] >= limit:
            stats["skipped"] += 1
            stats["skip_reasons"]["limit_reached"] = stats["skip_reasons"].get("limit_reached", 0) + 1
            continue
        stats["imported"] += 1
        stats["redactions"] += prepared.redaction_count
        stats["assets"] += len(prepared.assets)
        stats["images"] += len(prepared.scan.image_references)
        stats["links"] += prepared.scan.link_count
        if dry_run:
            continue
        for asset in prepared.assets:
            change_tracker.observe(asset.stored_path)
            _atomic_copy(asset)
        change_tracker.observe(output_path)
        _atomic_write_text(output_path, _render_document(prepared, policy))
        manifest["sources"][prepared.source_id] = _manifest_record(prepared, policy, output_path, current)

    if stats["discovered"] != stats["imported"] + stats["unchanged"] + stats["skipped"]:
        raise AssertionError("Markdown import discovered 等式不成立")
    stats["skip_reasons"] = dict(sorted(stats["skip_reasons"].items()))
    if not dry_run:
        manifest["version"] = max(int(manifest.get("version", 1)), 2)
        save_manifest(manifest, manifest_path)
        change_tracker.append(f"{policy.layer}s")
    return stats


def print_stats(label: str, stats: dict[str, Any], dry_run: bool) -> None:
    reasons = ", ".join(f"{key}={value}" for key, value in stats["skip_reasons"].items()) or "none"
    print(
        f"{label} ({'dry-run' if dry_run else 'written'}): "
        f"discovered={stats['discovered']}, imported={stats['imported']}, "
        f"unchanged={stats['unchanged']}, skipped={stats['skipped']}, "
        f"assets={stats['assets']}, images={stats['images']}, links={stats['links']}, "
        f"redactions={stats['redactions']}, skip_reasons={reasons}"
    )
