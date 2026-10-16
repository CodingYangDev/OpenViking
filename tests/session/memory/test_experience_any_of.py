from __future__ import annotations

import pytest

from openviking.session.memory.experience.compiler import compile_dag
from openviking.session.memory.experience.dag import Dag, DagInstance

SOURCE = """dag = workflow("Either supported situation")
a = check("Situation A holds")
b = check("Situation B holds")
either = any_of(a, b)
reminder = tell("Apply the scoped correction")
either.then(reminder)"""


def instance(source: str = SOURCE) -> DagInstance:
    return DagInstance(experience_uri="experience:test", dag=Dag.from_content(compile_dag(source)))


def descriptions(run: DagInstance, node_id: int = 4) -> list[str]:
    return [condition.description for condition in run.trigger_conditions(node_id)]


def test_any_of_compiles_to_a_native_logic_node_without_a_provider():
    built = instance().dag
    node = built.nodes[3]
    assert node.node_type == "AnyOfNode"
    assert node.predecessors == [1, 2]
    assert "slot_provider" not in node.model_dump()
    assert built.nodes[4].predecessors == [3]
    assert Dag.from_content(built.to_content()) == built


@pytest.mark.parametrize(
    ("a", "b", "witness"),
    [
        (True, False, "A"),
        (False, True, "B"),
        (True, None, "A"),
        (None, True, "B"),
        (True, True, "A"),
        (False, False, None),
        (False, None, None),
        (None, False, None),
        (None, None, None),
    ],
)
def test_any_of_uses_current_positive_inputs_and_reports_one_sufficient_witness(a, b, witness):
    run = instance()
    run.merge_slot_values(
        {key: value for key, value in {"a": a, "b": b}.items() if value is not None}
    )
    actions, waiting = run.advance()
    assert [action.slot_name for action in actions] == (["reminder"] if witness else [])
    assert 3 not in waiting
    assert "either" not in run.slot_values
    if witness:
        assert descriptions(run) == [f"Situation {witness} holds"]


@pytest.mark.parametrize("confirmed", [False, True])
def test_any_of_composes_with_ordinary_and_predecessors(confirmed):
    run = instance(SOURCE + '\nconfirmed = check("The user confirmed")\nconfirmed.then(reminder)')
    run.merge_slot_values({"a": True, "confirmed": confirmed})
    actions, _ = run.advance()
    assert [action.slot_name for action in actions] == (["reminder"] if confirmed else [])
    if confirmed:
        assert descriptions(run) == ["Situation A holds", "The user confirmed"]


def test_nested_any_of_is_computed_without_model_slots():
    run = instance("""dag = workflow("Nested OR")
a = check("A")
b = check("B")
c = check("C")
ab = any_of(a, b)
abc = any_of(ab, c)
r = tell("Act")
abc.then(r)""")
    run.merge_slot_values({"b": True})
    actions, waiting = run.advance()
    assert [a.slot_name for a in actions] == ["r"]
    assert set(waiting) <= {1, 2, 3}
    assert descriptions(run, 6) == ["B"]
    assert run.slot_values == {"b": True}


def test_unreachable_true_slot_does_not_satisfy_any_of():
    run = instance("""dag = workflow("Reachable evidence only")
gate = check("Prerequisite")
a = check("A")
b = check("B")
gate.then(a)
either = any_of(a, b)
r = tell("Act")
either.then(r)""")
    run.merge_slot_values({"gate": False, "a": True, "b": False})
    assert run.advance()[0] == []
    run.merge_slot_values({"gate": True})
    assert [action.slot_name for action in run.advance()[0]] == ["r"]
    assert descriptions(run, 5) == ["Prerequisite", "A"]


def test_any_of_joins_selected_branch_paths_not_unselected_true_slots():
    run = instance("""dag = workflow("Alternative paths")
route = choose("Which route?")
a = check("A")
b = check("B")
route.case("left", a)
route.case("right", b)
either = any_of(a, b)
r = tell("Act")
either.then(r)""")
    run.merge_slot_values({"route": "left", "a": False, "b": True})
    assert run.advance()[0] == []
    run.merge_slot_values({"route": "right"})
    assert [action.slot_name for action in run.advance()[0]] == ["r"]
    conditions = run.trigger_conditions(5)
    assert conditions[0].operator == "equals"
    assert conditions[0].values == ["right"]
    assert [item.description for item in conditions] == ["Which route?", "B"]


def test_any_of_recomputes_after_completion_instead_of_latching_true():
    run = instance()
    run.merge_slot_values({"a": True, "b": True, "reminder": True})
    assert run.advance() == ([], [])
    assert run.state == "completed"
    run.merge_slot_values({"a": False, "b": False, "reminder": False})
    assert run.advance()[0] == []
    run.merge_slot_values({"b": True})
    assert [action.slot_name for action in run.advance()[0]] == ["reminder"]
    assert descriptions(run) == ["Situation B holds"]


def test_any_of_does_not_accept_external_slot_values():
    run = instance()
    with pytest.raises(ValueError, match="locally"):
        run.merge_slot_values({"either": True})
    assert run.advance()[0] == []


@pytest.mark.parametrize(
    "expression",
    ["any_of()", 'any_of("a", b)', "any_of(a, a)", "any_of(dag, a)", "any_of(a, b, True)"],
)
def test_any_of_rejects_invalid_or_duplicate_inputs(expression):
    with pytest.raises(ValueError):
        instance(SOURCE.replace("any_of(a, b)", expression))


def test_any_of_preserves_cycle_validation():
    with pytest.raises(ValueError, match="cycle"):
        instance(SOURCE + "\neither.then(a)")


def test_any_of_does_not_silently_drop_branch_selector_inputs():
    with pytest.raises(ValueError, match="branch"):
        instance(SOURCE + "\na.if_true(reminder)")


def test_any_of_cannot_be_retyped_as_a_model_evaluated_branch():
    with pytest.raises(ValueError, match="any_of"):
        instance(SOURCE.replace("either.then(reminder)", "either.if_true(reminder)"))
