# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Composition of type-owned content gates; independent of optional quality gates."""

from __future__ import annotations

from collections.abc import Callable

from openviking.session.memory.experience.compiler import normalize_dag_source

# Registration is declarative. Generic extraction/merge/write code knows only this contract:
# a gate accepts fully materialized content and returns its validated canonical form.
_CONTENT_GATES: dict[str, Callable[[str], str]] = {"experiences": normalize_dag_source}


def get_content_gate(memory_type: str) -> Callable[[str], str] | None:
    return _CONTENT_GATES.get(memory_type)
