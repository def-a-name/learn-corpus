"""使用独立 SQLite 保存 MCP 检索任务的机械执行事实。"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
from datetime import datetime, timezone
from uuid import uuid4

from src.retrieval.contracts import SUPPORTED_SCOPES
from src.service.ledger_config import LedgerConfig, TaskLimitsConfig


SCHEMA_VERSION = 2
# 固定响应最多 65,536 bytes；任何 call/item 对象都远大于 16 bytes，
# 因而取最近 4,096+1 条足以覆盖所有可能进入单次响应的完整条目。
DETAIL_CANDIDATE_LIMIT = 4096

_TASK_ID = re.compile(r"tsk_[0-9a-f]{32}")
_CALL_ID = re.compile(r"req_[A-Za-z0-9_-]{1,64}")
_ITEM_ID = re.compile(r"itm_[a-z2-7]{32}")
_BUNDLE_KEY = re.compile(r"bnd_[0-9a-f]{40}")
_GENERATION = re.compile(r"gen_[0-9a-f]{20}")
_OWNER_KEY = re.compile(r"[A-Za-z0-9_-]{1,128}")

_DDL = """
CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY,
    owner_key TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('active', 'blocked')),
    generation TEXT,
    search_limit INTEGER NOT NULL CHECK (search_limit > 0),
    read_limit INTEGER NOT NULL CHECK (read_limit > 0),
    evidence_token_limit INTEGER NOT NULL CHECK (evidence_token_limit > 0),
    created_at TEXT NOT NULL,
    blocked_category TEXT
) STRICT;

CREATE TABLE calls (
    call_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    sequence_number INTEGER NOT NULL CHECK (sequence_number > 0),
    operation TEXT NOT NULL CHECK (operation IN ('search', 'read_bundle')),
    queries_json TEXT,
    scopes_json TEXT,
    result_limit INTEGER,
    search_key TEXT,
    cap INTEGER NOT NULL CHECK (cap > 0),
    state TEXT NOT NULL CHECK (state IN ('pending', 'succeeded', 'failed', 'uncertain')),
    usage INTEGER CHECK (usage IS NULL OR usage >= 0),
    estimator_version TEXT,
    error_category TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    seed_item_id TEXT,
    bundle_key TEXT,
    bundle_status TEXT,
    membership_complete INTEGER CHECK (membership_complete IS NULL OR membership_complete IN (0, 1)),
    missing_item_ids_json TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id),
    UNIQUE (task_id, sequence_number),
    UNIQUE (task_id, call_id)
) STRICT;

CREATE TABLE task_items (
    task_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_title TEXT,
    heading_path_json TEXT,
    path TEXT NOT NULL,
    locator TEXT NOT NULL,
    role TEXT,
    evidence_role TEXT,
    turn_index INTEGER,
    bundle_key TEXT NOT NULL,
    PRIMARY KEY (task_id, item_id),
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
) STRICT;

CREATE TABLE call_items (
    call_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    PRIMARY KEY (call_id, item_id),
    FOREIGN KEY (task_id, call_id) REFERENCES calls(task_id, call_id),
    FOREIGN KEY (task_id, item_id) REFERENCES task_items(task_id, item_id)
) STRICT;

CREATE UNIQUE INDEX calls_one_unresolved
ON calls(task_id) WHERE state IN ('pending', 'uncertain');
CREATE UNIQUE INDEX calls_search_once
ON calls(task_id, search_key) WHERE operation = 'search';
CREATE UNIQUE INDEX calls_seed_once
ON calls(task_id, seed_item_id) WHERE operation = 'read_bundle';
CREATE INDEX calls_task_operation ON calls(task_id, operation);
CREATE INDEX task_items_bundle ON task_items(task_id, bundle_key);
CREATE INDEX call_items_task_item ON call_items(task_id, item_id);
"""


class LedgerFailure(RuntimeError):
    """只携带可公开的固定账本错误类别。"""

    def __init__(self, code: str, *, details: dict[str, object] | None = None):
        super().__init__(code)
        self.code = code
        self.details = details


class _IntegrityFailure(RuntimeError):
    """表示 core 结果与已记录事实不一致。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _compact(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _valid_owner(owner_key: object) -> bool:
    return isinstance(owner_key, str) and _OWNER_KEY.fullmatch(owner_key) is not None


def _metadata(item: object, bundle_key: str) -> tuple:
    """验证并提取不含正文和 snippet 的引用元数据。"""

    if not isinstance(item, dict):
        raise _IntegrityFailure
    required = {
        "item_id", "source_type", "source_title", "heading_path", "path", "locator",
        "role", "evidence_role", "turn_index",
    }
    if not required <= item.keys():
        raise _IntegrityFailure
    item_id = item["item_id"]
    source_type = item["source_type"]
    source_title = item["source_title"]
    heading_path = item["heading_path"]
    path = item["path"]
    locator = item["locator"]
    role = item["role"]
    evidence_role = item["evidence_role"]
    turn_index = item["turn_index"]
    if (
        not isinstance(item_id, str) or _ITEM_ID.fullmatch(item_id) is None
        or source_type not in SUPPORTED_SCOPES
        or not isinstance(path, str) or not path.startswith(f"sources/{source_type}s/")
        or ".." in PurePosixPath(path).parts or "\\" in path
        or any(ord(value) < 32 for value in path)
        or not isinstance(locator, str) or not locator
        or _BUNDLE_KEY.fullmatch(bundle_key) is None
    ):
        raise _IntegrityFailure
    if source_type == "conversation":
        expected = "user_statement" if role == "human" else "assistant_suggestion"
        if (
            not isinstance(source_title, str) or not source_title.strip()
            or heading_path is not None
            or role not in {"human", "assistant"} or evidence_role != expected
        ):
            raise _IntegrityFailure
        if type(turn_index) is not int or turn_index < 0:
            raise _IntegrityFailure
    elif (
        not isinstance(source_title, str) or not source_title.strip()
        or not isinstance(heading_path, list)
        or any(not isinstance(value, str) for value in heading_path)
        or role is not None or turn_index is not None
        or (source_type == "article" and evidence_role != "external_source")
        or (source_type == "note" and evidence_role is not None
            and not isinstance(evidence_role, str))
    ):
        raise _IntegrityFailure
    return (
        item_id, source_type, source_title,
        None if heading_path is None else _compact(heading_path),
        path, locator, role, evidence_role, turn_index, bundle_key,
    )


def _attach_source_locations(
    items: list[object], source_locations: tuple[dict[str, str], ...] | None,
) -> list[object]:
    """将未公开的来源定位合并到服务端结算副本。"""

    if source_locations is None:
        return items
    locations: dict[str, dict[str, str]] = {}
    for value in source_locations:
        if (
            not isinstance(value, dict)
            or set(value) != {"item_id", "path", "locator"}
            or not isinstance(value["item_id"], str)
            or value["item_id"] in locations
            or not isinstance(value["path"], str)
            or not isinstance(value["locator"], str)
        ):
            raise _IntegrityFailure
        locations[value["item_id"]] = value
    item_ids = {
        item.get("item_id") for item in items if isinstance(item, dict)
    }
    if len(item_ids) != len(items) or item_ids != set(locations):
        raise _IntegrityFailure
    return [
        {**item, "path": locations[item["item_id"]]["path"],
         "locator": locations[item["item_id"]]["locator"]}
        for item in items if isinstance(item, dict)
    ]


class ExecutionLedgerStore:
    """每次操作使用短连接和短事务，允许多个服务进程共享数据库。"""

    def __init__(self, config: LedgerConfig):
        self.config = config

    @classmethod
    def open(cls, config: LedgerConfig) -> "ExecutionLedgerStore":
        """受控创建或验证账本文件，不静默重建未知 schema。"""

        path = config.path
        try:
            parent = path.parent
            parent_stat = parent.stat()
            if not stat.S_ISDIR(parent_stat.st_mode) or parent_stat.st_mode & 0o077:
                raise ValueError("ledger directory permissions are invalid")
            if path.exists():
                path_stat = path.lstat()
                if not stat.S_ISREG(path_stat.st_mode) or path_stat.st_mode & 0o077:
                    raise ValueError("ledger file permissions are invalid")
            else:
                try:
                    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except FileExistsError:
                    descriptor = None
                if descriptor is not None:
                    os.close(descriptor)
            path_stat = path.lstat()
            if not stat.S_ISREG(path_stat.st_mode) or path_stat.st_mode & 0o077:
                raise ValueError("ledger file permissions are invalid")
            store = cls(config)
            with store._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                tables = connection.execute(
                    "SELECT COUNT(*) FROM sqlite_schema "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                ).fetchone()[0]
                if version == 0 and tables == 0:
                    for statement in _DDL.split(";"):
                        if statement.strip():
                            connection.execute(statement)
                    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                elif version == 1:
                    connection.execute("ALTER TABLE task_items ADD COLUMN source_title TEXT")
                    connection.execute("ALTER TABLE task_items ADD COLUMN heading_path_json TEXT")
                    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                elif version != SCHEMA_VERSION:
                    raise ValueError("ledger schema version is unsupported")
                connection.commit()
            os.chmod(path, 0o600)
            return store
        except (OSError, sqlite3.Error) as exc:
            raise ValueError("cannot open execution ledger") from exc

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(
            self.config.path, timeout=self.config.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.config.busy_timeout_ms}")
        mode = connection.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
        if str(mode).lower() != "delete":
            connection.close()
            raise sqlite3.OperationalError("journal mode is unavailable")
        try:
            yield connection
        finally:
            connection.close()

    def close(self) -> None:
        """短连接模式没有常驻连接需要关闭。"""

    def _task(self, connection: sqlite3.Connection, owner_key: str, task_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None or row["owner_key"] != owner_key:
            raise LedgerFailure("task_not_found")
        return row

    def _summary(
        self, connection: sqlite3.Connection, task: sqlite3.Row, call_id: str | None = None,
    ) -> dict:
        values = connection.execute(
            """
            SELECT
                COALESCE(SUM(operation = 'search'), 0) AS search_calls,
                COALESCE(SUM(operation = 'read_bundle'), 0) AS read_calls,
                COALESCE(SUM(CASE WHEN usage IS NOT NULL THEN usage ELSE 0 END), 0) AS known_usage,
                COALESCE(SUM(CASE WHEN state IN ('pending', 'uncertain') AND usage IS NULL
                                  THEN cap ELSE 0 END), 0) AS reserved_tokens,
                COALESCE(SUM(operation = 'read_bundle' AND state = 'succeeded'
                             AND bundle_status != 'complete'), 0) AS partial_windows,
                COALESCE(SUM(state IN ('pending', 'uncertain')), 0) AS unresolved_calls
            FROM calls WHERE task_id = ?
            """,
            (task["task_id"],),
        ).fetchone()
        known = values["known_usage"]
        reserved = values["reserved_tokens"]
        result = {
            "task_id": task["task_id"],
            "call_id": call_id,
            "task_state": task["state"],
            "generation": task["generation"],
            "search_calls": values["search_calls"],
            "read_calls": values["read_calls"],
            "estimated_evidence_tokens": known,
            "reserved_estimated_tokens": reserved,
            "available_estimated_tokens": task["evidence_token_limit"] - known - reserved,
            "partial_windows": values["partial_windows"],
            "unresolved_calls": values["unresolved_calls"],
        }
        return result

    def _database_bytes(self, connection: sqlite3.Connection) -> int:
        pages = connection.execute("PRAGMA page_count").fetchone()[0]
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        return pages * page_size

    def create_task(
        self, owner_key: str, limits: TaskLimitsConfig | None = None,
    ) -> dict:
        if not _valid_owner(owner_key):
            raise LedgerFailure("ledger_unavailable")
        limits = limits or TaskLimitsConfig()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                task_count = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
                if task_count >= self.config.max_tasks or self._database_bytes(connection) >= self.config.max_bytes:
                    connection.rollback()
                    raise LedgerFailure("ledger_capacity_exceeded")
                task_id = "tsk_" + uuid4().hex
                connection.execute(
                    "INSERT INTO tasks VALUES (?, ?, 'active', NULL, ?, ?, ?, ?, NULL)",
                    (
                        task_id, owner_key, limits.search_calls, limits.read_calls,
                        limits.estimated_evidence_tokens, _now(),
                    ),
                )
                task = self._task(connection, owner_key, task_id)
                summary = self._summary(connection, task)
                connection.commit()
                return {
                    "task_id": task_id,
                    "task_state": "active",
                    "limits": {
                        "search_calls": limits.search_calls,
                        "read_calls": limits.read_calls,
                        "estimated_evidence_tokens": limits.estimated_evidence_tokens,
                    },
                    "execution": summary,
                }
        except LedgerFailure:
            raise
        except sqlite3.Error as exc:
            raise LedgerFailure("ledger_unavailable") from exc

    def _admission_common(
        self, connection: sqlite3.Connection, owner_key: str, task_id: str,
        operation: str, cap: int, current_generation: str,
    ) -> tuple[sqlite3.Row, int]:
        task = self._task(connection, owner_key, task_id)
        if task["state"] != "active":
            raise LedgerFailure(
                "task_blocked",
                details={
                    "blocked_category": task["blocked_category"] or "unknown",
                    "retryable": False,
                },
            )
        unresolved = connection.execute(
            "SELECT 1 FROM calls WHERE task_id = ? AND state IN ('pending', 'uncertain')",
            (task_id,),
        ).fetchone()
        if unresolved:
            raise LedgerFailure("task_busy")
        if task["generation"] is not None and task["generation"] != current_generation:
            connection.execute(
                "UPDATE tasks SET state = 'blocked', blocked_category = 'generation_mismatch' WHERE task_id = ?",
                (task_id,),
            )
            connection.commit()
            raise LedgerFailure(
                "task_blocked",
                details={"blocked_category": "generation_mismatch", "retryable": False},
            )
        count = connection.execute(
            "SELECT COUNT(*) FROM calls WHERE task_id = ? AND operation = ?",
            (task_id, operation),
        ).fetchone()[0]
        limit = task["search_limit"] if operation == "search" else task["read_limit"]
        if count >= limit:
            raise LedgerFailure(
                "task_call_limit_exceeded",
                details={"operation": operation, "used": count, "limit": limit},
            )
        summary = self._summary(connection, task)
        if cap > summary["available_estimated_tokens"]:
            raise LedgerFailure(
                "task_budget_exceeded",
                details={
                    "requested_estimated_tokens": cap,
                    "available_estimated_tokens": summary["available_estimated_tokens"],
                    "task_limit": task["evidence_token_limit"],
                },
            )
        return task, count + 1

    def admit_search(
        self, owner_key: str, task_id: str, call_id: str, *, queries: tuple[str, ...],
        query_keys: tuple[str, ...], scopes: tuple[str, ...], result_limit: int,
        cap: int, current_generation: str,
    ) -> dict:
        if not isinstance(call_id, str) or _CALL_ID.fullmatch(call_id) is None:
            raise LedgerFailure("ledger_unavailable")
        search_key = _compact([sorted(query_keys), sorted(scopes)])
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                task, _ = self._admission_common(
                    connection, owner_key, task_id, "search", cap, current_generation,
                )
                if connection.execute(
                    "SELECT 1 FROM calls WHERE task_id = ? AND operation = 'search' AND search_key = ?",
                    (task_id, search_key),
                ).fetchone():
                    raise LedgerFailure("search_already_attempted")
                sequence = connection.execute(
                    "SELECT COALESCE(MAX(sequence_number), 0) + 1 FROM calls WHERE task_id = ?",
                    (task_id,),
                ).fetchone()[0]
                connection.execute(
                    """
                    INSERT INTO calls (
                        call_id, task_id, sequence_number, operation, queries_json, scopes_json,
                        result_limit, search_key, cap, state, started_at
                    ) VALUES (?, ?, ?, 'search', ?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (call_id, task_id, sequence, _compact(queries), _compact(scopes),
                     result_limit, search_key, cap, _now()),
                )
                connection.commit()
                return {"generation": task["generation"]}
        except LedgerFailure:
            raise
        except sqlite3.IntegrityError as exc:
            raise LedgerFailure("task_busy") from exc
        except sqlite3.Error as exc:
            raise LedgerFailure("ledger_unavailable") from exc

    def admit_read(
        self, owner_key: str, task_id: str, call_id: str, *, seed_item_id: str,
        cap: int, current_generation: str,
    ) -> dict:
        if not isinstance(call_id, str) or _CALL_ID.fullmatch(call_id) is None:
            raise LedgerFailure("ledger_unavailable")
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                task, _ = self._admission_common(
                    connection, owner_key, task_id, "read_bundle", cap, current_generation,
                )
                candidate = connection.execute(
                    """
                    SELECT item.bundle_key
                    FROM task_items AS item
                    WHERE item.task_id = ? AND item.item_id = ? AND EXISTS (
                        SELECT 1 FROM call_items AS relation
                        JOIN calls AS call ON call.call_id = relation.call_id
                        WHERE relation.task_id = item.task_id AND relation.item_id = item.item_id
                          AND call.operation = 'search' AND call.state = 'succeeded'
                    )
                    """,
                    (task_id, seed_item_id),
                ).fetchone()
                if candidate is None or task["generation"] is None:
                    raise LedgerFailure("seed_not_available")
                if connection.execute(
                    "SELECT 1 FROM calls WHERE task_id = ? AND operation = 'read_bundle' AND seed_item_id = ?",
                    (task_id, seed_item_id),
                ).fetchone():
                    raise LedgerFailure("seed_already_attempted")
                sequence = connection.execute(
                    "SELECT COALESCE(MAX(sequence_number), 0) + 1 FROM calls WHERE task_id = ?",
                    (task_id,),
                ).fetchone()[0]
                connection.execute(
                    """
                    INSERT INTO calls (
                        call_id, task_id, sequence_number, operation, cap, state, started_at,
                        seed_item_id, bundle_key
                    ) VALUES (?, ?, ?, 'read_bundle', ?, 'pending', ?, ?, ?)
                    """,
                    (call_id, task_id, sequence, cap, _now(), seed_item_id, candidate["bundle_key"]),
                )
                connection.commit()
                return {"generation": task["generation"], "bundle_key": candidate["bundle_key"]}
        except LedgerFailure:
            raise
        except sqlite3.IntegrityError as exc:
            raise LedgerFailure("task_busy") from exc
        except sqlite3.Error as exc:
            raise LedgerFailure("ledger_unavailable") from exc

    def _record_item(
        self, connection: sqlite3.Connection, task_id: str, call_id: str,
        item: object, bundle_key: str,
    ) -> None:
        values = _metadata(item, bundle_key)
        existing = connection.execute(
            """
            SELECT item_id, source_type, source_title, heading_path_json, path, locator,
                   role, evidence_role, turn_index, bundle_key
            FROM task_items WHERE task_id = ? AND item_id = ?
            """,
            (task_id, values[0]),
        ).fetchone()
        if existing is not None and tuple(existing) != values:
            raise _IntegrityFailure
        if existing is None:
            connection.execute(
                """
                INSERT INTO task_items (
                    task_id, item_id, source_type, source_title, heading_path_json,
                    path, locator, role, evidence_role, turn_index, bundle_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (task_id, *values),
            )
        connection.execute(
            "INSERT INTO call_items VALUES (?, ?, ?)", (call_id, values[0], task_id),
        )

    def finalize_success(
        self, call_id: str, response: dict,
        *, source_locations: tuple[dict[str, str], ...] | None = None,
    ) -> dict:
        """从同一份 core payload 结算一次调用并返回派生摘要。"""

        usage_value = response.get("usage") if isinstance(response, dict) else None
        usage = usage_value.get("estimated_evidence_tokens") if isinstance(usage_value, dict) else None
        estimator = usage_value.get("estimator_version") if isinstance(usage_value, dict) else None
        if type(usage) is not int or usage < 0 or not isinstance(estimator, str) or not estimator:
            self.finalize_failure(call_id, "ledger_integrity_error", block=True)
            raise LedgerFailure("task_blocked")
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                call = connection.execute("SELECT * FROM calls WHERE call_id = ?", (call_id,)).fetchone()
                if call is None or call["state"] != "pending":
                    raise _IntegrityFailure
                task = connection.execute(
                    "SELECT * FROM tasks WHERE task_id = ?", (call["task_id"],),
                ).fetchone()
                generation = response.get("generation")
                if (
                    response.get("request_id") != call_id
                    or not isinstance(generation, str) or _GENERATION.fullmatch(generation) is None
                    or task["generation"] is not None and task["generation"] != generation
                ):
                    raise _IntegrityFailure
                if call["operation"] == "search":
                    results = response.get("results")
                    if not isinstance(results, list):
                        raise _IntegrityFailure
                    results = _attach_source_locations(results, source_locations)
                    for item in results:
                        bundle_key = item.get("bundle_key") if isinstance(item, dict) else None
                        if not isinstance(bundle_key, str):
                            raise _IntegrityFailure
                        self._record_item(connection, call["task_id"], call_id, item, bundle_key)
                else:
                    items = response.get("items")
                    missing = response.get("missing_item_ids")
                    if not isinstance(items, list) or not isinstance(missing, list):
                        raise _IntegrityFailure
                    items = _attach_source_locations(items, source_locations)
                    returned = [item.get("item_id") for item in items if isinstance(item, dict)]
                    if (
                        response.get("seed_item_id") != call["seed_item_id"]
                        or response.get("bundle_key") != call["bundle_key"]
                        or call["seed_item_id"] not in returned
                        or response.get("bundle_status") not in {
                            "complete", "partial_budget", "partial_error",
                        }
                        or type(response.get("membership_complete")) is not bool
                        or len(set(missing)) != len(missing)
                        or any(not isinstance(value, str) or _ITEM_ID.fullmatch(value) is None
                               for value in missing)
                    ):
                        raise _IntegrityFailure
                    for item in items:
                        self._record_item(
                            connection, call["task_id"], call_id, item, call["bundle_key"],
                        )
                    connection.execute(
                        """
                        UPDATE calls SET bundle_status = ?, membership_complete = ?,
                                         missing_item_ids_json = ?
                        WHERE call_id = ?
                        """,
                        (response["bundle_status"], int(response["membership_complete"]),
                         _compact(missing), call_id),
                    )
                connection.execute(
                    "UPDATE tasks SET generation = COALESCE(generation, ?) WHERE task_id = ?",
                    (generation, call["task_id"]),
                )
                connection.execute(
                    """
                    UPDATE calls SET state = 'succeeded', usage = ?, estimator_version = ?,
                                     finished_at = ? WHERE call_id = ? AND state = 'pending'
                    """,
                    (usage, estimator, _now(), call_id),
                )
                task = connection.execute(
                    "SELECT * FROM tasks WHERE task_id = ?", (call["task_id"],),
                ).fetchone()
                summary = self._summary(connection, task, call_id)
                if usage > call["cap"] or summary["available_estimated_tokens"] < 0:
                    connection.execute(
                        "UPDATE tasks SET state = 'blocked', blocked_category = 'usage_exceeded' WHERE task_id = ?",
                        (call["task_id"],),
                    )
                    task = connection.execute(
                        "SELECT * FROM tasks WHERE task_id = ?", (call["task_id"],),
                    ).fetchone()
                    summary = self._summary(connection, task, call_id)
                connection.commit()
                return summary
        except _IntegrityFailure:
            self.finalize_failure(
                call_id, "ledger_integrity_error", usage=usage,
                estimator_version=estimator, block=True,
            )
            raise LedgerFailure(
                "task_blocked",
                details={"blocked_category": "ledger_integrity_error", "retryable": False},
            ) from None
        except LedgerFailure:
            raise
        except sqlite3.Error as exc:
            raise LedgerFailure("ledger_unavailable") from exc

    def finalize_failure(
        self, call_id: str, category: str, *, usage: int | None = None,
        estimator_version: str | None = None, block: bool = False,
    ) -> dict:
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                call = connection.execute("SELECT * FROM calls WHERE call_id = ?", (call_id,)).fetchone()
                if call is None or call["state"] != "pending":
                    raise LedgerFailure("ledger_unavailable")
                connection.execute(
                    """
                    UPDATE calls SET state = 'failed', usage = ?, estimator_version = ?,
                                     error_category = ?, finished_at = ?
                    WHERE call_id = ? AND state = 'pending'
                    """,
                    (usage, estimator_version, category, _now(), call_id),
                )
                if block:
                    connection.execute(
                        "UPDATE tasks SET state = 'blocked', blocked_category = ? WHERE task_id = ?",
                        (category, call["task_id"]),
                    )
                task = connection.execute(
                    "SELECT * FROM tasks WHERE task_id = ?", (call["task_id"],),
                ).fetchone()
                summary = self._summary(connection, task, call_id)
                connection.commit()
                return summary
        except LedgerFailure:
            raise
        except sqlite3.Error as exc:
            raise LedgerFailure("ledger_unavailable") from exc

    def mark_uncertain(self, call_id: str, category: str) -> None:
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                changed = connection.execute(
                    """
                    UPDATE calls SET state = 'uncertain', error_category = ?, finished_at = ?
                    WHERE call_id = ? AND state = 'pending'
                    """,
                    (category, _now(), call_id),
                ).rowcount
                if changed != 1:
                    raise LedgerFailure("ledger_unavailable")
                connection.commit()
        except LedgerFailure:
            raise
        except sqlite3.Error as exc:
            raise LedgerFailure("ledger_unavailable") from exc

    def get_task(
        self, owner_key: str, task_id: str,
        *, item_ids: tuple[str, ...] | None = None,
    ) -> dict:
        try:
            with self._connect() as connection:
                connection.execute("BEGIN")
                task = self._task(connection, owner_key, task_id)
                calls_total = connection.execute(
                    "SELECT COUNT(*) FROM calls WHERE task_id = ?", (task_id,),
                ).fetchone()[0]
                call_rows = connection.execute(
                    "SELECT calls.*, (SELECT COUNT(*) FROM call_items "
                    "WHERE call_items.call_id = calls.call_id) AS returned_item_count "
                    "FROM calls WHERE task_id = ? ORDER BY sequence_number DESC LIMIT ?",
                    (task_id, DETAIL_CANDIDATE_LIMIT + 1),
                ).fetchall()
                calls = []
                for row in reversed(call_rows[:DETAIL_CANDIDATE_LIMIT]):
                    value = {
                        "call_id": row["call_id"], "sequence_number": row["sequence_number"],
                        "operation": row["operation"], "state": row["state"], "cap": row["cap"],
                        "usage": row["usage"], "error_category": row["error_category"],
                    }
                    if row["operation"] == "search":
                        value.update({
                            "queries": json.loads(row["queries_json"]),
                            "scopes": json.loads(row["scopes_json"]),
                            "limit": row["result_limit"],
                        })
                    else:
                        missing = json.loads(row["missing_item_ids_json"] or "[]")
                        value.update({
                            "seed_item_id": row["seed_item_id"],
                            "bundle_key": row["bundle_key"],
                            "bundle_status": row["bundle_status"],
                            "membership_complete": (
                                None if row["membership_complete"] is None
                                else bool(row["membership_complete"])
                            ),
                            "returned_item_count": row["returned_item_count"],
                            "missing_item_count": len(missing),
                        })
                    calls.append(value)
                items_total = connection.execute(
                    """
                    SELECT COUNT(DISTINCT item.item_id)
                    FROM task_items AS item
                    JOIN call_items AS relation
                      ON relation.task_id = item.task_id AND relation.item_id = item.item_id
                    JOIN calls AS call ON call.call_id = relation.call_id
                    WHERE item.task_id = ? AND call.operation = 'read_bundle'
                                      AND call.state = 'succeeded'
                    """,
                    (task_id,),
                ).fetchone()[0]
                item_filter = ""
                item_parameters: tuple[object, ...] = (task_id,)
                if item_ids is not None:
                    item_filter = (
                        " AND item.item_id IN ("
                        + ",".join("?" for _ in item_ids) + ")"
                    )
                    item_parameters += item_ids
                rows = connection.execute(
                    """
                    SELECT item.item_id, item.source_type, item.source_title,
                           item.heading_path_json, item.path, item.locator, item.role,
                           item.evidence_role, item.turn_index, item.bundle_key,
                           MAX(call.sequence_number) AS last_sequence
                    FROM task_items AS item
                    JOIN call_items AS relation
                      ON relation.task_id = item.task_id AND relation.item_id = item.item_id
                    JOIN calls AS call ON call.call_id = relation.call_id
                    WHERE item.task_id = ? AND call.operation = 'read_bundle'
                                      AND call.state = 'succeeded'
                    """ + item_filter + """
                    GROUP BY item.task_id, item.item_id
                    ORDER BY last_sequence DESC, item.item_id
                    LIMIT ?
                    """,
                    (*item_parameters, DETAIL_CANDIDATE_LIMIT + 1),
                ).fetchall()
                items = [dict(row) for row in rows[:DETAIL_CANDIDATE_LIMIT]]
                for item in items:
                    item.pop("last_sequence")
                    raw_heading = item.pop("heading_path_json")
                    item["heading_path"] = (
                        None if raw_heading is None else json.loads(raw_heading)
                    )
                unavailable_item_ids: list[str] = []
                if item_ids is not None:
                    by_id = {item["item_id"]: item for item in items}
                    unavailable_item_ids = [
                        item_id for item_id in item_ids if item_id not in by_id
                    ]
                    items = [by_id[item_id] for item_id in item_ids if item_id in by_id]
                result = {
                    "task_id": task_id,
                    "task_state": task["state"],
                    "generation": task["generation"],
                    "blocked_category": task["blocked_category"],
                    "limits": {
                        "search_calls": task["search_limit"],
                        "read_calls": task["read_limit"],
                        "estimated_evidence_tokens": task["evidence_token_limit"],
                    },
                    "execution": self._summary(connection, task),
                    "calls": calls,
                    "calls_total": calls_total,
                    "calls_truncated": calls_total > len(calls),
                    "server_returned_items": items,
                    "items_total": items_total,
                    "items_truncated": (
                        len(rows) > DETAIL_CANDIDATE_LIMIT
                        if item_ids is not None else items_total > len(items)
                    ),
                    "item_filter_applied": item_ids is not None,
                    "unavailable_item_ids": unavailable_item_ids,
                }
                connection.commit()
                return result
        except LedgerFailure:
            raise
        except (json.JSONDecodeError, sqlite3.Error) as exc:
            raise LedgerFailure("ledger_unavailable") from exc

    def health(self) -> dict:
        try:
            with self._connect() as connection:
                connection.execute("BEGIN")
                task_count = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
                database_bytes = self._database_bytes(connection)
                status = (
                    "capacity_exceeded"
                    if task_count >= self.config.max_tasks or database_bytes >= self.config.max_bytes
                    else "healthy"
                )
                connection.commit()
                return {
                    "status": status,
                    "task_count": task_count,
                    "database_bytes": database_bytes,
                    "max_tasks": self.config.max_tasks,
                    "max_mb": self.config.max_mb,
                }
        except sqlite3.Error as exc:
            raise LedgerFailure("ledger_unavailable") from exc
