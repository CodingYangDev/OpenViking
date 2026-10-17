# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Repairable candidate rejection; infrastructure errors must propagate instead."""

from __future__ import annotations

import json
from typing import Any


class ExperienceGateRejected(ValueError):
    def __init__(self, issues: list[dict[str, Any]]):
        self.issues = issues
        super().__init__(json.dumps(issues, ensure_ascii=False))
