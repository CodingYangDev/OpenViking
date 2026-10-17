"""Synthesize one Experience per Case from batched factual Trajectories."""

from __future__ import annotations

import hashlib
import json
import posixpath
from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Any

from openviking.session.archive_store import is_storage_not_found
from openviking.session.memory.utils.link_renderer import LinkRenderer
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
from openviking.session.train.domain import PolicyPlanItem, PolicyUpdatePlan, Trajectory
from openviking.session.train.gates.experience import (
    EXPERIENCE_GATE_CONTEXTS_KEY,
)
from openviking.telemetry import tracer

SUCCESS_TRAJECTORY_REFS = "success_trajectory_refs"


@dataclass(slots=True)
class TrajectoryExperienceOptimizer:
    viking_fs: Any
    estimator: ExperienceGradientEstimator
    memory_registry: Any = None

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
            policy = next((p for p in policy_set.policies if p.uri == target), None)
            references = policy.metadata.get(SUCCESS_TRAJECTORY_REFS, {}) if policy else {}
            if not isinstance(references, dict):
                raise ValueError("Experience success trajectory references must be a mapping")
            task_keys = {t.uri: _comparison_key(t.metadata) for t in current.values()}
            successes = {
                task_keys[t.uri]
                for t in current.values()
                if t.outcome == "success" and task_keys[t.uri] is not None
            }
            missing = dict.fromkeys(
                task_keys[t.uri]
                for t in current.values()
                if t.outcome != "success"
                and task_keys[t.uri] is not None
                and task_keys[t.uri] not in successes
            )
            history = await self._history(
                first,
                context.request_context,
                references={key: references[key] for key in missing if key in references},
                exclude_uris=set(current),
            )
            updated_references = dict(references)
            for trajectory in sorted(
                current.values(), key=lambda t: (str(t.metadata.get("created_at", "")), t.uri)
            ):
                key = task_keys[trajectory.uri]
                if trajectory.outcome != "success" or key is None:
                    continue
                relative = LinkRenderer.relative_path(target, trajectory.uri)
                if relative:
                    updated_references[key] = relative
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
                context.request_context,
                [],
                memory_registry=self.memory_registry,
                metadata={
                    EXPERIENCE_GATE_CONTEXTS_KEY: [self._source_context(t) for t in trajectories]
                },
            )
            proposals = await self.estimator.estimate(analysis, policy_set, gradient_context)
            gates.extend(proposal.metadata["experience_proposal_gate"] for proposal in proposals)
            case_items = []
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
                case_items.extend(case_experience_plan_items(operations, [proposal], policy_set))
            if updated_references != references:
                if not case_items and policy is not None:
                    # Evidence references are independent of a semantic DAG change. Use the
                    # normal locked plan/apply path, never create an empty Experience.
                    case_items.append(
                        PolicyPlanItem(
                            kind="upsert",
                            memory_type="experiences",
                            target_name=first.target_name,
                            target_uri=target,
                            before_content=policy.content,
                            after_content=policy.content,
                            base_version=policy.version,
                            metadata={"success_reference_update_only": True},
                        )
                    )
                for item in case_items:
                    item.metadata.setdefault("patch_metadata", {})[SUCCESS_TRAJECTORY_REFS] = (
                        updated_references
                    )
            items.extend(case_items)
            comparisons.append(
                {
                    "case_name": first.target_name,
                    "current_trajectory_count": len(current),
                    "trajectory_uris": [t.uri for t in trajectories],
                    "outcomes": [t.outcome for t in trajectories],
                }
            )
        return PolicyUpdatePlan(
            items=items,
            read_policy_uris=sorted(groups),
            metadata={
                "optimizer": "trajectory_comparison",
                "comparisons": comparisons,
                "proposal_gates": gates,
            },
        )

    async def _history(self, signal, ctx, *, references: dict[str, str], exclude_uris: set[str]):
        """Load only referenced successes with the same concrete task context."""
        directory = signal.after_file.uri.rsplit("/", 1)[0]
        exp_directory = signal.target_uri.removeprefix("viking://").rsplit("/", 1)[0]
        result = []
        for key, relative in references.items():
            if not isinstance(relative, str) or not relative or relative.startswith("/"):
                continue
            if ":" in relative or "\\" in relative:
                continue
            uri = "viking://" + posixpath.normpath(posixpath.join(exp_directory, relative))
            if (
                uri in exclude_uris
                or not uri.startswith(directory + "/")
                or not uri.endswith(".md")
                or uri.rsplit("/", 1)[-1].startswith(".")
            ):
                continue
            try:
                raw = await self.viking_fs.read_file(uri, ctx=ctx)
            except Exception as exc:
                if is_storage_not_found(exc):
                    continue
                raise
            if not raw:
                continue
            memory = MemoryFileUtils.read(raw, uri=uri)
            metadata = memory.extra_fields
            if (
                memory.memory_type != "trajectories"
                or metadata.get("case_name") != signal.target_name
                or metadata.get("outcome") != "success"
                or metadata.get("trajectory_format") != TRAJECTORY_FORMAT
                or not metadata.get("trajectory_name")
                or _comparison_key(metadata) != key
            ):
                continue
            update_state = metadata.get("experience_update")
            if isinstance(update_state, str):
                try:
                    update_state = json.loads(update_state)
                except ValueError:
                    continue
            if not isinstance(update_state, dict) or update_state.get("state") not in {
                "applied",
                "no_op",
            }:
                continue
            try:
                source_session_uri = trajectory_source_session_uri(memory)
            except ValueError:
                continue
            result.append(
                Trajectory(
                    name=metadata["trajectory_name"],
                    uri=uri,
                    outcome=metadata["outcome"],
                    content=memory.content,
                    metadata={**metadata, "source_session_uri": source_session_uri},
                )
            )
        return result

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


def _comparison_key(metadata: dict[str, Any]) -> str | None:
    """Match concrete task inputs/constraints, not a broad Case or trial identity.

    This is conservative snapshot matching, not proof that two conversations took
    the same path. The updater must still compare their recorded requests and facts.
    """
    task = metadata.get("task_spec")
    if not isinstance(task, dict) or not isinstance(task.get("input"), dict):
        return None
    if not isinstance(task.get("rubric"), dict) or not task.get("task_signature"):
        return None
    task_metadata = task.get("metadata") or {}
    if not isinstance(task_metadata, dict):
        return None
    snapshot = {
        "task_signature": task["task_signature"],
        "input": task["input"],
        "rubric": task["rubric"],
        "metadata": {
            key: value
            for key, value in task_metadata.items()
            if key not in {"trial", "trial_count", "rollout_case_name", "rollout_task_signature"}
        },
    }
    return hashlib.sha256(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
