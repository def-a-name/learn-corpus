"""验证已提交来源并打包一个可手动部署的检索索引。"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath

from src.corpus.ingest_log import check_committed_ingest_log
from src.corpus.paths import REPO_ROOT, CODE_ROOT
from src.corpus.storage import sha256_file
from src.maintenance.scan_secrets import git_candidate_files, scan_paths
from src.retrieval.build_lexical_index import build_index
from src.retrieval.index_artifact import validate_index_artifact
from src.retrieval.project_items import project_corpus


REPOSITORY = "example-owner/learn-corpus-private"
REF = "refs/heads/main"
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")


def validate_code_checkout(repo_root: Path, code_root: Path) -> str:
    """双仓库构建只接受数据 commit 固定且工作区未修改的代码版本。"""

    code_root = code_root.resolve(strict=True)
    if code_root == repo_root:
        relative = "."
    else:
        try:
            relative = code_root.relative_to(repo_root).as_posix()
        except ValueError as exc:
            raise ValueError("release code must be in the data repository's pinned submodule") from exc
        entry = subprocess.run(["git", "ls-tree", "HEAD", "--", relative], cwd=repo_root, capture_output=True, text=True, check=True).stdout.split()
        if len(entry) != 4 or entry[0:2] != ["160000", "commit"]:
            raise ValueError("release code directory must be a committed submodule")
    actual = subprocess.run(["git", "rev-parse", "HEAD"], cwd=code_root, capture_output=True, text=True, check=True).stdout.strip()
    if relative != "." and actual != entry[2]:
        raise ValueError("release code checkout does not match the data commit's submodule")
    dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=normal"], cwd=code_root, capture_output=True, text=True, check=True).stdout
    if dirty:
        raise ValueError("release code checkout contains uncommitted changes")
    return actual


def _validate_assets(repo_root: Path) -> None:
    """只校验 manifest 登记的标准化附件，不回读 raw 输入。"""

    manifest = json.loads((repo_root / "meta" / "manifest.json").read_text(encoding="utf-8"))
    records = manifest.get("sources") if isinstance(manifest, dict) else None
    if not isinstance(records, dict):
        raise ValueError("manifest sources must be an object")
    assets_root = (repo_root / "sources" / "assets").resolve()
    for source_id, record in records.items():
        if not isinstance(record, dict):
            raise ValueError(f"manifest source record is invalid: {source_id}")
        assets = record.get("assets") or []
        if not isinstance(assets, list):
            raise ValueError(f"manifest assets must be a list: {source_id}")
        for asset in assets:
            if not isinstance(asset, dict):
                raise ValueError(f"manifest asset record is invalid: {source_id}")
            relative = asset.get("stored_path")
            expected_hash = asset.get("asset_hash")
            if not isinstance(relative, str) or not isinstance(expected_hash, str):
                raise ValueError(f"manifest asset path or hash is invalid: {source_id}")
            pure = PurePosixPath(relative)
            if (
                pure.is_absolute()
                or pure.parts[:2] != ("sources", "assets")
                or len(pure.parts) < 3
                or any(part in {"", ".", ".."} for part in pure.parts)
                or "\\" in relative
            ):
                raise ValueError(f"manifest asset path escapes sources/assets: {source_id}")
            path = (repo_root / relative).resolve()
            if not path.is_relative_to(assets_root) or not path.is_file():
                raise ValueError(f"manifest asset is missing or outside sources/assets: {source_id}")
            if sha256_file(path) != expected_hash:
                raise ValueError(f"manifest asset hash mismatch: {source_id}")


def build_release(repo_root: Path, output_root: Path, *, repository: str, ref: str, commit: str, run_id: str, run_attempt: str, base_commit: str | None = None, expected_repository: str = REPOSITORY, code_root: Path | None = None) -> dict:
    """只为预期仓库的 main 提交生成发布目录，不切换 current。"""

    repo_root = repo_root.resolve(strict=True)
    output_root = output_root.resolve()
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/(?!\.{1,2}$)[A-Za-z0-9_.-]+", expected_repository) is None or repository != expected_repository or ref != REF or SHA_PATTERN.fullmatch(commit) is None:
        raise ValueError("release repository, ref, or commit is invalid")
    if not run_id.isdecimal() or int(run_id) < 1:
        raise ValueError("release run ID is invalid")
    if not run_attempt.isdecimal() or int(run_attempt) < 1:
        raise ValueError("release run attempt is invalid")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, text=True, check=True
    ).stdout.strip()
    if head != commit:
        raise ValueError("release commit does not match checkout HEAD")
    if code_root is not None:
        validate_code_checkout(repo_root, code_root)
    if output_root.exists() and any(output_root.iterdir()):
        raise ValueError("release output directory is not empty")
    base_commit = base_commit if base_commit and set(base_commit) != {"0"} else None
    if base_commit is not None and SHA_PATTERN.fullmatch(base_commit) is None:
        raise ValueError("release base commit is invalid")
    log_errors = check_committed_ingest_log(repo_root, base_commit, commit)
    if log_errors:
        raise ValueError("committed ingest log check failed: " + "; ".join(log_errors))
    _validate_assets(repo_root)

    source_paths = [
        path for path in git_candidate_files(repo_root)
        if path.relative_to(repo_root).parts[0] == "sources"
    ]
    findings = scan_paths(source_paths, repo_root)
    if findings:
        for finding in findings:
            print(f"{finding.path}:{finding.line}: {finding.kind}")
        raise ValueError(f"source secrets scan failed: findings={len(findings)}")

    projection = project_corpus(repo_root)
    result = build_index(projection, output_root)
    index_path = result.index_path
    manifest = validate_index_artifact(index_path)
    release = {
        "version": 2,
        "repository": repository,
        "ref": ref,
        "commit": commit,
        "run_id": int(run_id),
        "run_attempt": int(run_attempt),
        "index_id": result.index_id,
        "source_digest": manifest["source_digest"],
        "database_sha256": manifest["database_sha256"],
    }
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "release.json").write_text(
        json.dumps(release, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    return release


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate sources and package a retrieval index release.")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repository", required=True, help="expected private data repository owner/name")
    args = parser.parse_args()
    try:
        release = build_release(
            args.repo_root,
            args.output,
            repository=os.environ.get("GITHUB_REPOSITORY", ""),
            ref=os.environ.get("GITHUB_REF", ""),
            commit=os.environ.get("GITHUB_SHA", ""),
            run_id=os.environ.get("GITHUB_RUN_ID", ""),
            run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT", ""),
            base_commit=os.environ.get("LEARN_CORPUS_BASE_COMMIT") or None,
            expected_repository=args.repository,
            code_root=CODE_ROOT,
        )
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Release build failed: {exc}\n")
    print(json.dumps(release, sort_keys=True))


if __name__ == "__main__":
    main()
