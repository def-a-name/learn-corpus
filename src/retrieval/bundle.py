"""从不可变 item 关系恢复并验证一个 exchange 或直接 section。"""

from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from time import monotonic

from src.retrieval.contracts import Item
from src.retrieval.lexical_store import (
    IndexUnavailableError,
    InvalidRequestError,
    ItemNotFoundError,
    LexicalStore,
    RetrievalTimeoutError,
)
from src.retrieval.text import estimate_evidence_tokens


_ITEM_ID = re.compile(r"itm_[a-z2-7]{32}")


def check_deadline(deadline: float) -> None:
    """在 Python 聚合与序列化步骤之间检查同一个请求时限。"""

    if monotonic() >= deadline:
        raise RetrievalTimeoutError("request processing deadline exceeded")


def logical_locator(item: Item) -> str:
    suffix = f"/part:{item.part}"
    if (
        type(item.part) is not int or item.part < 1
        or not isinstance(item.locator, str) or not item.locator.endswith(suffix)
        or len(item.locator) == len(suffix)
    ):
        raise IndexUnavailableError("indexed item locator is invalid")
    return item.locator[:-len(suffix)]


def bundle_identity(item: Item) -> tuple[object, ...]:
    """沿用 projector 的持久化逻辑身份，不以显示标题判断归属。"""

    base = logical_locator(item)
    if item.scope == "conversation":
        if (
            item.role not in {"human", "assistant"}
            or type(item.turn_index) is not int or item.turn_index < 0
            or (item.session_id is not None and not isinstance(item.session_id, str))
        ):
            raise IndexUnavailableError("indexed conversation identity is invalid")
        return item.scope, item.source_path, item.session_id, item.turn_index
    if item.scope not in {"note", "article"} or item.role is not None:
        raise IndexUnavailableError("indexed section identity is invalid")
    return item.scope, item.source_path, base


def bundle_key(item: Item) -> str:
    identity = json.dumps(bundle_identity(item), ensure_ascii=False, separators=(",", ":"))
    return "bnd_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:40]


def _validate_item(item: Item) -> None:
    bundle_identity(item)
    refs = (
        *item.relations.counterpart_item_ids,
        item.relations.previous_part_id,
        item.relations.next_part_id,
    )
    if any(ref is not None and _ITEM_ID.fullmatch(ref) is None for ref in refs):
        raise IndexUnavailableError("indexed relation identity is invalid")
    if len(set(item.relations.counterpart_item_ids)) != len(item.relations.counterpart_item_ids):
        raise IndexUnavailableError("indexed counterpart membership is invalid")
    if (
        not isinstance(item.body, str)
        or hashlib.sha256(item.body.encode("utf-8")).hexdigest() != item.body_sha256
        or estimate_evidence_tokens(item.body) != item.token_estimate
        or not 1 <= item.token_estimate <= 800
    ):
        raise IndexUnavailableError("indexed canonical body is invalid")


def read_members(store: LexicalStore, seed: Item, deadline: float) -> tuple[Item, ...]:
    """验证关系闭包及独立成员枚举后返回完整成员，任何完整性错误整体失败。"""

    identity = bundle_identity(seed)
    pending = deque([seed.item_id])
    scheduled = {seed.item_id}
    members: dict[str, Item] = {}
    while pending:
        check_deadline(deadline)
        item_id = pending.popleft()
        try:
            item = seed if item_id == seed.item_id else store.read_canonical_item(
                item_id, store.index_id, deadline=deadline
            )
        except (ItemNotFoundError, InvalidRequestError) as exc:
            raise IndexUnavailableError("indexed bundle relation is unavailable") from exc
        _validate_item(item)
        if bundle_identity(item) != identity or item.source_id != seed.source_id:
            raise IndexUnavailableError("indexed relation crosses a logical unit")
        members[item_id] = item
        for ref in (
            item.relations.previous_part_id,
            item.relations.next_part_id,
            *item.relations.counterpart_item_ids,
        ):
            if ref is not None and ref not in scheduled:
                scheduled.add(ref)
                pending.append(ref)

    check_deadline(deadline)
    if set(store.logical_member_ids(seed, deadline=deadline)) != set(members):
        raise IndexUnavailableError("indexed bundle membership is disconnected")

    groups: dict[str | None, list[Item]] = {}
    for item in members.values():
        groups.setdefault(item.role, []).append(item)
    if seed.scope == "conversation" and set(groups) != {"human", "assistant"}:
        raise IndexUnavailableError("indexed exchange is missing a role")

    ordered: list[Item] = []
    for role in (("human", "assistant") if seed.scope == "conversation" else (None,)):
        check_deadline(deadline)
        parts = sorted(groups[role], key=lambda item: item.part)
        if [item.part for item in parts] != list(range(1, len(parts) + 1)):
            raise IndexUnavailableError("indexed parts are not continuous")
        if len({logical_locator(item) for item in parts}) != 1:
            raise IndexUnavailableError("indexed parts have inconsistent identity")
        opposite = "assistant" if role == "human" else "human"
        expected_counterparts = {item.item_id for item in groups.get(opposite, [])}
        for index, item in enumerate(parts):
            check_deadline(deadline)
            previous = parts[index - 1].item_id if index else None
            following = parts[index + 1].item_id if index + 1 < len(parts) else None
            if (
                item.relations.previous_part_id != previous
                or item.relations.next_part_id != following
                or set(item.relations.counterpart_item_ids) != expected_counterparts
            ):
                raise IndexUnavailableError("indexed bundle relations are inconsistent")
        ordered.extend(parts)
    check_deadline(deadline)
    return tuple(ordered)


def prioritize_members(members: tuple[Item, ...], seed_item_id: str) -> tuple[Item, ...]:
    """从种子开始按距离扩展，同距离优先后项；不改变成员集合或规范顺序。"""

    positions = [index for index, item in enumerate(members) if item.item_id == seed_item_id]
    if len(positions) != 1:
        raise IndexUnavailableError("bundle seed is not a unique member")
    center = positions[0]
    prioritized = [members[center]]
    for distance in range(1, len(members)):
        following = center + distance
        previous = center - distance
        if following < len(members):
            prioritized.append(members[following])
        if previous >= 0:
            prioritized.append(members[previous])
    if len(prioritized) != len(members):
        raise IndexUnavailableError("bundle priority is incomplete")
    return tuple(prioritized)
