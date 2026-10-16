# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Domain rubric definitions and immutable, per-execution result snapshots."""

from __future__ import annotations

import json
from typing import Any

from openviking_cli.utils import get_logger

logger = get_logger(__name__)


def parse_case_rubric(value: Any) -> list[dict[str, str]]:
    """Read the structured rubric carried in the schema's string field."""
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        raise ValueError("Case rubric must be a JSON list of id/description objects")
    result = []
    seen = set()
    for item in value:
        if (
            not isinstance(item, dict)
            or set(item) != {"id", "description"}
            or any(not isinstance(text, str) or not text.strip() for text in item.values())
        ):
            raise ValueError("Each Case rubric item requires non-empty id and description strings")
        if item["id"] in seen:
            raise ValueError(f"Duplicate Case rubric id: {item['id']}")
        seen.add(item["id"])
        result.append(dict(item))
    return result


def normalize_case_rubric(value: Any) -> str:
    """Validate after patch application, before persisting the complete rubric."""
    return json.dumps(
        parse_case_rubric([] if value is None else value), ensure_ascii=False, indent=2
    )


def snapshot_rubric_results(value: Any, rubric: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Keep valid judgments without letting auxiliary results block the factual trajectory.

    Missing entries are not invented. Unknown or repeated IDs and invalid judgments are
    omitted; descriptions always come from this call's Case rubric, not the model.
    """
    definitions = {item["id"]: item for item in rubric}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, RecursionError) as exc:
            logger.warning("Discarding unparseable evaluation_criteria JSON: %s", exc)
            return []
    if not isinstance(value, list):
        logger.warning("Discarding non-list evaluation_criteria: %s", type(value).__name__)
        return []

    results = {}
    seen = set()
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        rubric_id = item["id"]
        if rubric_id not in definitions:
            continue
        if rubric_id in seen:
            # Do not choose between potentially conflicting judgments, even if one
            # of the repeated entries is invalid. Omit this ID altogether.
            results.pop(rubric_id, None)
            continue
        seen.add(rubric_id)
        if "passed" not in item or (
            item["passed"] is not None and type(item["passed"]) is not bool
        ):
            continue
        if not isinstance(item.get("evidence"), str) or not item["evidence"].strip():
            continue
        results[rubric_id] = {
            **definitions[rubric_id],
            "passed": item["passed"],
            "evidence": item["evidence"],
        }
    if len(results) != len(value):
        logger.warning(
            "Discarded %d of %d evaluation_criteria entries "
            "(invalid fields, unknown IDs or duplicate IDs)",
            len(value) - len(results),
            len(value),
        )
    return [results[item["id"]] for item in rubric if item["id"] in results]
