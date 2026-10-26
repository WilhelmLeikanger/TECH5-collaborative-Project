"""Tests that engine output matches what Component A reads.

Checks the fields, indicator keys and status values listed in
docs/engine_integration_checklist.md. The tests run against Team B's
AssignmentEngine once execute_run() is implemented, and against
MockAssignmentEngine until then, so the same checks apply to both.
"""

import pathlib
from typing import Any

import pytest

from app import create_app
from app.contract import contract
from app.contract import mocks
from app.domain import models
from app.engine import solvers
from app.persistence import repositories
from app.samples import demo_data

_RUN_STATUSES = {status.value for status in contract.RunStatus}

_INDICATOR_KEYS = {
    "feasibility": {
        "is_feasible",
        "violations",
        "assigned_count",
        "total_students",
    },
    "efficiency": {
        "mean_skill_fit",
        "unmet_soft_requirements_count",
        "total_skill_fit_sum",
    },
    "fairness": {
        "mean_rank",
        "worst_rank",
        "rank_gini",
        "rank_variance",
        "unranked_count",
        "unranked_pct",
        "rank_distribution",
    },
    "capacity": {
        "total_assigned",
        "total_capacity",
        "overall_utilization_pct",
        "projects",
    },
}

_PROJECT_CAPACITY_KEYS = {
    "assigned_count",
    "min_capacity",
    "max_capacity",
    "utilization_pct",
}


def _request(scenario: dict[str, Any]) -> contract.RunRequest:
    """Builds a run request from a demo scenario."""
    return contract.RunRequest(
        students=scenario["students"],
        projects=scenario["projects"],
        skills=scenario["skills"],
        fixed_assignments=scenario["fixed_assignments"],
        strategy_key="PREFERENCE",
        max_alternatives=3,
    )


def _execute(
    request: contract.RunRequest,
) -> tuple[contract.AssignmentEngineContract, models.AssignmentRun]:
    """Runs the real engine, falling back to the mock while unimplemented."""
    engine: contract.AssignmentEngineContract = solvers.AssignmentEngine()
    try:
        run = engine.execute_run(request)
    except NotImplementedError:
        engine = mocks.MockAssignmentEngine()
        run = engine.execute_run(request)
    return engine, run


def _saved_run(run_id: str) -> models.AssignmentRun:
    """Loads a run the way the web pages do, from the database."""
    saved = repositories.RunRepository().get_run(run_id)
    assert saved is not None, "execute_run() must save the run"
    return saved


@pytest.fixture(name="fixed_run")
def fixture_fixed_run(
    tmp_path: pathlib.Path,
) -> tuple[contract.AssignmentEngineContract, dict[str, Any], str]:
    """Executes a run on the fixed-assignment scenario (S02 locked to P2)."""
    create_app({"TESTING": True, "DATABASE_PATH": tmp_path / "engine.db"})
    scenario = demo_data.get_fixed_assignment_scenario()
    engine, run = _execute(_request(scenario))
    return engine, scenario, run.id


def test_run_is_saved_with_plain_string_status(
    fixed_run: tuple[contract.AssignmentEngineContract, dict[str, Any], str],
) -> None:
    """Tests that the run is saved and its status is a plain string."""
    _, _, run_id = fixed_run
    run = _saved_run(run_id)

    assert isinstance(run.status, str)
    assert run.status in _RUN_STATUSES
    assert run.status == "COMPLETED"
    assert "primary_strategy" in run.configuration


def test_solutions_and_assignments(
    fixed_run: tuple[contract.AssignmentEngineContract, dict[str, Any], str],
) -> None:
    """Tests solution numbering, assignment fields and fixed locks."""
    _, scenario, run_id = fixed_run
    run = _saved_run(run_id)
    student_ids = {s.id for s in scenario["students"]}
    project_ids = {p.id for p in scenario["projects"]}

    assert len(run.solutions) >= 2
    assert [s.alternative_number for s in run.solutions] == list(
        range(1, len(run.solutions) + 1)
    )
    for solution in run.solutions:
        assert solution.run_id == run.id
        assert solution.strategy_name
        assert {a.student_id for a in solution.assignments} == student_ids
        assert len(solution.assignments) == len(student_ids)
        for asg in solution.assignments:
            assert asg.project_id in project_ids
            assert asg.preference_rank is None or asg.preference_rank >= 1
            assert 0.0 <= asg.skill_fit_score <= 1.0
            assert isinstance(asg.unmet_requirements, list)

        # The fixed assignment is kept and flagged in every solution
        s02 = next(a for a in solution.assignments if a.student_id == "S02")
        assert s02.project_id == "P2"
        assert s02.is_fixed


def test_indicator_keys(
    fixed_run: tuple[contract.AssignmentEngineContract, dict[str, Any], str],
) -> None:
    """Tests that every indicator read by the pages is present."""
    _, scenario, run_id = fixed_run
    run = _saved_run(run_id)
    project_ids = {p.id for p in scenario["projects"]}

    for solution in run.solutions:
        indicators = solution.indicators
        for group, keys in _INDICATOR_KEYS.items():
            missing = keys - set(indicators.get(group, {}))
            assert not missing, f"indicators['{group}'] lacks {missing}"

        fairness = indicators["fairness"]
        assert 0.0 <= indicators["efficiency"]["mean_skill_fit"] <= 1.0
        assert isinstance(fairness["unranked_count"], int)
        assert all(
            key.startswith("rank_") for key in fairness["rank_distribution"]
        )

        projects = indicators["capacity"]["projects"]
        assert set(projects) == project_ids
        for p_info in projects.values():
            assert _PROJECT_CAPACITY_KEYS <= set(p_info)


def test_compare_solutions_fields(
    fixed_run: tuple[contract.AssignmentEngineContract, dict[str, Any], str],
) -> None:
    """Tests the comparison fields read by the compare page."""
    engine, _, run_id = fixed_run
    run = _saved_run(run_id)
    sol_a, sol_b = run.solutions[0], run.solutions[1]

    try:
        comparison = engine.compare_solutions(sol_a.id, sol_b.id)
    except NotImplementedError:
        engine = mocks.MockAssignmentEngine()
        comparison = engine.compare_solutions(sol_a.id, sol_b.id)

    assert isinstance(comparison, models.RunComparison)
    assert comparison.solution_a_id == sol_a.id
    assert comparison.solution_b_id == sol_b.id
    assert isinstance(comparison.is_distinct, bool)
    assert isinstance(comparison.changed_students_count, int)
    assert isinstance(comparison.changed_assignments, list)
    assert isinstance(comparison.rank_improved_count, int)
    assert isinstance(comparison.rank_worsened_count, int)
    assert isinstance(comparison.delta_mean_skill_fit, (int, float))
    assert isinstance(comparison.delta_mean_rank, (int, float))
    assert isinstance(comparison.trade_off_summary, list)

    with pytest.raises(KeyError):
        engine.compare_solutions(sol_a.id, "non_existent_id")


def test_infeasible_run_is_saved_with_diagnostics(
    tmp_path: pathlib.Path,
) -> None:
    """Tests that an infeasible run is saved with diagnostics to display."""
    create_app({"TESTING": True, "DATABASE_PATH": tmp_path / "infeas.db"})
    _, run = _execute(_request(demo_data.get_infeasible_scenario()))
    saved = _saved_run(run.id)

    assert saved.status == "INFEASIBLE"
    assert not saved.solutions
    assert saved.diagnostics
    shown_keys = {"message", "reasons", "errors", "warnings", "notes"}
    assert shown_keys & set(saved.diagnostics)
