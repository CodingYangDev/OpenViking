"""Synthesize one Experience per Case from batched factual Trajectories."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Any

from openviking.session.archive_store import is_storage_not_found
from openviking.session.memory.dataclass import StoredLink
from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils
from openviking.session.train.components.gradient_estimator import (
    ExperienceGradientContext,
    ExperienceGradientEstimator,
)
from openviking.session.train.components.policy_optimizer import case_experience_plan_items
from openviking.session.train.components.trajectory_observation import (
    TRAJECTORY_FORMAT,
    ObservationGradient,
    trajectory_source_session_uri,
)
from openviking.session.train.domain import PolicyUpdatePlan, Trajectory
from openviking.session.train.gates.experience import (
    EXPERIENCE_GATE_CONTEXTS_KEY,
    accept_proposals_without_gate,
)
from openviking.session.train.interfaces import PolicyProposalGate
from openviking.telemetry import tracer


@dataclass(slots=True)
class TrajectoryExperienceOptimizer:
    viking_fs: Any
    estimator: ExperienceGradientEstimator
    memory_registry: Any = None
    proposal_gate: PolicyProposalGate | None = None

    @tracer("train.experience.synthesize_trajectories", ignore_args=True, ignore_result=True)
    async def plan(self, gradients, policy_set, context):
        groups = defaultdict(list)
        for signal in gradients:
            if not isinstance(signal, ObservationGradient):
                raise ValueError(
                    "Trajectory optimizer requires observation signals, not DAG patches"
                )
            groups[signal.target_uri].append(signal)
        items, gates, comparisons = [], [], []
        for target, group in groups.items():
            first = group[0]
            if any(g.target_name != first.target_name for g in group):
                raise ValueError("Observation batch contains conflicting Case identities")
            current = {t.uri: t for g in group for t in g.analysis.trajectories}
            history = []
            if not any(trajectory.outcome == "success" for trajectory in current.values()):
                history = await self._history(
                    first,
                    context.request_context,
                    exclude_uris=set(current),
                )
            trajectories = [*current.values(), *history]
            analysis = replace(
                first.analysis,
                trajectories=trajectories,
                metadata={
                    **first.analysis.metadata,
                    "current_trajectory_uris": list(current),
                },
            )
            gradient_context = ExperienceGradientContext(
                context.request_context, [], memory_registry=self.memory_registry
            )
            proposals = await self.estimator.estimate(analysis, policy_set, gradient_context)
            contexts = [self._source_context(t) for t in trajectories]
            for proposal in proposals:
                proposal.metadata[EXPERIENCE_GATE_CONTEXTS_KEY] = contexts
                proposal.links = [
                    StoredLink(from_uri=target, to_uri=uri, link_type="derived_from", weight=1.0)
                    for uri in dict.fromkeys(t.uri for t in trajectories)
                ]
            if self.proposal_gate is None:
                proposals, gate_metadata = accept_proposals_without_gate(proposals)
            else:
                proposals, gate_metadata = await self.proposal_gate.validate_proposals(
                    proposals, policy_set, gradient_context
                )
            gates.append(gate_metadata)
            # The estimator produced a complete DAG against the observed target version.
            # The deterministic plan retains its base version for commit-time validation.
            for proposal in proposals:
                from openviking.session.memory.dataclass import (
                    ResolvedOperation,
                    ResolvedOperations,
                )

                operations = ResolvedOperations(
                    upsert_operations=[
                        ResolvedOperation(
                            memory_type="experiences",
                            uris=[target],
                            memory_fields=proposal.metadata["memory_fields"],
                        )
                    ],
                    delete_file_contents=[],
                    errors=[],
                )
                items.extend(case_experience_plan_items(operations, [proposal], policy_set))
            comparisons.append(
                {
                    "case_name": first.target_name,
                    "current_trajectory_count": len(current),
                    "trajectory_uris": [t.uri for t in trajectories],
                    "outcomes": [t.outcome for t in trajectories],
                    "synthesis_noop": analysis.metadata.get("experience_synthesis_noop"),
                }
            )
        return PolicyUpdatePlan(
            items=items,
            read_policy_uris=sorted(groups) if self.proposal_gate is None else None,
            metadata={
                "optimizer": "trajectory_comparison",
                "comparisons": comparisons,
                "proposal_gates": gates,
            },
        )

    async def _history(self, signal, ctx, *, exclude_uris: set[str]):
        directory = signal.after_file.uri.rsplit("/", 1)[0]
        try:
            entries = await self.viking_fs.ls(directory, output="original", ctx=ctx)
        except Exception as exc:
            if is_storage_not_found(exc):
                return []
            raise
        result = []
        for entry in entries:
            uri = str(entry.get("uri") or "")
            if (
                entry.get("isDir")
                or uri in exclude_uris
                or not uri.startswith(directory + "/")
                or not uri.endswith(".md")
                or uri.rsplit("/", 1)[-1].startswith(".")
            ):
                continue
            memory = MemoryFileUtils.read(await self.viking_fs.read_file(uri, ctx=ctx), uri=uri)
            metadata = memory.extra_fields
            if (
                memory.memory_type != "trajectories"
                or metadata.get("case_name") != signal.target_name
                or metadata.get("outcome") != "success"
            ):
                continue
            if metadata.get("trajectory_format") != TRAJECTORY_FORMAT:
                continue
            update_state = metadata.get("experience_update")
            if isinstance(update_state, str):
                try:
                    update_state = json.loads(update_state)
                except ValueError:
                    update_state = None
            if isinstance(update_state, dict) and update_state.get("state") not in {
                "applied",
                "no_op",
            }:
                continue
            source_session_uri = trajectory_source_session_uri(memory)
            result.append(
                Trajectory(
                    name=metadata["trajectory_name"],
                    uri=uri,
                    outcome=metadata["outcome"],
                    content=memory.content,
                    metadata={
                        **metadata,
                        "source_session_uri": source_session_uri,
                    },
                )
            )
        ordered = sorted(
            result, key=lambda t: (str(t.metadata.get("created_at", "")), t.uri), reverse=True
        )
        return ordered[:1]

    @staticmethod
    def _source_context(trajectory):
        metadata = trajectory.metadata
        evaluation = metadata.get("evaluation") or {}
        return {
            "source_session_uri": metadata["source_session_uri"],
            "case_name": metadata["case_name"],
            "case_uri": "",
            "passed": evaluation.get("passed"),
            "score": evaluation.get("score"),
            "feedback": metadata.get("feedback", []),
            "experience_execution": metadata["experience_execution"],
            "evidence": [
                {k: v for k, v in item.items() if k != "passed"}
                for item in metadata.get("gate_evidence", [])
            ],
        }
