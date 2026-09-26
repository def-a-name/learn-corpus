from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.maintenance import deploy_index as deployment  # noqa: E402
from src.maintenance.build_release import _validate_assets, build_release  # noqa: E402
from src.corpus.document import yaml_document  # noqa: E402
from src.retrieval.build_lexical_index import build_index, publish_index  # noqa: E402
from test_build_lexical_index import fixture_projection, make_item  # noqa: E402


COMMIT = "a" * 40
RUN_ID = 12345
RUN_ATTEMPT = 2
INDEX_ID = "idx_" + "b" * 20
CURRENT_INDEX_ID = "idx_" + "d" * 20
PREVIOUS_INDEX_ID = "idx_" + "e" * 20
DIGEST = "sha256:" + "b" * 64
DATABASE_HASH = "sha256:" + "c" * 64


def run_record(run_id: int, created_at: str = "2026-01-02T00:00:00Z") -> dict:
    return {
        "id": run_id,
        "repository": {"full_name": deployment.REPOSITORY},
        "head_branch": "main",
        "path": ".github/workflows/build-index.yml@refs/heads/main",
        "status": "completed",
        "conclusion": "success",
        "head_sha": COMMIT,
        "run_attempt": RUN_ATTEMPT,
        "created_at": created_at,
    }


def archive_bytes(*, extra: str | None = None, version: int = 2, index_prefix: str = "") -> bytes:
    metadata = {
        "version": version,
        "repository": deployment.REPOSITORY,
        "ref": deployment.REF,
        "commit": COMMIT,
        "run_id": RUN_ID,
        "run_attempt": RUN_ATTEMPT,
        "index_id": INDEX_ID,
        "source_digest": DIGEST,
        "database_sha256": DATABASE_HASH,
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("release.json", json.dumps(metadata))
        archive.writestr(f"{index_prefix}{INDEX_ID}/index.json", "{}")
        archive.writestr(f"{index_prefix}{INDEX_ID}/corpus.sqlite", b"synthetic database fixture")
        if extra is not None:
            archive.writestr(extra, b"unwanted")
    return output.getvalue()


def test_accepts_matching_run_and_artifact_metadata() -> None:
    run = run_record(RUN_ID)
    assert deployment._check_run(run, RUN_ID) == (COMMIT, RUN_ATTEMPT)
    artifact = {
        "artifacts": [{
            "name": f"learn-corpus-index-{RUN_ID}-{RUN_ATTEMPT}",
            "expired": False,
            "id": 987,
            "digest": "sha256:" + hashlib.sha256(b"fixture").hexdigest(),
            "workflow_run": {"id": RUN_ID, "head_sha": COMMIT},
        }]
    }
    assert deployment._check_artifacts(artifact, RUN_ID, RUN_ATTEMPT, COMMIT)[0] == 987
    with pytest.raises(ValueError, match="expected index artifact"):
        deployment._check_artifacts({"artifacts": []}, RUN_ID, RUN_ATTEMPT, COMMIT)
    run["head_branch"] = "test"
    with pytest.raises(ValueError, match="workflow run"):
        deployment._check_run(run, RUN_ID)


def test_latest_run_selects_newest_successful_main_build(monkeypatch) -> None:
    urls = []
    monkeypatch.setattr(deployment, "_request", lambda url, token: urls.append(url) or {
        "workflow_runs": [
            run_record(101, "2026-01-01T00:00:00Z"),
            run_record(103, "2026-01-03T00:00:00Z"),
            run_record(102, "2026-01-02T00:00:00Z"),
        ],
    })
    assert deployment._latest_run_id("synthetic-token") == 103
    assert urls == [
        f"{deployment.API_ROOT}/actions/workflows/build-index.yml/runs"
        "?branch=main&status=success&per_page=100&page=1"
    ]


def test_latest_run_checks_later_pages(monkeypatch) -> None:
    pages = []

    def request(url, token):
        pages.append(url)
        if len(pages) == 1:
            return {"workflow_runs": [run_record(index, "2026-01-01T00:00:00Z") for index in range(1, 101)]}
        return {"workflow_runs": [run_record(101, "2026-01-02T00:00:00Z")]}

    monkeypatch.setattr(deployment, "_request", request)
    assert deployment._latest_run_id("synthetic-token") == 101
    assert len(pages) == 2


def test_latest_run_requires_explicit_id_when_search_limit_is_reached(monkeypatch) -> None:
    pages = []

    def request(url, token):
        pages.append(url)
        return {"workflow_runs": [run_record(index) for index in range(1, 101)]}

    monkeypatch.setattr(deployment, "_request", request)
    with pytest.raises(ValueError, match="set run_id in deployment configuration"):
        deployment._latest_run_id("synthetic-token")
    assert len(pages) == deployment.MAX_RUN_PAGES


def test_latest_run_fails_closed_on_missing_or_invalid_listing(monkeypatch) -> None:
    monkeypatch.setattr(deployment, "_request", lambda url, token: {"workflow_runs": []})
    with pytest.raises(ValueError, match="no successful main"):
        deployment._latest_run_id("synthetic-token")
    invalid = run_record(101)
    invalid["path"] = ".github/workflows/other.yml@refs/heads/main"
    monkeypatch.setattr(deployment, "_request", lambda url, token: {"workflow_runs": [invalid]})
    with pytest.raises(ValueError, match="expected repository"):
        deployment._latest_run_id("synthetic-token")
    invalid = run_record(101, "invalid-time")
    monkeypatch.setattr(deployment, "_request", lambda url, token: {"workflow_runs": [invalid]})
    with pytest.raises(ValueError, match="creation time"):
        deployment._latest_run_id("synthetic-token")


def test_cli_only_accepts_required_config(monkeypatch, capsys, tmp_path: Path) -> None:
    selected = []
    config_path = tmp_path / "deploy.json"
    monkeypatch.setattr(deployment, "deploy", lambda path: selected.append(path) or "Synthetic deployment")
    monkeypatch.setattr(sys, "argv", ["deploy_index", "--config", str(config_path)])
    deployment.main()
    assert selected == [config_path]
    assert "Synthetic deployment" in capsys.readouterr().out
    for arguments in (
        [],
        [str(RUN_ID), "--config", str(config_path)],
        ["--config", str(config_path), "--service", "synthetic-service"],
        ["--conf", str(config_path)],
    ):
        monkeypatch.setattr(sys, "argv", ["deploy_index", *arguments])
        with pytest.raises(SystemExit, match="2"):
            deployment.main()


@pytest.mark.parametrize("run_id, expected", [
    ("latest", None), (RUN_ID, RUN_ID), (str(RUN_ID), RUN_ID),
    ("12345", 12345), ("0012345", 12345),
])
def test_deployment_config_selects_run_and_resolves_paths(tmp_path: Path, run_id, expected) -> None:
    config_path = tmp_path / "config" / "deploy.json"
    config_path.parent.mkdir()
    config_path.write_text(json.dumps({
        "service_config": "../service.json",
        "github_header_file": "header",
        "service": "synthetic-service",
        "service_user": "synthetic-user",
        "run_id": run_id,
    }))
    parsed = deployment.load_deployment_config(config_path)
    assert parsed.service_config == tmp_path / "service.json"
    assert parsed.github_header_file == config_path.parent / "header"
    assert parsed.run_id == expected
    assert parsed.run_id is None or type(parsed.run_id) is int


def test_deployment_example_points_to_unusable_header_example(tmp_path: Path) -> None:
    config = deployment.load_deployment_config(REPO_ROOT / "config" / "deploy-index.json.example")
    assert config.github_header_file == REPO_ROOT / "config" / "github.header.example"
    assert config.github_header_file.is_file()
    example_copy = tmp_path / "github-artifact.header"
    example_copy.write_bytes(config.github_header_file.read_bytes())
    example_copy.chmod(0o600)
    with pytest.raises(ValueError, match="format is invalid"):
        deployment._github_token(example_copy)


@pytest.mark.parametrize("extra, message", [
    ({"run_id": True}, "run_id"),
    ({"run_id": 0}, "run_id"),
    ({"run_id": -1}, "run_id"),
    ({"run_id": 12345.0}, "run_id"),
    ({"run_id": "0"}, "run_id"),
    ({"run_id": "000"}, "run_id"),
    ({"run_id": "-12345"}, "run_id"),
    ({"run_id": "+12345"}, "run_id"),
    ({"run_id": "12345.0"}, "run_id"),
    ({"run_id": " 12345"}, "run_id"),
    ({"run_id": "12345\n"}, "run_id"),
    ({"run_id": "１２３４５"}, "run_id"),
    ({"run_id": ""}, "run_id"),
    ({"run_id": "invalid"}, "run_id"),
    ({"status_url": "https://service.example.test/v1/status"}, "unknown fields"),
    ({"unknown": "value"}, "unknown fields"),
])
def test_deployment_config_rejects_invalid_fields(tmp_path: Path, extra, message) -> None:
    config_path = tmp_path / "deploy.json"
    config = {
        "service_config": "service.json",
        "github_header_file": "header",
        "service": "synthetic-service",
        "service_user": "synthetic-user",
    }
    config_path.write_text(json.dumps({**config, **extra}))
    with pytest.raises(ValueError, match=message):
        deployment.load_deployment_config(config_path)


@pytest.mark.parametrize("bind, expected", [("127.0.0.1", "127.0.0.1"), ("0.0.0.0", "127.0.0.1"), ("::1", "::1")])
def test_local_status_uses_service_config_and_loopback(bind, expected) -> None:
    selected = SimpleNamespace(
        host=bind, port=2699,
        runtime=SimpleNamespace(allowed_peers=(expected,), allowed_hosts=("service.example.test",)),
    )
    assert deployment._local_status_target(selected) == deployment.LocalStatusTarget(
        expected, 2699, "service.example.test"
    )
    selected.host = "192.0.2.10"
    with pytest.raises(ValueError, match="loopback"):
        deployment._local_status_target(selected)
    selected.host = bind
    selected.runtime.allowed_peers = ()
    with pytest.raises(ValueError, match="local status peer"):
        deployment._local_status_target(selected)


def test_status_connects_directly_to_loopback_with_allowed_host(monkeypatch) -> None:
    calls = []

    class FakeConnection:
        def __init__(self, host, port, timeout):
            calls.append((host, port, timeout))

        def request(self, method, path, headers):
            calls.append((method, path, headers))

        def getresponse(self):
            return SimpleNamespace(status=200, read=lambda limit: b'{"index_id":"idx_synthetic"}')

        def close(self):
            calls.append("closed")

    monkeypatch.setattr(deployment.http.client, "HTTPConnection", FakeConnection)
    target = deployment.LocalStatusTarget("127.0.0.1", 2699, "service.example.test")
    assert deployment._status(target, "synthetic-status-token") == {"index_id": "idx_synthetic"}
    assert calls[0] == ("127.0.0.1", 2699, 10)
    assert calls[1] == ("GET", "/v1/status", {
        "Host": "service.example.test",
        "Authorization": "Bearer synthetic-status-token",  # secret-scan: allow - synthetic token fixture
        "Accept": "application/json",
    })
    assert calls[2] == "closed"


def test_latest_missing_artifact_does_not_fall_back(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "retrieval"
    root.mkdir()
    (root / CURRENT_INDEX_ID).mkdir()
    (root / "current").symlink_to(CURRENT_INDEX_ID)
    monkeypatch.setattr(deployment.os, "geteuid", lambda: 0)
    monkeypatch.setattr(deployment, "load_deployment_config", lambda path: deployment.DeploymentConfig(
        service_config=tmp_path / "service.json",
        github_header_file=tmp_path / "header",
        service="synthetic-service",
        service_user="synthetic-user",
        run_id=None,
    ))
    monkeypatch.setattr(deployment, "load_service_config", lambda path: SimpleNamespace(
        transport="http",
        host="127.0.0.1",
        port=2699,
        runtime=SimpleNamespace(
            corpus_path=root, credentials_file=root / "credentials.json",
            allowed_peers=("127.0.0.1",), allowed_hosts=("service.example.test",),
        ),
    ))
    monkeypatch.setattr(deployment, "_github_token", lambda path: "synthetic-token")
    latest = run_record(RUN_ID + 1, "2026-01-03T00:00:00Z")
    older = run_record(RUN_ID, "2026-01-02T00:00:00Z")
    urls = []

    def request(url, token, *, binary=False):
        urls.append(url)
        if "/workflows/build-index.yml/runs?" in url:
            return {"workflow_runs": [older, latest]}
        if url.endswith(f"/actions/runs/{RUN_ID + 1}"):
            return latest
        if url.endswith(f"/actions/runs/{RUN_ID + 1}/artifacts?per_page=100"):
            return {"artifacts": []}
        raise AssertionError("unexpected GitHub request")

    monkeypatch.setattr(deployment, "_request", request)
    with pytest.raises(ValueError, match="expected index artifact"):
        deployment.deploy(tmp_path / "deploy.json")
    assert all(f"/actions/runs/{RUN_ID}/" not in url for url in urls)
    assert (root / "current").readlink() == Path(CURRENT_INDEX_ID)


def test_extract_rejects_extra_archive_member(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unexpected paths"):
        deployment._extract_release(
            archive_bytes(extra="../escape.txt"), tmp_path, RUN_ID, RUN_ATTEMPT, COMMIT
        )
    assert not (tmp_path.parent / "escape.txt").exists()


def test_extract_rejects_old_release_version_before_writing(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="release metadata does not match workflow run"):
        deployment._extract_release(archive_bytes(version=1), tmp_path, RUN_ID, RUN_ATTEMPT, COMMIT)
    assert not list(tmp_path.iterdir())


def test_extract_rejects_nested_index_layout_before_writing(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unexpected paths"):
        deployment._extract_release(
            archive_bytes(index_prefix="indexes/"), tmp_path, RUN_ID, RUN_ATTEMPT, COMMIT
        )
    assert not list(tmp_path.iterdir())


def test_extract_checks_release_binding_before_index_validation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        deployment,
        "validate_index_artifact",
        lambda path: {"source_digest": DIGEST, "database_sha256": DATABASE_HASH},
    )
    release, index_path = deployment._extract_release(
        archive_bytes(), tmp_path, RUN_ID, RUN_ATTEMPT, COMMIT
    )
    assert release["index_id"] == INDEX_ID
    assert index_path == tmp_path / INDEX_ID
    assert {path.name for path in index_path.iterdir()} == {"index.json", "corpus.sqlite"}


def test_cross_domain_redirect_drops_github_token() -> None:
    request = deployment.urllib.request.Request(
        f"{deployment.API_ROOT}/actions/artifacts/987/zip",
        headers={"Authorization": "Bearer synthetic-fixture"},  # secret-scan: allow - synthetic token fixture
    )
    redirected = deployment._SafeRedirect().redirect_request(
        request, None, 302, "Found", {}, "https://objects.example.test/archive.zip"
    )
    assert redirected is not None
    assert not redirected.has_header("Authorization")


def test_build_release_from_synthetic_committed_note(tmp_path: Path) -> None:
    repo = tmp_path / "synthetic-repository"
    source = repo / "sources" / "notes" / "synthetic-note.md"
    source.parent.mkdir(parents=True)
    source.write_text(
        yaml_document(
            {
                "id": "synthetic-note",
                "type": "note",
                "title": "Synthetic calibration note",
                "source_hash": "sha256:" + "d" * 64,
                "importer_version": 1,
                "layer": "note",
            },
            "# Calibration\n\nA fictional telescope is calibrated every seven days.\n",
        ),
        encoding="utf-8",
    )
    asset = repo / "sources" / "assets" / "synthetic-image.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"synthetic image fixture")
    asset_hash = hashlib.sha256(asset.read_bytes()).hexdigest()
    manifest = repo / "meta" / "manifest.json"
    manifest.parent.mkdir()
    manifest.write_text(
        json.dumps({
            "version": 2,
            "sources": {
                "synthetic-note": {
                    "output_path": "sources/notes/synthetic-note.md",
                    "source_hash": "d" * 64,
                    "importer_version": 1,
                    "ingest_status": "ready",
                    "assets": [{
                        "stored_path": "sources/assets/synthetic-image.png",
                        "asset_hash": asset_hash,
                    }],
                }
            },
        }),
        encoding="utf-8",
    )
    (repo / "meta" / "ingest.log").write_text(
        json.dumps({
            "version": 1,
            "timestamp": "2025-01-02T00:00:00Z",
            "importer": "notes",
            "changes": [
                {"action": "added", "path": "sources/notes/synthetic-note.md", "sha256": hashlib.sha256(source.read_bytes()).hexdigest()},
                {"action": "added", "path": "sources/assets/synthetic-image.png", "sha256": asset_hash},
            ],
        }) + "\n",
        encoding="utf-8",
    )
    for args in (
        ["git", "init", "-q"],
        ["git", "config", "user.name", "Synthetic Tester"],
        ["git", "config", "user.email", "synthetic@example.test"],
        ["git", "add", "sources", "meta"],
        ["git", "commit", "-qm", "Add synthetic fixture"],
    ):
        subprocess.run(args, cwd=repo, check=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    output = tmp_path / "release"
    release = build_release(
        repo, output,
        repository=deployment.REPOSITORY,
        ref=deployment.REF,
        commit=commit,
        run_id=str(RUN_ID),
        run_attempt=str(RUN_ATTEMPT),
    )
    index_path = output / release["index_id"]
    assert {path.name for path in index_path.iterdir()} == {"index.json", "corpus.sqlite"}
    assert {path.name for path in output.iterdir()} == {"release.json", release["index_id"]}
    assert json.loads((output / "release.json").read_text()) == release
    archive_output = io.BytesIO()
    with zipfile.ZipFile(archive_output, "w") as archive:
        for path in output.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(output).as_posix())
    extracted = tmp_path / "synthetic-extracted"
    extracted.mkdir()
    extracted_release, extracted_index = deployment._extract_release(
        archive_output.getvalue(), extracted, RUN_ID, RUN_ATTEMPT, commit
    )
    assert extracted_release == release
    assert (extracted_index / "corpus.sqlite").read_bytes() == (index_path / "corpus.sqlite").read_bytes()
    asset.write_bytes(b"tampered synthetic image fixture")
    with pytest.raises(ValueError, match="asset hash mismatch"):
        _validate_assets(repo)


def _stub_deployment(monkeypatch, retrieval_root: Path, *, run_id: int | None = RUN_ID) -> None:
    config = SimpleNamespace(
        transport="http",
        host="127.0.0.1",
        port=2699,
        runtime=SimpleNamespace(
            corpus_path=retrieval_root, credentials_file=retrieval_root / "credentials.json",
            allowed_peers=("127.0.0.1",), allowed_hosts=("service.example.test",),
        ),
    )
    monkeypatch.setattr(deployment.os, "geteuid", lambda: 0)
    monkeypatch.setattr(deployment, "load_deployment_config", lambda path: deployment.DeploymentConfig(
        service_config=retrieval_root / "service.json",
        github_header_file=retrieval_root / "header",
        service="synthetic-service",
        service_user="synthetic-user",
        run_id=run_id,
    ))
    monkeypatch.setattr(deployment, "load_service_config", lambda path: config)
    monkeypatch.setattr(deployment, "_github_token", lambda path: "synthetic-token")
    monkeypatch.setattr(deployment, "_request", lambda url, token, binary=False: b"archive" if binary else {})
    monkeypatch.setattr(deployment, "_check_run", lambda run, run_id: (COMMIT, RUN_ATTEMPT))
    monkeypatch.setattr(
        deployment,
        "_check_artifacts",
        lambda payload, run_id, run_attempt, commit: (987, "sha256:" + hashlib.sha256(b"archive").hexdigest()),
    )


@pytest.mark.parametrize("selected_run", [RUN_ID, None])
def test_incompatible_index_leaves_links_unchanged(tmp_path: Path, monkeypatch, selected_run, capsys) -> None:
    root = tmp_path / "retrieval"
    root.mkdir(parents=True)
    for name, index_id in (("current", CURRENT_INDEX_ID), ("previous", PREVIOUS_INDEX_ID)):
        (root / index_id).mkdir()
        (root / name).symlink_to(index_id)
    _stub_deployment(monkeypatch, root, run_id=selected_run)
    selected = []
    monkeypatch.setattr(deployment, "_latest_run_id", lambda token: selected.append(token) or RUN_ID)

    def extract(_archive, temporary, _run_id, _run_attempt, _commit):
        staged = temporary / INDEX_ID
        staged.mkdir()
        return {"index_id": INDEX_ID}, staged

    monkeypatch.setattr(deployment, "_extract_release", extract)
    monkeypatch.setattr(
        deployment,
        "validate_index_artifact",
        lambda *args, **kwargs: (_ for _ in ()).throw(deployment.LexicalBuildError("index schema_version mismatch")),
    )
    with pytest.raises(deployment.LexicalBuildError, match="schema_version"):
        deployment.deploy(tmp_path / "deploy.json")
    assert (root / "current").readlink() == Path(CURRENT_INDEX_ID)
    assert (root / "previous").readlink() == Path(PREVIOUS_INDEX_ID)
    assert selected == (["synthetic-token"] if selected_run is None else [])
    output = capsys.readouterr().out
    assert "Checking index" in output
    assert "Switching current" not in output
    assert "Restarting" not in output


@pytest.mark.parametrize("installation", ["new", "existing", "current"])
def test_deployment_reports_progress_without_credentials(tmp_path: Path, monkeypatch, capsys, installation) -> None:
    root = tmp_path / "retrieval"
    root.mkdir(parents=True)
    current = INDEX_ID if installation == "current" else CURRENT_INDEX_ID
    for name in (current, PREVIOUS_INDEX_ID):
        (root / name).mkdir(exist_ok=True)
    if installation == "existing":
        (root / INDEX_ID).mkdir()
    (root / "current").symlink_to(current)
    (root / "previous").symlink_to(PREVIOUS_INDEX_ID)
    _stub_deployment(monkeypatch, root)

    def extract(_archive, temporary, _run_id, _run_attempt, _commit):
        staged = temporary / INDEX_ID
        staged.mkdir()
        (staged / "index.json").write_text("{}")
        return {"index_id": INDEX_ID}, staged

    monkeypatch.setattr(deployment, "_extract_release", extract)
    monkeypatch.setattr(deployment, "validate_index_artifact", lambda *args, **kwargs: {})
    monkeypatch.setattr(deployment, "_status_token", lambda path: "synthetic-status-token")
    monkeypatch.setattr(deployment.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=12345, pw_gid=12345))
    monkeypatch.setattr(deployment.grp, "getgrgid", lambda gid: SimpleNamespace(gr_gid=gid))
    monkeypatch.setattr(deployment.os, "chown", lambda *args: None)
    monkeypatch.setattr(deployment.os, "chmod", lambda *args, **kwargs: None)
    monkeypatch.setattr(deployment.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    monkeypatch.setattr(deployment, "_status", lambda *args: {"index_id": (root / "current").readlink().name})

    def publish(retrieval_root, index_id):
        deployment._replace_link(retrieval_root / "previous", (retrieval_root / "current").readlink())
        deployment._replace_link(retrieval_root / "current", Path(index_id))

    monkeypatch.setattr(deployment, "publish_index", publish)
    result = deployment.deploy(tmp_path / "deploy.json")
    assert (root / "current").readlink() == Path(INDEX_ID)
    output = capsys.readouterr().out
    assert "synthetic-token" not in output
    assert "synthetic-status-token" not in output

    assert str(tmp_path) not in output
    assert f"run {RUN_ID}, attempt {RUN_ATTEMPT}, commit {COMMIT}" in output
    stages = [
        "Loading deployment", "Checking workflow run", "Checking index artifact", "Downloading artifact",
        "SHA-256 verified", "Extracting and validating", f"Checking index {INDEX_ID}",
    ]
    if installation == "current":
        stages.append("Retrieval index is already current")
        assert "already current" in result
        assert "Switching current" not in output
        assert "Restarting" not in output
    else:
        if installation == "existing":
            stages.append("Retrieval index is already installed")
        stages.append("Checking current service")
        if installation == "new":
            stages.append("Installing index")
        stages.extend([
            "Switching current", "Restarting the HTTP service",
            "Waiting for the service", "New index verified",
        ])
        assert (root / "previous").readlink() == Path(CURRENT_INDEX_ID)
    positions = [output.index(stage) for stage in stages]
    assert positions == sorted(positions)


@pytest.fixture
def ready_deployment(tmp_path: Path, monkeypatch):
    root = tmp_path / "synthetic-corpus"
    root.mkdir()
    projection = fixture_projection(make_item(
        "deployment", "note", "A fictional telescope uses a synthetic calibration index.",
        title="Synthetic deployment",
    ))
    artifact = build_index(projection, tmp_path / "synthetic-artifact")
    metadata = {
        "version": 2, "repository": deployment.REPOSITORY, "ref": deployment.REF,
        "commit": COMMIT, "run_id": RUN_ID, "run_attempt": RUN_ATTEMPT,
        "index_id": artifact.index_id, "source_digest": artifact.source_digest,
        "database_sha256": artifact.database_sha256,
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("release.json", json.dumps(metadata))
        for path in artifact.index_path.iterdir():
            archive.write(path, f"{artifact.index_id}/{path.name}")
    data = buffer.getvalue()
    _stub_deployment(monkeypatch, root)
    monkeypatch.setattr(deployment, "_request", lambda url, token, binary=False: data if binary else {})
    monkeypatch.setattr(deployment, "_check_artifacts", lambda *args: (
        987, "sha256:" + hashlib.sha256(data).hexdigest(),
    ))
    monkeypatch.setattr(deployment, "_status_token", lambda path: "synthetic-status-token")
    monkeypatch.setattr(deployment.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=12345, pw_gid=12345))
    monkeypatch.setattr(deployment.grp, "getgrgid", lambda gid: SimpleNamespace(gr_gid=gid))
    monkeypatch.setattr(deployment.os, "chown", lambda *args: None)
    monkeypatch.setattr(deployment.os, "chmod", lambda *args, **kwargs: None)
    commands = []

    def run(args, **kwargs):
        commands.append(tuple(args))
        return SimpleNamespace(returncode=0)

    def status(*args):
        assert (root / "current").is_symlink(), "Status was requested before initial publication"
        return {"index_id": (root / "current").resolve(strict=True).name}

    monkeypatch.setattr(deployment.subprocess, "run", run)
    monkeypatch.setattr(deployment, "_status", status)
    monkeypatch.setattr(deployment.time, "sleep", lambda seconds: None)
    return SimpleNamespace(
        root=root, artifact=artifact, projection=projection, commands=commands,
        config=tmp_path / "synthetic-deploy.json",
    )


def test_empty_corpus_deployment_creates_only_current(ready_deployment, capsys) -> None:
    ready = ready_deployment
    result = deployment.deploy(ready.config)
    assert {path.name for path in ready.root.iterdir()} == {INDEX_ID, "current"}
    assert (ready.root / "current").readlink() == Path(INDEX_ID)
    assert ready.commands == [("systemctl", "restart", "synthetic-service")]
    assert (ready.root / INDEX_ID / "corpus.sqlite").read_bytes() == ready.artifact.database_path.read_bytes()
    assert INDEX_ID in result
    output = capsys.readouterr().out
    assert "Preparing initial deployment" in output
    assert "Creating current" in output
    assert "Checking current service" not in output
    assert "synthetic-status-token" not in output


def test_current_only_deployment_creates_previous(ready_deployment) -> None:
    ready = ready_deployment
    original = build_index(replace(ready.projection, source_digest="sha256:" + "d" * 64), ready.root)
    publish_index(ready.root, original.index_id)
    deployment.deploy(ready.config)
    assert (ready.root / "current").readlink() == Path(INDEX_ID)
    assert (ready.root / "previous").readlink() == Path(original.index_id)
    assert original.database_path.is_file()


def test_current_only_same_index_keeps_previous_absent(ready_deployment) -> None:
    ready = ready_deployment
    shutil.copytree(ready.artifact.index_path, ready.root / INDEX_ID)
    publish_index(ready.root, INDEX_ID)
    result = deployment.deploy(ready.config)
    assert "already current" in result
    assert ready.commands == []
    assert {path.name for path in ready.root.iterdir()} == {INDEX_ID, "current"}


@pytest.mark.parametrize("phase", ["download", "digest", "extraction", "credentials"])
def test_initial_preparation_failure_keeps_corpus_empty(ready_deployment, monkeypatch, phase) -> None:
    ready = ready_deployment

    def fail(*args, **kwargs):
        raise ValueError("synthetic preparation failure")

    if phase == "download":
        monkeypatch.setattr(deployment, "_request", fail)
    elif phase == "digest":
        monkeypatch.setattr(deployment, "_check_artifacts", lambda *args: (987, "sha256:" + "0" * 64))
    elif phase == "extraction":
        def extract(data, target, *args):
            (target / "synthetic-partial-file").write_text("Synthetic partial extraction")
            fail()
        monkeypatch.setattr(deployment, "_extract_release", extract)
    else:
        monkeypatch.setattr(deployment, "_status_token", fail)
    with pytest.raises(ValueError):
        deployment.deploy(ready.config)
    assert list(ready.root.iterdir()) == []
    assert ready.commands == []


@pytest.mark.parametrize("phase", ["permissions", "installation", "publication", "restart", "verification"])
def test_initial_mutation_failure_restores_empty_corpus(ready_deployment, monkeypatch, capsys, phase) -> None:
    ready = ready_deployment

    def fail(*args, **kwargs):
        raise OSError("synthetic deployment failure")

    if phase == "permissions":
        monkeypatch.setattr(deployment.os, "chmod", fail)
    elif phase == "installation":
        real_replace = os.replace
        def replace_path(source, destination):
            if destination == ready.root / INDEX_ID:
                fail()
            return real_replace(source, destination)
        monkeypatch.setattr(deployment.os, "replace", replace_path)
    elif phase == "publication":
        def publish(root, index_id):
            publish_index(root, index_id)
            fail()
        monkeypatch.setattr(deployment, "publish_index", publish)
    elif phase == "restart":
        def run(args, **kwargs):
            ready.commands.append(tuple(args))
            if args[1] == "restart":
                fail()
            assert (ready.root / "current").is_symlink()
            return SimpleNamespace(returncode=0)
        monkeypatch.setattr(deployment.subprocess, "run", run)
    else:
        monkeypatch.setattr(deployment, "_status", fail)
    with pytest.raises(RuntimeError, match="initial deployment failed; corpus directory restored to empty"):
        deployment.deploy(ready.config)
    assert list(ready.root.iterdir()) == []
    expected = [] if phase in ("permissions", "installation", "publication") else [
        ("systemctl", "restart", "synthetic-service"),
        ("systemctl", "stop", "synthetic-service"),
    ]
    assert ready.commands == expected
    output = capsys.readouterr().out
    assert "cleanup complete; corpus directory is empty" in output
    assert "synthetic-status-token" not in output


@pytest.mark.parametrize("reuse_target", [False, True])
def test_initial_failure_preserves_preexisting_corpus_contents(ready_deployment, monkeypatch, reuse_target) -> None:
    ready = ready_deployment
    preserved = ready.root / "synthetic-preserved.txt"
    preserved.write_text("Synthetic preexisting corpus content")
    if reuse_target:
        shutil.copytree(ready.artifact.index_path, ready.root / INDEX_ID)
    before = {path.name for path in ready.root.iterdir()}
    monkeypatch.setattr(deployment, "_status", lambda *args: (_ for _ in ()).throw(RuntimeError("synthetic status failure")))
    with pytest.raises(RuntimeError, match="original corpus contents preserved"):
        deployment.deploy(ready.config)
    assert {path.name for path in ready.root.iterdir()} == before
    assert preserved.read_text() == "Synthetic preexisting corpus content"
    if reuse_target:
        assert (ready.root / INDEX_ID / "corpus.sqlite").read_bytes() == ready.artifact.database_path.read_bytes()


def test_current_only_partial_publication_restores_missing_previous(ready_deployment, monkeypatch) -> None:
    ready = ready_deployment
    original = build_index(replace(ready.projection, source_digest="sha256:" + "d" * 64), ready.root)
    publish_index(ready.root, original.index_id)
    from src.retrieval import build_lexical_index as builder
    atomic_link = builder._atomic_index_link

    def link(root, name, index_id):
        if name == "current":
            raise OSError("synthetic current link failure")
        atomic_link(root, name, index_id)

    monkeypatch.setattr(builder, "_atomic_index_link", link)
    with pytest.raises(RuntimeError, match="previous index restored"):
        deployment.deploy(ready.config)
    assert {path.name for path in ready.root.iterdir()} == {original.index_id, "current"}
    assert (ready.root / "current").readlink() == Path(original.index_id)
    assert ready.commands == []


@pytest.mark.parametrize("phase", ["service_stop", "index_cleanup"])
def test_initial_recovery_failure_is_reported(ready_deployment, monkeypatch, capsys, phase) -> None:
    ready = ready_deployment
    monkeypatch.setattr(deployment, "_status", lambda *args: (_ for _ in ()).throw(RuntimeError("synthetic status failure")))
    if phase == "service_stop":
        def run(args, **kwargs):
            ready.commands.append(tuple(args))
            if args[1] == "stop":
                raise OSError("synthetic stop failure")
            return SimpleNamespace(returncode=0)
        monkeypatch.setattr(deployment.subprocess, "run", run)
    else:
        real_rmtree = shutil.rmtree
        def remove(path, *args, **kwargs):
            if Path(path) == ready.root / INDEX_ID:
                raise OSError("synthetic cleanup failure")
            return real_rmtree(path, *args, **kwargs)
        monkeypatch.setattr(deployment.shutil, "rmtree", remove)
    with pytest.raises(RuntimeError, match="rollback or cleanup failed: " + phase):
        deployment.deploy(ready.config)
    assert not os.path.lexists(ready.root / "current")
    assert not os.path.lexists(ready.root / "previous")
    assert {path.name for path in ready.root.iterdir()} == ({INDEX_ID} if phase == "index_cleanup" else set())
    output = capsys.readouterr().out
    assert "cleanup complete" not in output
    assert "Rollback verified" not in output


@pytest.mark.parametrize("state", ["previous_only", "regular", "dangling", "loop", "outside", "nested", "invalid_name"])
def test_invalid_publication_state_is_rejected_before_download(ready_deployment, monkeypatch, state) -> None:
    ready = ready_deployment
    current = ready.root / "current"
    if state == "previous_only":
        (ready.root / INDEX_ID).mkdir()
        (ready.root / "previous").symlink_to(INDEX_ID)
    elif state == "regular":
        current.write_text("Synthetic regular file")
    elif state == "dangling":
        current.symlink_to(INDEX_ID)
    elif state == "loop":
        current.symlink_to("current")
    else:
        target = {
            "outside": ready.artifact.index_path,
            "nested": ready.root / "synthetic-nested" / INDEX_ID,
            "invalid_name": ready.root / "synthetic-unrelated",
        }[state]
        target.mkdir(parents=True, exist_ok=True)
        current.symlink_to(target)
    before = {path.name for path in ready.root.iterdir()}
    raw_link = current.readlink() if current.is_symlink() else None
    monkeypatch.setattr(deployment, "_request", lambda *args, **kwargs: pytest.fail("Unexpected download"))
    with pytest.raises(ValueError, match="deployment"):
        deployment.deploy(ready.config)
    assert {path.name for path in ready.root.iterdir()} == before
    if raw_link is not None:
        assert current.readlink() == raw_link
    assert ready.commands == []


@pytest.mark.parametrize("with_previous", [False, True])
@pytest.mark.parametrize("reuse_target", [False, True])
def test_failed_update_restores_exact_links_and_preserves_existing_indexes(
    ready_deployment, monkeypatch, with_previous, reuse_target,
) -> None:
    ready = ready_deployment
    original = build_index(replace(ready.projection, source_digest="sha256:" + "d" * 64), ready.root)
    (ready.root / "current").symlink_to(original.index_path)
    if with_previous:
        older = build_index(replace(ready.projection, source_digest="sha256:" + "e" * 64), ready.root)
        (ready.root / "previous").symlink_to(older.index_path)
    if reuse_target:
        shutil.copytree(ready.artifact.index_path, ready.root / INDEX_ID)
    before = {path.name for path in ready.root.iterdir()}
    previous = (ready.root / "previous").readlink() if with_previous else None

    def status(*args):
        index_id = (ready.root / "current").resolve(strict=True).name
        if index_id == INDEX_ID:
            raise RuntimeError("synthetic new index status failure")
        return {"index_id": index_id}

    monkeypatch.setattr(deployment, "_status", status)
    with pytest.raises(RuntimeError, match="previous index restored"):
        deployment.deploy(ready.config)
    assert {path.name for path in ready.root.iterdir()} == before
    assert (ready.root / "current").readlink() == original.index_path
    if with_previous:
        assert (ready.root / "previous").readlink() == previous
    else:
        assert not os.path.lexists(ready.root / "previous")
    assert ready.commands == [("systemctl", "restart", "synthetic-service")] * 2
    if reuse_target:
        assert (ready.root / INDEX_ID / "corpus.sqlite").read_bytes() == ready.artifact.database_path.read_bytes()


@pytest.mark.parametrize("failure", ["unavailable", "mismatch"])
def test_update_checks_current_service_before_installing(ready_deployment, monkeypatch, failure) -> None:
    ready = ready_deployment
    original = build_index(replace(ready.projection, source_digest="sha256:" + "d" * 64), ready.root)
    publish_index(ready.root, original.index_id)

    def status(*args):
        if failure == "unavailable":
            raise OSError("synthetic current service unavailable")
        return {"index_id": INDEX_ID}

    monkeypatch.setattr(deployment, "_status", status)
    with pytest.raises((OSError, RuntimeError)):
        deployment.deploy(ready.config)
    assert {path.name for path in ready.root.iterdir()} == {original.index_id, "current"}
    assert (ready.root / "current").readlink() == Path(original.index_id)
    assert ready.commands == []


def test_initial_failure_cli_reports_empty_corpus_and_nonzero_exit(ready_deployment, monkeypatch, capsys) -> None:
    ready = ready_deployment
    monkeypatch.setattr(deployment, "_status", lambda *args: (_ for _ in ()).throw(RuntimeError("synthetic status failure")))
    monkeypatch.setattr(sys, "argv", ["deploy_index", "--config", str(ready.config)])
    with pytest.raises(SystemExit) as exit_info:
        deployment.main()
    assert exit_info.value.code == 1
    assert list(ready.root.iterdir()) == []
    output = capsys.readouterr()
    assert "Deployment failed: initial deployment failed; corpus directory restored to empty" in output.err
    assert "synthetic-status-token" not in output.out + output.err


@pytest.mark.parametrize("phase", ["current_restore", "previous_restore", "service_restart"])
def test_update_recovery_failure_retains_installed_index(ready_deployment, monkeypatch, capsys, phase) -> None:
    ready = ready_deployment
    original = build_index(replace(ready.projection, source_digest="sha256:" + "d" * 64), ready.root)
    publish_index(ready.root, original.index_id)
    restore_link = deployment._restore_link

    def status(*args):
        index_id = (ready.root / "current").resolve(strict=True).name
        if index_id == INDEX_ID:
            raise RuntimeError("synthetic new index status failure")
        return {"index_id": index_id}

    def restore(path, target):
        if path.name + "_restore" == phase:
            raise OSError("synthetic link restoration failure")
        restore_link(path, target)

    def run(args, **kwargs):
        ready.commands.append(tuple(args))
        if phase == "service_restart" and len(ready.commands) == 2:
            raise OSError("synthetic rollback restart failure")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(deployment, "_status", status)
    monkeypatch.setattr(deployment, "_restore_link", restore)
    monkeypatch.setattr(deployment.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="rollback or cleanup failed: " + phase):
        deployment.deploy(ready.config)
    assert (ready.root / INDEX_ID / "corpus.sqlite").read_bytes() == ready.artifact.database_path.read_bytes()
    assert original.database_path.is_file()
    assert (ready.root / "current").readlink() == Path(INDEX_ID if phase == "current_restore" else original.index_id)
    assert os.path.lexists(ready.root / "previous") is (phase == "previous_restore")
    assert len(ready.commands) == (2 if phase == "service_restart" else 1)
    output = capsys.readouterr().out
    assert "Rollback incomplete; retaining newly installed index files" in output
    assert "Rollback verified" not in output


@pytest.mark.parametrize("rollback_fails", [False, True])
@pytest.mark.parametrize("with_previous", [False, True])
def test_failed_restart_restores_previous_links(tmp_path: Path, monkeypatch, capsys, rollback_fails, with_previous) -> None:
    root = tmp_path / "retrieval"
    root.mkdir(parents=True)
    for name in (CURRENT_INDEX_ID, PREVIOUS_INDEX_ID, INDEX_ID):
        (root / name).mkdir()
    (root / "current").symlink_to(CURRENT_INDEX_ID)
    if with_previous:
        (root / "previous").symlink_to(PREVIOUS_INDEX_ID)
    _stub_deployment(monkeypatch, root)

    def extract(_archive, temporary, _run_id, _run_attempt, _commit):
        staged = temporary / INDEX_ID
        staged.mkdir()
        return {"index_id": INDEX_ID}, staged

    monkeypatch.setattr(deployment, "_extract_release", extract)
    monkeypatch.setattr(deployment, "validate_index_artifact", lambda *args, **kwargs: {})
    monkeypatch.setattr(deployment, "_status_token", lambda path: "synthetic-status-token")
    monkeypatch.setattr(deployment, "_status", lambda *args: {"index_id": CURRENT_INDEX_ID})
    monkeypatch.setattr(
        "src.retrieval.build_lexical_index.validate_index_artifact",
        lambda path: {"index_id": path.name},
    )
    restarted = []

    def fail_restart(*args):
        restarted.append((root / "current").readlink())
        raise RuntimeError("synthetic restart failure")

    monkeypatch.setattr(deployment, "_restart_and_wait", fail_restart)
    monkeypatch.setattr(deployment.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    def rollback_check(*args):
        if rollback_fails:
            raise RuntimeError("synthetic rollback failure")

    monkeypatch.setattr(deployment, "_restart_status_check", rollback_check)
    expected = "rollback or cleanup failed: service_verification" if rollback_fails else "previous index restored"
    with pytest.raises(RuntimeError, match=expected):
        deployment.deploy(tmp_path / "deploy.json")
    assert restarted == [Path(INDEX_ID)]
    assert (root / "current").readlink() == Path(CURRENT_INDEX_ID)
    if with_previous:
        assert (root / "previous").readlink() == Path(PREVIOUS_INDEX_ID)
    else:
        assert not os.path.lexists(root / "previous")
    output = capsys.readouterr().out
    assert "Deployment failed; restoring" in output
    assert "Restarting the HTTP service after rollback" in output
    assert "Waiting for the service to report the restored index" in output
    assert "New index verified" not in output
    assert ("Rollback verified" in output) is not rollback_fails
    assert ("Rollback or cleanup failed" in output) is rollback_fails
    assert "synthetic-token" not in output
    assert "synthetic-status-token" not in output
