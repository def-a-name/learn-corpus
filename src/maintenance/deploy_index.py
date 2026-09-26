"""从指定或最新成功的 GitHub Actions run 下载并发布一个检索索引。"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import ipaddress
import io
import json
import os
import pwd
import grp
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.retrieval.build_lexical_index import publish_index
from src.retrieval.index_artifact import LexicalBuildError, validate_index_artifact
from src.service.config import load_service_config, read_config_object
from src.service.errors import HTTPFailure


REPOSITORY = "def-a-name/learn-corpus"
REF = "refs/heads/main"
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
API_ROOT = f"https://api.github.com/repos/{REPOSITORY}"
INDEX_ID_PATTERN = re.compile(r"idx_[0-9a-f]{20}")
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
RUNS_PER_PAGE = 100
MAX_RUN_PAGES = 10
DEPLOYMENT_FIELDS = {
    "service_config", "github_header_file", "service", "service_user", "run_id",
}
REQUIRED_DEPLOYMENT_FIELDS = {
    "service_config", "github_header_file", "service", "service_user",
}


@dataclass(frozen=True)
class DeploymentConfig:
    """从独立配置文件加载的部署参数。"""

    service_config: Path
    github_header_file: Path
    service: str
    service_user: str
    run_id: int | None


@dataclass(frozen=True)
class LocalStatusTarget:
    """从服务配置推导的本机状态检查入口。"""

    host: str
    port: int
    host_header: str


def _progress(message: str) -> None:
    """立即输出部署进度，重定向 stdout 时也不等待缓冲区刷新。"""

    print(f"[deploy] {message}", flush=True)


def load_deployment_config(path: Path) -> DeploymentConfig:
    """严格读取部署配置，相对路径以该文件所在目录为基准。"""

    try:
        value = read_config_object(path)
    except HTTPFailure as exc:
        raise ValueError("deployment configuration JSON is invalid") from exc
    unknown = sorted(value.keys() - DEPLOYMENT_FIELDS)
    missing = sorted(REQUIRED_DEPLOYMENT_FIELDS - value.keys())
    if unknown:
        raise ValueError(f"deployment configuration contains unknown fields: {', '.join(unknown)}")
    if missing:
        raise ValueError(f"deployment configuration is missing required fields: {', '.join(missing)}")

    def config_path(name: str) -> Path:
        raw = value[name]
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"deployment {name} must be a nonempty path")
        target = Path(raw)
        return target if target.is_absolute() else (path.resolve().parent / target).resolve()

    for name in ("service", "service_user"):
        if not isinstance(value[name], str) or not value[name].strip():
            raise ValueError(f"deployment {name} must be a nonempty string")
    run_id = value.get("run_id", "latest")
    if run_id == "latest":
        run_id = None
    else:
        if isinstance(run_id, str) and re.fullmatch(r"[0-9]+", run_id):
            run_id = int(run_id)
        if type(run_id) is not int or run_id < 1:
            raise ValueError("deployment run_id must be a positive integer, a positive integer digit string, or latest")
    return DeploymentConfig(
        service_config=config_path("service_config"),
        github_header_file=config_path("github_header_file"),
        service=value["service"],
        service_user=value["service_user"],
        run_id=run_id,
    )


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """跨域跳转到对象存储时移除 GitHub Authorization。"""

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise ValueError("artifact download redirect must use HTTPS")
        redirected = super().redirect_request(request, fp, code, msg, headers, newurl)
        if redirected is not None and urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(request.full_url).netloc:
            redirected.remove_header("Authorization")
        return redirected


def _github_token(path: Path) -> str:
    if path.stat().st_mode & 0o077:
        raise ValueError("GitHub credential file permissions must be 0600")
    value = path.read_text(encoding="utf-8").strip()
    match = re.fullmatch(r"(?:Authorization:\s*Bearer\s+|header\s*=\s*\"Authorization:\s*Bearer\s+)([A-Za-z0-9_]+)\"?", value, re.IGNORECASE)
    if match is None:
        raise ValueError("GitHub credential file format is invalid")
    return match.group(1)


def _request(url: str, token: str, *, binary: bool = False) -> bytes | dict:
    if not url.startswith(API_ROOT + "/"):
        raise ValueError("GitHub API URL is outside the expected repository")
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "learn-corpus-index-deployer",
        },
    )
    with urllib.request.build_opener(_SafeRedirect()).open(request, timeout=30) as response:
        if binary:
            data = response.read(MAX_ARCHIVE_BYTES + 1)
            if len(data) > MAX_ARCHIVE_BYTES:
                raise ValueError("artifact archive exceeds size limit")
            return data
        data = response.read(2 * 1024 * 1024)
    return json.loads(data)


def _check_run(run: dict, run_id: int) -> tuple[str, int]:
    if not isinstance(run, dict) or not isinstance(run.get("repository"), dict):
        raise ValueError("workflow run response is invalid")
    commit = run.get("head_sha")
    attempt = run.get("run_attempt")
    if (
        run.get("id") != run_id
        or run.get("repository", {}).get("full_name") != REPOSITORY
        or run.get("head_branch") != "main"
        or str(run.get("path", "")).split("@")[0] != ".github/workflows/build-index.yml"
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or not isinstance(commit, str)
        or SHA_PATTERN.fullmatch(commit) is None
        or not isinstance(attempt, int)
        or attempt < 1
    ):
        raise ValueError("workflow run is not a successful main build for the expected repository")
    return commit, attempt


def _latest_run_id(token: str) -> int:
    """从预期工作流的成功运行中选创建时间最新的一次。"""

    latest: tuple[datetime, int] | None = None
    for page in range(1, MAX_RUN_PAGES + 1):
        _progress(f"Checking successful workflow runs (page {page}).")
        url = (
            f"{API_ROOT}/actions/workflows/build-index.yml/runs"
            f"?branch=main&status=success&per_page={RUNS_PER_PAGE}&page={page}"
        )
        payload = _request(url, token)
        if not isinstance(payload, dict) or not isinstance(payload.get("workflow_runs"), list):
            raise ValueError("workflow run list response is invalid")
        runs = payload["workflow_runs"]
        if len(runs) > RUNS_PER_PAGE:
            raise ValueError("workflow run list response exceeds page size")
        for run in runs:
            if not isinstance(run, dict) or type(run.get("id")) is not int or run["id"] < 1:
                raise ValueError("workflow run list contains an invalid run ID")
            _check_run(run, run["id"])
            created_at = run.get("created_at")
            if not isinstance(created_at, str) or not created_at.endswith("Z"):
                raise ValueError("workflow run list contains an invalid creation time")
            try:
                created = datetime.fromisoformat(created_at[:-1] + "+00:00")
            except ValueError as exc:
                raise ValueError("workflow run list contains an invalid creation time") from exc
            candidate = (created, run["id"])
            if latest is None or candidate > latest:
                latest = candidate
        if len(runs) < RUNS_PER_PAGE:
            break
    else:
        raise ValueError("workflow run search limit reached; set run_id in deployment configuration")
    if latest is None:
        raise ValueError("no successful main index workflow run was found")
    return latest[1]


def _check_artifacts(payload: dict, run_id: int, run_attempt: int, commit: str) -> tuple[int, str]:
    if not isinstance(payload, dict) or not isinstance(payload.get("artifacts"), list):
        raise ValueError("artifact list response is invalid")
    expected = f"learn-corpus-index-{run_id}-{run_attempt}"
    matches = [item for item in payload.get("artifacts", []) if item.get("name") == expected]
    if len(matches) != 1 or matches[0].get("expired") is not False:
        raise ValueError("expected index artifact is missing, duplicated, or expired")
    artifact_id = matches[0].get("id")
    digest = matches[0].get("digest")
    source_run = matches[0].get("workflow_run")
    if (
        not isinstance(artifact_id, int)
        or artifact_id < 1
        or not isinstance(digest, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
        or not isinstance(source_run, dict)
        or source_run.get("id") != run_id
        or source_run.get("head_sha") != commit
    ):
        raise ValueError("artifact metadata does not match the expected run")
    return artifact_id, digest


def _extract_release(data: bytes, target: Path, run_id: int, run_attempt: int, commit: str) -> tuple[dict, Path]:
    """精确解包三个允许的文件，拒绝路径逃逸和额外内容。"""

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("artifact archive contains duplicate paths")
        release_name = "release.json"
        if release_name not in names:
            raise ValueError("artifact release metadata is missing")
        if archive.getinfo(release_name).file_size > 65536:
            raise ValueError("artifact release metadata exceeds size limit")
        release = json.loads(archive.read(release_name))
        if not isinstance(release, dict) or set(release) != {
            "version", "repository", "ref", "commit", "run_id", "run_attempt", "index_id",
            "source_digest", "database_sha256",
        }:
            raise ValueError("artifact release metadata schema is invalid")
        index_id = release.get("index_id")
        if not isinstance(index_id, str) or INDEX_ID_PATTERN.fullmatch(index_id) is None:
            raise ValueError("artifact index ID is invalid")
        expected = {
            release_name,
            f"{index_id}/index.json",
            f"{index_id}/corpus.sqlite",
        }
        files = {name for name in names if not name.endswith("/")}
        directories = {name for name in names if name.endswith("/")}
        if files != expected or not directories.issubset({f"{index_id}/"}):
            raise ValueError("artifact archive contains unexpected paths")
        if (
            release.get("version") != 2
            or release.get("repository") != REPOSITORY
            or release.get("ref") != REF
            or release.get("run_id") != run_id
            or release.get("run_attempt") != run_attempt
            or release.get("commit") != commit
        ):
            raise ValueError("artifact release metadata does not match workflow run")
        index_path = target / index_id
        index_path.mkdir()
        for name in sorted(expected - {release_name}):
            member = archive.getinfo(name)
            if member.file_size > MAX_ARCHIVE_BYTES or (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("artifact member type or size is invalid")
            destination = index_path / Path(name).name
            with archive.open(member) as source, destination.open("wb") as output:
                shutil.copyfileobj(source, output)
    manifest = validate_index_artifact(index_path)
    if manifest["source_digest"] != release.get("source_digest") or manifest["database_sha256"] != release.get("database_sha256"):
        raise ValueError("artifact release metadata does not match index")
    return release, index_path


def _status_token(credentials_path: Path) -> str:
    rows = json.loads(credentials_path.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not rows:
        raise ValueError("service credentials are unavailable")
    row = rows[0]
    if not isinstance(row, dict) or not isinstance(row.get("key"), str) or not isinstance(row.get("secret"), str):
        raise ValueError("service credentials are invalid")
    encoded = base64.urlsafe_b64encode(f"{row['key']}.{row['secret']}".encode("ascii"))
    return encoded.decode("ascii").rstrip("=")


def _local_status_target(selected) -> LocalStatusTarget:
    """只允许从本机回环地址验收，避免通过明文 HTTP 向网络发送令牌。"""

    bind = ipaddress.ip_address(selected.host)
    if str(bind) == "0.0.0.0":
        host = "127.0.0.1"
    elif bind.is_loopback:
        host = str(bind)
    else:
        raise ValueError("HTTP service must have a loopback status endpoint for deployment")
    if host not in selected.runtime.allowed_peers:
        raise ValueError("HTTP service does not allow the local status peer")
    return LocalStatusTarget(host, selected.port, selected.runtime.allowed_hosts[0])


def _status(target: LocalStatusTarget, token: str) -> dict:
    connection = http.client.HTTPConnection(target.host, target.port, timeout=10)
    try:
        connection.request("GET", "/v1/status", headers={
            "Host": target.host_header,
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        })
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError("authenticated local service status request failed")
        data = response.read(65537)
        if len(data) > 65536:
            raise ValueError("local service status response exceeds size limit")
        payload = json.loads(data)
        if not isinstance(payload, dict):
            raise ValueError("local service status response is invalid")
        return payload
    finally:
        connection.close()


def _restart_and_wait(service: str, target: LocalStatusTarget, token: str, index_id: str) -> None:
    _progress("Restarting the HTTP service.")
    subprocess.run(["systemctl", "restart", service], check=True)
    _progress("Waiting for the service to report the new index.")
    for _ in range(12):
        try:
            response = _status(target, token)
            if response.get("index_id") == index_id:
                return
        except (OSError, ValueError, RuntimeError, http.client.HTTPException):
            pass
        time.sleep(2)
    raise RuntimeError("service did not report the expected index")


def deploy(config_path: Path) -> str:
    """先验证完整产物与本机代码，再安装、切换并验收；失败恢复旧索引。"""

    if os.geteuid() != 0:
        raise ValueError("deployment must run as root")
    _progress("Loading deployment configuration and checking local prerequisites.")
    deployment = load_deployment_config(config_path)
    selected = load_service_config(deployment.service_config)
    if selected.transport != "http":
        raise ValueError("deployment config must select HTTP transport")
    status_target = _local_status_target(selected)
    retrieval_root = selected.runtime.corpus_path.resolve(strict=True)
    token = _github_token(deployment.github_header_file)
    run_id = deployment.run_id
    if run_id is None:
        _progress("Selecting the latest successful main workflow run.")
        run_id = _latest_run_id(token)
    _progress(f"Checking workflow run {run_id}.")
    run = _request(f"{API_ROOT}/actions/runs/{run_id}", token)
    commit, run_attempt = _check_run(run, run_id)
    _progress(f"Workflow run verified: run {run_id}, attempt {run_attempt}, commit {commit}.")
    _progress("Checking index artifact metadata.")
    artifacts = _request(f"{API_ROOT}/actions/runs/{run_id}/artifacts?per_page=100", token)
    artifact_id, artifact_digest = _check_artifacts(artifacts, run_id, run_attempt, commit)
    _progress(f"Downloading artifact {artifact_id}.")
    archive = _request(f"{API_ROOT}/actions/artifacts/{artifact_id}/zip", token, binary=True)
    if f"sha256:{hashlib.sha256(archive).hexdigest()}" != artifact_digest:
        raise ValueError("downloaded artifact archive hash mismatch")
    _progress(f"Archive downloaded and SHA-256 verified ({len(archive)} bytes).")
    with tempfile.TemporaryDirectory(prefix=".release-", dir=retrieval_root) as temporary:
        _progress("Extracting and validating release metadata and index files.")
        release, staged = _extract_release(archive, Path(temporary), run_id, run_attempt, commit)
        index_id = release["index_id"]
        target = retrieval_root / index_id
        # 本机版本验证必须在触碰 current/previous 前完成。
        _progress(f"Checking index {index_id} against local code.")
        validate_index_artifact(staged, expected_index_id=index_id)
        old_current_link = retrieval_root / "current"
        old_previous_link = retrieval_root / "previous"
        if not old_current_link.is_symlink() or not old_previous_link.is_symlink():
            raise ValueError("current and previous must both be index symlinks")
        old_current = old_current_link.readlink()
        old_previous = old_previous_link.readlink()
        status_token = _status_token(selected.runtime.credentials_file)
        if old_current.name == index_id:
            _progress("Retrieval index is already current; verifying installed files and running service.")
            validate_index_artifact(old_current_link.resolve(strict=True), expected_index_id=index_id)
            if _status(status_target, status_token).get("index_id") != index_id:
                raise RuntimeError("current link matches but running service has a different index")
            return f"Retrieval index {index_id} is already current (run {run_id}, attempt {run_attempt}, commit {commit})"
        if target.exists() or target.is_symlink():
            _progress("Retrieval index is already installed; validating existing files.")
            validate_index_artifact(target, expected_index_id=index_id)
        else:
            _progress("Installing index files and setting service account permissions.")
            identity = pwd.getpwnam(deployment.service_user)
            group = grp.getgrgid(identity.pw_gid)
            os.chown(staged, identity.pw_uid, group.gr_gid)
            os.chmod(staged, 0o550)
            for child in staged.iterdir():
                os.chown(child, identity.pw_uid, group.gr_gid)
                os.chmod(child, 0o440)
            os.replace(staged, target)
        # 先确保现有入口可验收，避免在不可观测状态下切换。
        _progress("Checking current service index before switching.")
        current_status = _status(status_target, status_token)
        if current_status.get("index_id") != old_current.name:
            raise RuntimeError("running service index does not match current link")
        try:
            _progress(f"Switching current to {index_id} and preserving the previous index.")
            publish_index(retrieval_root, index_id)
            _restart_and_wait(deployment.service, status_target, status_token, index_id)
            _progress("New index verified through authenticated local service status.")
        except Exception as exc:
            _progress("Deployment failed; restoring the original current and previous links.")
            _replace_link(old_current_link, old_current)
            _replace_link(old_previous_link, old_previous)
            try:
                _progress("Restarting the HTTP service after rollback.")
                subprocess.run(["systemctl", "restart", deployment.service], check=True)
                _progress("Waiting for the service to report the restored index.")
                _restart_status_check(status_target, status_token, old_current.name)
            except Exception as rollback_exc:
                _progress("Rollback verification failed.")
                raise RuntimeError("deployment failed and rollback verification failed") from rollback_exc
            _progress("Rollback verified; the original index is active.")
            raise RuntimeError("deployment failed; previous index restored") from exc
    return f"Retrieval index {index_id} is current (run {run_id}, attempt {run_attempt}, commit {commit})"


def _replace_link(path: Path, target: Path) -> None:
    with tempfile.TemporaryDirectory(prefix=f".{path.name}-", dir=path.parent) as temporary:
        replacement = Path(temporary) / "link"
        replacement.symlink_to(target)
        os.replace(replacement, path)


def _restart_status_check(target: LocalStatusTarget, token: str, index_id: str) -> None:
    for _ in range(12):
        try:
            if _status(target, token).get("index_id") == index_id:
                return
        except (OSError, ValueError, RuntimeError, http.client.HTTPException):
            pass
        time.sleep(2)
    raise RuntimeError("rollback index did not become ready")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deploy a retrieval index from the latest successful or configured GitHub Actions run.",
        allow_abbrev=False,
    )
    parser.add_argument("--config", type=Path, required=True, help="Deployment configuration JSON file")
    args = parser.parse_args()
    try:
        print(deploy(args.config))
    except (OSError, ValueError, RuntimeError, LexicalBuildError, urllib.error.URLError, zipfile.BadZipFile, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"Deployment failed: {exc}\n")


if __name__ == "__main__":
    main()
