"""Unit tests for optimization solvers and distinct alternative generation."""

import pytest

from app.contract import contract
from app.domain import models
from app.engine import solvers
from app.samples import demo_data


def test_assignment_strategy_abc() -> None:
    """Verifies that AssignmentStrategy cannot be instantiated directly."""
    with pytest.raises(TypeError):
        solvers.AssignmentStrategy()  # type: ignore[abstract]


def test_assignment_engine_initialization() -> None:
    """Verifies that AssignmentEngine registers the three ILP strategies.

    Updated by Team B: the starter version expected an empty registry.
    """
    engine = solvers.AssignmentEngine()
    assert isinstance(engine, contract.AssignmentEngineContract)
    assert isinstance(engine.strategies, dict)
    assert set(engine.strategies) == {"PREFERENCE", "SKILL_FIT", "BALANCED"}


def test_assignment_engine_raises_not_implemented() -> None:
    """Verifies that execute_run raises NotImplementedError for Team B."""
    engine = solvers.AssignmentEngine()
    benchmark = demo_data.get_standard_scenario()
    request = contract.RunRequest(
        students=benchmark["students"],
        projects=benchmark["projects"],
    )
    with pytest.raises(NotImplementedError) as exc_info:
        engine.execute_run(request)
    assert "TODO (Team B)" in str(exc_info.value)


@pytest.mark.xfail(
    raises=NotImplementedError,
    reason="TODO (Team B): Implement optimization solver and alternative generation",
)
def test_distinct_alternatives_generation() -> None:
    """Verifies AssignmentEngine produces distinct alternatives."""
    benchmark = demo_data.get_standard_scenario()
    engine = solvers.AssignmentEngine()

    run = engine.run_assignment(
        students=benchmark["students"],
        projects=benchmark["projects"],
        skills=benchmark["skills"],
        max_alternatives=3,
    )

    assert run.status == "COMPLETED"
    assert len(run.solutions) >= 2

    # Check distinctness: verify that each pair of solutions differs
    for i in range(len(run.solutions)):
        for j in range(i + 1, len(run.solutions)):
            pairs_i = {
                (a.student_id, a.project_id)
                for a in run.solutions[i].assignments
            }
            pairs_j = {
                (a.student_id, a.project_id)
                for a in run.solutions[j].assignments
            }
            assert (
                pairs_i != pairs_j
            ), f"Solution {i+1} and {j+1} are duplicate allocations!"
