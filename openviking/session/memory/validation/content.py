# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Type-owned field gates, applied to complete values after merging patches."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from openviking.session.memory.experience.compiler import normalize_dag_source
from openviking.session.memory.experience.rubric import normalize_case_rubric

_FIELD_GATES: dict[str, dict[str, Callable[[Any], Any]]] = {
    "experiences": {"content": normalize_dag_source},
    "cases": {"rubric": normalize_case_rubric},
}


def get_field_gates(memory_type: str) -> dict[str, Callable[[Any], Any]]:
    return _FIELD_GATES.get(memory_type, {})
