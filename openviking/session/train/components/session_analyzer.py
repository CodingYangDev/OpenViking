# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Analyze Session evidence and persist its immutable Case Trajectory."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from openviking.config.vlm import VLMResolver
from openviking.message import Message
from openviking.server.identity import RequestContext
from openviking.session.archive_store import is_storage_not_found
from openviking.session.memory import ExtractLoop
from openviking.session.memory.dataclass import MemoryFile, ResolvedOperations, StoredLink
from openviking.session.memory.experience.feedback import (
    injected_actions,
    record_experience_feedback_stats,
)
from openviking.session.memory.experience.trajectory_context import (
    AgentTrajectoryContextProvider,
    trajectory_source_steps,
)
from openviking.session.memory.memory_isolation_handler import MemoryIsolationHandler
from openviking.session.memory.memory_type_registry import MemoryTypeRegistry
from openviking.session.memory.memory_updater import MemoryUpdater
from openviking.session.memory.streaming_memory_updater import _acquire_stable_operation_lease
from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils
from openviking.session.memory.utils.uri import generate_uri
from openviking.session.skill.session_skill_context_provider import (
    SESSION_SKILL_MEMORY_TYPE,
    SessionSkillContextProvider,
    load_skill_extract_registry,
)
from openviking.session.train.components.trajectory_observation import (
    TRAJECTORY_FORMAT,
    execution_paths,
    observation_evidence,
    trajectory_evaluation_summary,
    trajectory_source_session_uri,
)
from openviking.session.train.domain import Rollout, RolloutAnalysis, Trajectory
from openviking.session.train.gradients import PatchSemanticGradient
from openviking.session.train.interfaces import RolloutEvaluator
from openviking.storage.viking_fs import get_viking_fs
from openviking.telemetry import tracer


@dataclass(slots=True)
class SessionAnalyzerContext:
    request_context: RequestContext
    strict_extract_errors: bool = False
    evaluator_context: Any = None
    include_session_skills: bool = False
    source_archive_uri: str = ""
    memory_registry: MemoryTypeRegistry | None = None


@dataclass(slots=True)
class SessionRolloutAnalyzer:
    """Return evaluated Session evidence with a persisted Trajectory.

    Analysis may call the model and write the observation. Experience synthesis
    remains a separate, cross-Session batch operation.
    """

    viking_fs: Any = None
    vikingdb: Any = None
    vlm: Any = None
    evaluator: RolloutEvaluator | None = None
    vlm_resolver: VLMResolver | None = None

    @tracer("train.rollout_analyzer.session.analyze", ignore_result=True, ignore_args=True)
    async def analyze(self, rollout: Rollout, context: SessionAnalyzerContext) -> RolloutAnalysis:
        if context is None or context.request_context is None:
            raise ValueError("SessionAnalyzerContext.request_context is required")
        evaluation = rollout.evaluation
        if evaluation is None and self.evaluator is not None:
            evaluation = await self.evaluator.evaluate(rollout, context.evaluator_context)
        source_uri = str(rollout.metadata.get("source_session_uri") or context.source_archive_uri)
        gradients = []
        if context.include_session_skills:
            result = await self.extract_session_skills(
                messages=rollout.messages,
                ctx=context.request_context,
                source_archive_uri=source_uri,
                strict_extract_errors=context.strict_extract_errors,
            )
            gradients = result["skill_gradients"]
        analysis = RolloutAnalysis(
            evaluation=evaluation,
            rollout=rollout,
            gradients=gradients,
            metadata={
                "case_uri": rollout.metadata.get("case_uri", ""),
                "task_spec": rollout.metadata.get("task_spec"),
                "source_session_uri": source_uri,
                "policy_snapshot_id": rollout.policy_snapshot_id,
                "experience_execution": _experience_execution_from_rollout(rollout),
            },
        )
        await self._extract_trajectory(analysis, context)
        return analysis

    @tracer("train.trajectory.observe", ignore_args=True, ignore_result=True)
    async def _extract_trajectory(
        self, analysis: RolloutAnalysis, context: SessionAnalyzerContext
    ) -> None:
        """Extract or reuse the immutable observation for this Case and Session."""
        rollout = analysis.rollout
        if rollout is None or not analysis.metadata.get("source_session_uri"):
            raise ValueError("Trajectory requires a Case rollout and source Session archive")
        registry = context.memory_registry
        if registry is None:
            from openviking.session.memory.memory_type_registry import get_default_registry

            registry = get_default_registry()
        schema = registry.get("trajectories")
        if schema is None or not schema.enabled:
            raise ValueError("Trajectory schema is required for Experience learning")
        source = analysis.metadata["source_session_uri"]
        identity = hashlib.sha256((rollout.case.name + "\0" + source).encode()).hexdigest()
        fields = {"trajectory_name": identity, "case_name": rollout.case.name}
        ctx = context.request_context
        uri = generate_uri(schema, fields, user_space=ctx.user.user_id)
        exp_schema = registry.get("experiences")
        if exp_schema is None or not exp_schema.enabled:
            raise ValueError("Experience schema is required for deferred learning")
        target = generate_uri(
            exp_schema, {"experience_name": rollout.case.name}, user_space=ctx.user.user_id
        )
        fs = self.viking_fs or get_viking_fs()
        if fs is None:
            raise RuntimeError("VikingFS is required for Trajectory extraction")
        raw = await self._read_trajectory(fs, uri, ctx)
        if raw is None:
            paths = execution_paths(analysis.metadata.get("experience_execution") or {})
            source_steps = trajectory_source_steps(rollout.messages)
            outcome = (
                "unknown"
                if analysis.evaluation is None
                else ("success" if analysis.evaluation.passed else "failure")
            )
            model = self.vlm
            if model is None:
                if self.vlm_resolver is None:
                    raise RuntimeError("Trajectory extraction requires a VLM or VLM resolver")
                model = await self.vlm_resolver.get_vlm(ctx.account_id)
            provider = AgentTrajectoryContextProvider(
                messages=rollout.messages,
                case=asdict(rollout.case),
                task_spec=analysis.metadata.get("task_spec"),
                external_evaluation=asdict(analysis.evaluation) if analysis.evaluation else None,
                trajectory_name=identity,
                case_name=rollout.case.name,
                outcome=outcome,
                target_uri=uri,
                source_steps=source_steps,
                injected_actions=injected_actions(
                    analysis.metadata.get("experience_execution") or {}
                ),
                memory_registry=registry,
                ctx=ctx,
                viking_fs=fs,
                vlm_config=model,
            )
            extract_context = provider.get_extract_context()
            isolation = MemoryIsolationHandler(
                ctx,
                extract_context,
                allowed_memory_types={"trajectories"},
            )
            isolation.prepare_messages()
            provider._isolation_handler = isolation
            operations, _ = await ExtractLoop(
                vlm=model,
                viking_fs=fs,
                ctx=ctx,
                context_provider=provider,
                isolation_handler=isolation,
                thinking=True,
            ).run()
            if operations.errors or operations.delete_file_contents:
                raise ValueError(f"Invalid Trajectory operations: {operations.errors}")
            if len(operations.upsert_operations) != 1:
                raise ValueError("Trajectory extraction must create exactly one memory")
            operation = operations.upsert_operations[0]
            if operation.memory_type != "trajectories" or operation.uris != [uri]:
                raise ValueError("Trajectory extraction changed the fixed target")
            evaluation = trajectory_evaluation_summary(analysis.evaluation)
            fields.update(
                {
                    "outcome": outcome,
                    "task_spec": analysis.metadata.get("task_spec"),
                    "trajectory_format": TRAJECTORY_FORMAT,
                    "evaluation": evaluation,
                    "experience_execution": paths,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "experience_update": {
                        "state": "pending",
                        "attempts": 0,
                        "experience_uri": target,
                        "batch_id": None,
                        "lease_until": None,
                        "processed_at": None,
                        "error": None,
                    },
                }
            )
            links = [
                StoredLink(
                    from_uri=uri, to_uri=source, link_type="derived_from", weight=1.0
                ).model_dump()
            ]
            case_uri = str(analysis.metadata.get("case_uri") or "")
            if case_uri:
                links.append(
                    StoredLink(
                        from_uri=uri, to_uri=case_uri, link_type="related_to", weight=1.0
                    ).model_dump()
                )
            operation.memory_fields.update(fields)
            # The common writer renders the schema's content_template from these fields
            # once. Lifecycle updates preserve the resulting immutable body.
            operations.resolved_links = [StoredLink.model_validate(link) for link in links]
            lock = await _acquire_stable_operation_lease(operations, fs, ctx)
            try:
                existing = await self._read_trajectory(fs, uri, ctx)
                if existing is None:
                    updater = MemoryUpdater(
                        registry=registry,
                        vikingdb=self.vikingdb,
                        transaction_handle=lock,
                    )
                    updater._viking_fs = fs
                    update_result = await updater.apply_operations(operations, ctx)
                    if update_result.errors:
                        raise ValueError(
                            "; ".join(
                                f"{target}: {error}" for target, error in update_result.errors
                            )
                        )
                    raw = await fs.read_file(uri, ctx=ctx)
                else:
                    raw = existing
            finally:
                if lock is not None:
                    await fs._async_agfs.pathlock_release(lock)
        memory = MemoryFileUtils.read(raw, uri=uri)
        linked_source = trajectory_source_session_uri(memory)
        if memory.extra_fields.get("case_name") != rollout.case.name or linked_source != source:
            raise ValueError("Trajectory identity mismatch")
        if memory.extra_fields.get("task_spec") != analysis.metadata.get("task_spec"):
            raise ValueError("Trajectory task identity mismatch")
        metadata = dict(memory.extra_fields)
        metadata["source_session_uri"] = linked_source
        metadata["gate_evidence"] = observation_evidence(
            rollout.messages,
            analysis.evaluation,
            metadata.get("experience_execution") or {},
        )
        metadata["feedback"] = (
            list(analysis.evaluation.feedback) if analysis.evaluation is not None else []
        )
        trajectory = Trajectory(
            name=memory.extra_fields["trajectory_name"],
            uri=uri,
            content=memory.content,
            outcome=memory.extra_fields["outcome"],
            metadata=metadata,
        )
        analysis.trajectories = [trajectory]
        analysis.metadata["trajectory_memory"] = memory
        analysis.metadata["experience_feedback_stats"] = await record_experience_feedback_stats(
            memory, registry=registry, viking_fs=fs, ctx=ctx
        )

    @staticmethod
    async def _read_trajectory(fs: Any, uri: str, ctx: RequestContext) -> str | None:
        try:
            return await fs.read_file(uri, ctx=ctx)
        except Exception as exc:
            if is_storage_not_found(exc):
                return None
            raise

    async def extract_session_skills(
        self,
        *,
        messages: list[Message],
        ctx: RequestContext,
        source_archive_uri: str = "",
        strict_extract_errors: bool = False,
    ) -> dict[str, Any]:
        vlm_config = None
        if self.vlm is None:
            if self.vlm_resolver is None:
                raise RuntimeError("Session skill extraction requires a VLM resolver")
            vlm_config = await self.vlm_resolver.get_vlm(ctx.account_id)
        provider = SessionSkillContextProvider(messages=messages, vlm_config=vlm_config)
        provider._registry = load_skill_extract_registry()
        provider.include_tool_parts_in_conversation = True
        provider._ctx = ctx
        viking_fs = self.viking_fs or get_viking_fs()
        provider._viking_fs = viking_fs
        isolation = MemoryIsolationHandler(
            ctx, provider.get_extract_context(), allowed_memory_types={SESSION_SKILL_MEMORY_TYPE}
        )
        isolation.prepare_messages()
        provider._isolation_handler = isolation
        orchestrator = ExtractLoop(
            vlm=self.vlm or vlm_config,
            viking_fs=viking_fs,
            ctx=ctx,
            context_provider=provider,
            isolation_handler=isolation,
            thinking=True,
        )
        operations, _ = await orchestrator.run()
        if operations is None:
            return {"skill_gradients": []}
        if operations.errors:
            raise ValueError(f"Session skill extraction failed: {operations.errors}")
        return {"skill_gradients": _skill_operations_to_gradients(operations)}


def _experience_execution_from_rollout(rollout: Rollout) -> dict[str, dict[str, Any]]:
    """Build the final per-Experience DAG snapshots recorded with a Session."""
    metadata = getattr(rollout, "metadata", {})
    precomputed = metadata.get("experience_execution")
    if isinstance(precomputed, dict):
        return {
            str(uri): dict(snapshot)
            for uri, snapshot in precomputed.items()
            if uri and isinstance(snapshot, dict)
        }
    return experience_execution_from_runtime(metadata.get("dag_runtime"))


def experience_execution_from_runtime(runtime: Any) -> dict[str, dict[str, Any]]:
    """Collapse runtime events into one factual final snapshot per Experience."""
    events = runtime.get("events") if isinstance(runtime, dict) else None
    if not isinstance(events, list):
        return {}

    snapshot_fields = (
        "state",
        "revision",
        "slot_values",
        "slot_evidence",
        "executed_nodes",
        "current_nodes",
        "waiting_for_context",
        "actions",
        "completed_nodes",
        "error",
    )
    snapshots: dict[str, dict[str, Any]] = {}
    node_slots_by_experience: dict[str, dict[int, str]] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        experience_uri = str(event.get("experience_uri") or "").strip()
        if not experience_uri:
            continue
        snapshot = snapshots.setdefault(experience_uri, {})
        node_slots = node_slots_by_experience.setdefault(experience_uri, {})
        for node_id, slot_name in dict(event.get("node_slots") or {}).items():
            try:
                node_slots[int(node_id)] = str(slot_name)
            except (TypeError, ValueError):
                continue
        for collection_name in ("actions", "completed_nodes"):
            for node in event.get(collection_name) or []:
                if not isinstance(node, dict) or not node.get("slot_name"):
                    continue
                try:
                    node_slots[int(node.get("node_id"))] = str(node["slot_name"])
                except (TypeError, ValueError):
                    continue
        snapshot["experience_name"] = (
            experience_uri.rstrip("/").rsplit("/", 1)[-1].removesuffix(".md")
        )
        for field_name in snapshot_fields:
            if field_name in event:
                snapshot[field_name] = event[field_name]
        if event.get("delivered_advice"):
            # Delivery is a separate fact; asking or suggesting a tool never proves completion.
            snapshot.setdefault("delivered_advice", []).extend(event["delivered_advice"])
    for experience_uri, snapshot in snapshots.items():
        node_slots = node_slots_by_experience.get(experience_uri, {})
        for field_name in ("executed_nodes", "current_nodes", "waiting_for_context"):
            node_ids = snapshot.get(field_name)
            if isinstance(node_ids, list):
                snapshot[field_name] = [node_slots.get(node_id, node_id) for node_id in node_ids]
    return snapshots


def _skill_operations_to_gradients(
    operations: ResolvedOperations,
    *,
    viking_fs: Any = None,
    ctx: Any = None,
) -> list[PatchSemanticGradient]:
    """Convert skill ResolvedOperations to PatchSemanticGradient instances.

    The resulting gradients carry the full proposed skill content in their
    ``after_file`` so the patch-merge optimizer can reconcile multiple
    proposals against the current policy set.
    """
    gradients: list[PatchSemanticGradient] = []
    for op in operations.upsert_operations or []:
        if op.memory_type != SESSION_SKILL_MEMORY_TYPE:
            continue
        fields = dict(op.memory_fields or {})
        skill_name = str(fields.get("skill_name") or _fallback_skill_name(op))
        target_uri = (op.uris or [None])[0]
        after_content = str(fields.get("content") or "")
        if not after_content.strip():
            continue

        old_file = op.old_memory_file_content
        after_file = MemoryFile(
            uri=target_uri,
            content=after_content,
            memory_type="skills",
            extra_fields={
                **dict(getattr(old_file, "extra_fields", {}) or {}),
                **{k: v for k, v in fields.items() if k != "content"},
                "memory_type": "skills",
                "skill_name": skill_name,
            },
        )
        links: list[StoredLink] = []

        gradients.append(
            PatchSemanticGradient(
                before_file=old_file,
                after_file=after_file,
                base_version=_base_version_from_old_file(old_file),
                rationale=(
                    "Session skill patch extracted from Session by SessionSkillContextProvider."
                ),
                links=links,
                confidence=0.7,
                metadata={
                    "source": "session_skill_extract",
                    "memory_fields": fields,
                    "skill_name": skill_name,
                    "uris": list(op.uris or []),
                },
            )
        )
    return gradients


def _fallback_skill_name(op: Any) -> str:
    uris = getattr(op, "uris", None) or []
    if uris:
        uri = str(uris[0])
        # path/to/skills/my_skill/SKILL.md → my_skill
        parts = uri.rstrip("/").split("/")
        if len(parts) >= 2 and parts[-1] == "SKILL.md":
            return parts[-2]
        return parts[-1].removesuffix(".md")
    return "unknown_skill"


def _base_version_from_old_file(old_file: Any) -> int | None:
    if old_file is None:
        return None
    fields = getattr(old_file, "extra_fields", {}) or {}
    try:
        v = int(fields.get("version"))
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None
