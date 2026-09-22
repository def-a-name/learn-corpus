from __future__ import annotations

import json

from src.retrieval.public_core import MAX_RESPONSE_BYTES
from src.service.retrieval_tasks import RetrievalTaskService


TASK_ID = "tsk_" + "0" * 32


class _DetailLedger:
    def get_task(self, owner_key, task_id, *, item_ids=None):
        assert owner_key == "synthetic_owner" and task_id == TASK_ID
        assert item_ids is None
        calls = [{
            "call_id": f"req_synthetic_{index}",
            "sequence_number": index,
            "operation": "search",
            "state": "succeeded",
            "cap": 100,
            "usage": 1,
            "error_category": None,
            "queries": [f"synthetic-{index}-" + "q" * 180],
            "scopes": ["note"],
            "limit": 8,
        } for index in range(1, 501)]
        items = [{
            "item_id": "itm_" + format(index, "032b").translate(str.maketrans("01", "ab")),
            "source_type": "note",
            "source_title": f"Synthetic source {index}",
            "heading_path": ["Synthetic section"],
            "path": f"sources/notes/synthetic-{index}-" + "p" * 180,
            "locator": f"note:synthetic/part:{index}/" + "l" * 180,
            "role": None,
            "evidence_role": None,
            "turn_index": None,
            "bundle_key": "bnd_" + format(index, "040x"),
        } for index in range(500, 0, -1)]
        return {
            "task_id": TASK_ID,
            "task_state": "active",
            "generation": "gen_" + "1" * 20,
            "blocked_category": None,
            "limits": {
                "search_calls": 500, "read_calls": 500,
                "estimated_evidence_tokens": 1_000_000,
            },
            "execution": {
                "task_id": TASK_ID, "call_id": None, "task_state": "active",
                "generation": "gen_" + "1" * 20, "search_calls": 500,
                "read_calls": 0, "estimated_evidence_tokens": 500,
                "reserved_estimated_tokens": 0, "available_estimated_tokens": 999_500,
                "partial_windows": 0, "unresolved_calls": 0,
            },
            "calls": calls,
            "calls_total": len(calls),
            "calls_truncated": False,
            "server_returned_items": items,
            "items_total": len(items),
            "items_truncated": False,
            "item_filter_applied": False,
            "unavailable_item_ids": [],
        }


def test_task_detail_truncates_complete_recent_entries_to_response_bytes():
    service = RetrievalTaskService(None, _DetailLedger())
    response = service.get_task(
        {"task_id": TASK_ID}, owner_key="synthetic_owner",
    )
    assert len(response.json_bytes) <= MAX_RESPONSE_BYTES
    detail = json.loads(response.json_bytes)
    assert detail["calls_total"] == 500 and detail["items_total"] == 500
    assert detail["calls_truncated"] is True and detail["items_truncated"] is True
    assert 0 < len(detail["calls"]) < detail["calls_total"]
    assert 0 < len(detail["server_returned_items"]) < detail["items_total"]
    sequences = [call["sequence_number"] for call in detail["calls"]]
    assert sequences == sorted(sequences)
    assert sequences[-1] == 500
    assert detail["server_returned_items"][0]["path"].startswith(
        "sources/notes/synthetic-500-"
    )
    assert all(len(item["locator"].rsplit("/", 1)[1]) == 180
               for item in detail["server_returned_items"])
