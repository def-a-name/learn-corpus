#!/usr/bin/env python3
"""扫描即将进入 Git 的文本文件，只报告位置和类型，不回显可疑值。"""

from __future__ import annotations

import argparse
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from scripts.common.wiki_core import REPO_ROOT


ALLOW_MARKER = "secret-scan: allow"
PLACEHOLDERS = {
    "[redacted]",
    "[masked]",
    "redacted",
    "masked",
    "changeme",
    "example",
    "placeholder",
    "\\",
}
PATTERNS = (
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\b(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,})\b")),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("bearer_token", re.compile(r"(?i)\bBearer\s+([A-Za-z0-9._~+/=-]{12,})")),
    (
        "credential_assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|"
            r"client[_-]?secret|private[_-]?key)\b[\"']?\s*[:=]\s*[\"']?([^\s\"'`,;}{]+)"
        ),
    ),
    (
        "chinese_credential_assignment",
        re.compile(
            r"(?i)(?:密码|口令)\s*(?::|：|=|是|为|\s)\s*"
            r"((?=[^\s\"'`,，。；;}{\]]*\d)[^\s\"'`,，。；;}{\]]+)"
        ),
    ),
)


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    kind: str


def git_candidate_files(repo_root: Path = REPO_ROOT) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    return [repo_root / value.decode("utf-8") for value in result.stdout.split(b"\0") if value]


def _is_placeholder(match: re.Match[str]) -> bool:
    value = match.group(match.lastindex or 0).strip().lower()
    return value in PLACEHOLDERS or value.startswith("[redacted") or value.startswith("[masked")


def scan_paths(paths: Iterable[Path], repo_root: Path = REPO_ROOT) -> list[Finding]:
    findings: list[Finding] = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for line_number, line in enumerate(text.splitlines(), 1):
            if ALLOW_MARKER in line:
                continue
            for kind, pattern in PATTERNS:
                match = pattern.search(line)
                if match and not _is_placeholder(match):
                    try:
                        display_path = path.relative_to(repo_root)
                    except ValueError:
                        display_path = path
                    findings.append(Finding(display_path, line_number, kind))
    return findings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, help="可选；默认扫描 Git 候选文件")
    args = parser.parse_args()
    paths = args.paths or git_candidate_files()
    findings = scan_paths(paths)
    for finding in findings:
        print(f"{finding.path}:{finding.line}: {finding.kind}")
    print(f"Secrets scan: files={len(paths)}, findings={len(findings)}")
    raise SystemExit(1 if findings else 0)


if __name__ == "__main__":
    main()
