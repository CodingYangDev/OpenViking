# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Shared training engine for rollout-driven policy updates."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass

from openviking.session.train.components.policy_updater import _normalize_guard_content, _target_uri
from openviking.session.train.context import PipelineContext
from openviking.session.train.domain import (
    ExperienceSet,
    Policy,
    PolicyApplyResult,
    PolicyUpdatePlan,
    Rollout,
    RolloutAnalysis,
)
from openviking.session.train.interfaces import (
    GradientEstimator,
    PolicyOptimizer,
    PolicyUpdateGate,
    PolicyUpdater,
    RolloutAnalyzer,
    SemanticGradient,
)
from openviking.storage.errors import ResourceBusyError

_MAX_PLAN_ATTEMPTS = 3


@dataclass(slots=True)
class PolicyTrainingEngine:
    """Shared implementation of analyze -> estimate -> plan -> apply."""

    rollout_analyzer: RolloutAnalyzer
    gradient_estimator: GradientEstimator
    policy_optimizer: PolicyOptimizer
    policy_updater: PolicyUpdater
    policy_update_gate: PolicyUpdateGate | None = None

    async def analyze_estimate_plan_apply(
        self,
        *,
        rollouts: list[Rollout],
        policy_set: ExperienceSet,
        ctx: PipelineContext,
    ) -> tuple[list[RolloutAnalysis], list[SemanticGradient], PolicyUpdatePlan, PolicyApplyResult]:
        analyses = await self.analyze_rollouts(rollouts, ctx)
        gradients = await self.estimate_gradients(analyses, policy_set, ctx)
        plan, apply_result = await self.plan_and_apply(
            gradients=gradients,
            policy_set=policy_set,
            ctx=ctx,
        )
        return analyses, gradients, plan, apply_result

    async def analyze_rollouts(
        self,
        rollouts: list[Rollout],
        ctx: PipelineContext,
    ) -> list[RolloutAnalysis]:
        analyses = await asyncio.gather(
            *[self.rollout_analyzer.analyze(rollout, ctx.analysis_context) for rollout in rollouts]
        )
        return list(analyses)

    async def estimate_gradients(
        self,
        analyses: list[RolloutAnalysis],
        policy_set: ExperienceSet,
        ctx: PipelineContext,
    ) -> list[SemanticGradient]:
        gradient_batches = await asyncio.gather(
            *[
                self.gradient_estimator.estimate(
                    analysis,
                    policy_set,
                    ctx.gradient_context,
                )
                for analysis in analyses
            ]
        )
        return [gradient for batch in gradient_batches for gradient in batch]

    async def plan_and_apply(
        self,
        *,
        gradients: list[SemanticGradient],
        policy_set: ExperienceSet,
        ctx: PipelineContext,
    ) -> tuple[PolicyUpdatePlan, PolicyApplyResult]:
        for _ in range(_MAX_PLAN_ATTEMPTS):
            async with policy_set.lock():
                planning_policy_set = await policy_set.reload()
                expected = deepcopy({policy.uri: policy for policy in planning_policy_set.policies})

            plan = await self.policy_optimizer.plan(
                gradients,
                planning_policy_set,
                ctx.optimization_context,
            )
            if self.policy_update_gate is not None:
                plan = await self.policy_update_gate.validate(
                    plan,
                    gradients,
                    planning_policy_set,
                    ctx.gradient_context,
                )
            async with policy_set.lock() as transaction_handle:
                latest_policy_set = await policy_set.reload()
                conflicts = _conflicting_policy_uris(
                    plan,
                    expected,
                    latest_policy_set,
                    check_all=self.policy_update_gate is not None,
                )
                if conflicts:
                    # Leaving the context releases the lock before any replanning.
                    continue
                apply_result = await self.policy_updater.apply(
                    plan,
                    latest_policy_set,
                    ctx.apply_context or latest_policy_set.request_context,
                    transaction_handle=transaction_handle,
                )
                return plan, apply_result
        raise ResourceBusyError(
            f"Policies changed or plan became stale during planning after {_MAX_PLAN_ATTEMPTS} "
            f"attempts: {', '.join(conflicts)}",
            uri=policy_set.root_uri,
            conflict_type="policy_plan_stale",
        )


def _conflicting_policy_uris(
    plan: PolicyUpdatePlan,
    expected: dict[str, Policy],
    current: ExperienceSet,
    *,
    check_all: bool,
) -> list[str]:
    actual = {policy.uri: policy for policy in current.policies}
    # A generic optimizer or gate may inspect the whole snapshot. Fixed-Case
    # optimizers declare their complete read set so unrelated Cases can progress.
    watched = (
        set(expected) | set(actual)
        if check_all or plan.read_policy_uris is None
        else set(plan.read_policy_uris)
    )
    watched.update(_target_uri(item, current.root_uri) for item in plan.items)
    conflicts = {uri for uri in watched if expected.get(uri) != actual.get(uri)}
    for item in plan.items:
        uri = _target_uri(item, current.root_uri)
        policy = actual.get(uri)
        if item.base_version is not None and (
            policy is None or policy.version != item.base_version
        ):
            conflicts.add(uri)
        if item.before_content is not None and (
            policy is None
            or _normalize_guard_content(policy.content)
            != _normalize_guard_content(item.before_content)
        ):
            conflicts.add(uri)
    return sorted(conflicts)
