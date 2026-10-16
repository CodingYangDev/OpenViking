"""Shared Trajectory evidence, provenance and pending Experience-learning signals."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

import yaml

from openviking.core.namespace import is_session_uri
from openviking.session.memory.dataclass import (
    MemoryFile,
    StoredLink,
)
from openviking.session.memory.experience.dag import clip_evidence_summary, tool_evidence_summary
from openviking.session.train.domain import RolloutAnalysis, RubricEvaluation, Trajectory

MAX_EVIDENCE_CHARS = 64_000
EXPERIENCE_EXECUTION_EVIDENCE_ID = "experience_execution"
TRAJECTORY_FORMAT = "ordered_markdown_v3"


def execution_paths(execution: dict) -> dict:
    return {
        snapshot.get("experience_name") or uri.rsplit("/", 1)[-1].removesuffix(".md"): {
            key: snapshot.get(key, {} if key == "slot_values" else [])
            for key in ("slot_values", "executed_nodes", "current_nodes")
        }
        for uri, snapshot in execution.items()
    }


def observation_evidence(messages, evaluation, execution: dict | None = None) -> list[dict]:
    evidence = []
    for index, message in enumerate(messages):
        content = str(message.content or "")
        if (
            content
            and not content.startswith(
                ("system:", "# OpenViking", "task_success:", "[Rollout Evaluation]")
            )
            and message.role != "system"
        ):
            evidence.append(
                {
                    "id": f"message:{index}",
                    "kind": (
                        "experience_reminder"
                        if content.startswith("[Experience Reminder]")
                        else f"{message.role}_message"
                    ),
                    "summary": clip_evidence_summary("text", content, 16_384),
                }
            )
        for part_index, part in enumerate(message.parts):
            if getattr(part, "tool_name", None):
                evidence.append(
                    {
                        "id": f"message:{index}:tool:{part_index}",
                        "kind": "tool_result",
                        "summary": tool_evidence_summary(
                            part.tool_name,
                            part.tool_output or "",
                            tool_input=part.tool_input,
                            tool_status=part.tool_status,
                        ),
                    }
                )
    # Bound complete records, never cut the serialized JSON. Preserve both early and late facts.
    selected = []
    used = 0
    for item in evidence:
        size = len(item["summary"])
        if used + size <= MAX_EVIDENCE_CHARS // 2:
            selected.append(item)
            used += size
        else:
            break
    selected_ids = {item["id"] for item in selected}
    tail = []
    for item in reversed(evidence):
        if item["id"] in selected_ids:
            continue
        size = len(item["summary"])
        if used + size > MAX_EVIDENCE_CHARS:
            continue
        tail.append(item)
        used += size
    selected.extend(reversed(tail))
    if len(selected) != len(evidence):
        selected.append(
            {
                "id": "evidence:omitted",
                "kind": "truncation",
                "summary": "Some conversation records were omitted by the evidence budget.",
            }
        )
    if execution is not None:
        selected.append(
            {
                "id": EXPERIENCE_EXECUTION_EVIDENCE_ID,
                "kind": "experience_execution",
                "summary": clip_evidence_summary(
                    "experience_execution",
                    json.dumps(execution, ensure_ascii=False),
                    16_384,
                ),
            }
        )
    if evaluation is not None:
        # Preserve the supplied evaluator report as evidence. Criterion definitions and
        # judgments belong to the Case/Trajectory LLM, not a whitelist of boolean keys.
        selected.append(
            {
                "id": "evaluation:external",
                "kind": "external_evaluation",
                "summary": json.dumps(asdict(evaluation), ensure_ascii=False),
            }
        )
    return selected


def trajectory_evaluation_summary(evaluation: RubricEvaluation | None) -> dict | None:
    """Keep external results and diagnostics out of the observed business dialogue."""
    if evaluation is None:
        return None
    summary: dict[str, Any] = {"passed": evaluation.passed, "score": evaluation.score}
    details = {}
    result = evaluation.metadata.get("evaluation_result")
    if isinstance(result, dict):
        # Keep diagnostic facts, not another copy of actions/criteria/rewards or LLM rationales.
        for key in ("termination_reason", "info", "error", "type", "message"):
            value = result.get(key)
            if key == "info" and isinstance(value, dict):
                value = {name: item for name, item in value.items() if item is not None}
            if value is not None and value != {}:
                details[key] = value
    reason = evaluation.metadata.get("termination_reason")
    if reason is not None:
        details["termination_reason"] = reason
    if details:
        summary["details"] = details
    return summary


def render_trajectory_content(trajectory: Trajectory) -> str:
    """Render immutable learning evidence once, before the Trajectory is persisted.

    Metadata remains authoritative for programmatic selection and gates. Only this
    allowlisted snapshot belongs in the body; mutable update state never does.
    """
    metadata = trajectory.metadata
    evaluation = metadata.get("evaluation")
    fields = {
        "trajectory_uri": trajectory.uri,
        "source_session_uri": metadata["source_session_uri"],
        "case_name": metadata["case_name"],
        "task_spec": metadata.get("task_spec"),
        "outcome": trajectory.outcome,
        "evaluation": None
        if evaluation is None
        else {
            "passed": evaluation["passed"],
            "score": evaluation["score"],
            **({"details": evaluation["details"]} if "details" in evaluation else {}),
        },
        "evaluation_criteria": metadata.get("evaluation_criteria", []),
        "experience_execution": metadata.get("experience_execution", {}),
        "experience_effects": metadata.get("experience_effects"),
        "experience_effect_sources": metadata.get("experience_effect_sources", {}),
    }

    normalized = json.loads(json.dumps(fields, ensure_ascii=False, default=str))
    header = yaml.safe_dump(normalized, allow_unicode=True, sort_keys=False).rstrip()
    return f"# Trajectory Record\n\n```yaml\n{header}\n```\n\n{trajectory.content}"


def trajectory_source_session_uri(memory: MemoryFile) -> str:
    """Resolve the immutable source Session from the Trajectory's provenance link."""
    sources = []
    for value in memory.links or []:
        link = value if isinstance(value, StoredLink) else StoredLink.model_validate(value)
        if link.link_type == "derived_from" and link.to_uri and is_session_uri(link.to_uri):
            sources.append(link.to_uri)
    sources = list(dict.fromkeys(sources))
    if len(sources) != 1:
        raise ValueError("Trajectory must have exactly one derived_from Session link")
    return sources[0]


@dataclass(slots=True)
class ObservationGradient:
    """A persisted observation awaiting synthesis, not an Experience patch."""

    analysis: RolloutAnalysis
    target_name: str
    target_uri: str
    after_file: MemoryFile
    before_file: MemoryFile | None = None
    base_version: int | None = None
    rationale: str = "Compare this factual Trajectory with other executions of the same Case."
    confidence: float = 1.0
    links: list[StoredLink] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
