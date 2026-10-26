"""Tests for execute_run outcomes and the metrics added by Team B.

Covers each run status (COMPLETED, INFEASIBLE, INVALID_INPUT), saving of
every run, dominance flagging, and indicator values from the analytical note.
"""

import pathlib

import pytest

from app import create_app
from app.contract import contract
from app.domain import models
from app.engine import metrics
from app.engine import solvers
from app.persistence import repositories
from app.samples import demo_data


def _student(s_id: str, prefs: list[str], skills=None) -> models.Student:
    return models.Student(
        id=s_id,
        display_name=s_id,
        skills=skills or {},
        preferences=[
            models.Preference(s_id, p_id, rank)
            for rank, p_id in enumerate(prefs, start=1)
        ],
    )


def _run(request: contract.RunRequest) -> models.AssignmentRun:
    """Executes a run and returns it as reloaded from the database."""
    run = solvers.AssignmentEngine().execute_run(request)
    saved = repositories.RunRepository().get_run(run.id)
    assert saved is not None, "every run must be saved"
    return saved


@pytest.fixture(autouse=True)
def _database(tmp_path: pathlib.Path) -> None:
    create_app({"TESTING": True, "DATABASE_PATH": tmp_path / "run.db"})


def test_completed_run_has_indicators_and_strategy_order() -> None:
    scenario = demo_data.get_standard_scenario()
    run = _run(
        contract.RunRequest(
            students=scenario["students"],
            projects=scenario["projects"],
            skills=scenario["skills"],
            strategy_key="BALANCED",
        )
    )
    assert run.status == "COMPLETED"
    assert [s.strategy_name for s in run.solutions] == [
        "BALANCED",
        "PREFERENCE",
        "SKILL_FIT",
    ]
    assert run.diagnostics["distinct_alternatives"] == 3
    for solution in run.solutions:
        assert solution.indicators["feasibility"]["is_feasible"]


def test_dominated_alternative_is_flagged() -> None:
    # Values from the analytical note, Table 1: BALANCED (rank 1.10, fit
    # 0.983) is worse on rank than PREFERENCE and worse on fit than SKILL_FIT.
    scenario = demo_data.get_standard_scenario()
    run = _run(
        contract.RunRequest(
            students=scenario["students"],
            projects=scenario["projects"],
            skills=scenario["skills"],
        )
    )
    balanced = run.solutions[2]
    assert balanced.strategy_name == "BALANCED"
    assert balanced.diagnostics["dominated_by"] == [1, 2]
    assert any("Dominated by alternatives 1 and 2" in w for w in balanced.warnings)
    assert "dominated_by" not in run.solutions[0].diagnostics


def test_infeasible_found_by_validation_is_saved_with_reasons() -> None:
    run = _run(
        contract.RunRequest(
            students=[_student("S1", ["P1"]), _student("S2", ["P1"])],
            projects=[models.Project(id="P1", title="P1", max_capacity=1)],
        )
    )
    assert run.status == "INFEASIBLE"
    assert run.solutions == []
    assert any("capacity" in r for r in run.diagnostics["reasons"])


def test_infeasible_found_by_solver_is_saved_with_reasons() -> None:
    def req(p_id: str) -> models.ProjectRequirement:
        return models.ProjectRequirement(p_id, "sql", 5, is_mandatory=True)

    run = _run(
        contract.RunRequest(
            students=[
                _student("S1", ["P1", "P2"], {"sql": 5}),
                _student("S2", ["P1", "P2"]),
                _student("S3", ["P2", "P1"]),
            ],
            projects=[
                models.Project("P1", "P1", min_capacity=1, max_capacity=3,
                               requirements=[req("P1")]),
                models.Project("P2", "P2", min_capacity=1, max_capacity=3,
                               requirements=[req("P2")]),
            ],
            skills=[models.Skill("sql", "SQL")],
        )
    )
    assert run.status == "INFEASIBLE"
    assert run.diagnostics["solver_status"] == "INFEASIBLE"
    assert run.diagnostics["reasons"]


def test_invalid_input_is_rejected_and_saved() -> None:
    run = _run(
        contract.RunRequest(
            students=[_student("S1", ["P_GHOST"])],
            projects=[models.Project(id="P1", title="P1", max_capacity=1)],
        )
    )
    assert run.status == "INVALID_INPUT"
    assert any("P_GHOST" in e for e in run.diagnostics["errors"])


def test_unknown_strategy_is_invalid_input() -> None:
    scenario = demo_data.get_standard_scenario()
    run = _run(
        contract.RunRequest(
            students=scenario["students"],
            projects=scenario["projects"],
            skills=scenario["skills"],
            strategy_key="RANDOM",
        )
    )
    assert run.status == "INVALID_INPUT"


def test_gini_matches_analytical_note_examples() -> None:
    assert metrics.calculate_gini([1, 1, 1, 4]) == pytest.approx(0.3214, abs=1e-4)
    assert metrics.calculate_gini([3, 1, 3, 1]) == 0.25
    assert metrics.calculate_gini([3, 3, 3, 3]) == 0.0
    assert metrics.calculate_gini([]) == 0.0


def test_unranked_assignment_counts_as_num_projects_plus_one() -> None:
    projects = [models.Project(id=p, title=p, max_capacity=1) for p in "AB"]
    students = [_student("S1", ["A"]), _student("S2", ["A", "B"])]
    assignments = [
        models.Assignment("S1", "B", preference_rank=None, skill_fit_score=1.0),
        models.Assignment("S2", "A", preference_rank=1, skill_fit_score=1.0),
    ]
    fairness = metrics.compute_solution_indicators(
        students, projects, assignments
    )["fairness"]
    assert fairness["unranked_count"] == 1
    assert fairness["worst_rank"] == 3
    assert fairness["mean_rank"] == 2.0
    assert fairness["rank_distribution"] == {"rank_1": 1, "rank_unranked": 1}


def test_feasibility_check_reports_violations() -> None:
    req = models.ProjectRequirement("P1", "py", 3, is_mandatory=True)
    projects = [
        models.Project("P1", "P1", min_capacity=2, max_capacity=2,
                       requirements=[req]),
    ]
    students = [_student("S1", ["P1"]), _student("S2", ["P1"])]
    assignments = [models.Assignment("S1", "P1")]
    fixed = [models.FixedAssignment("S2", "P1")]
    feasibility = metrics.compute_solution_indicators(
        students, projects, assignments, fixed
    )["feasibility"]
    assert not feasibility["is_feasible"]
    text = " ".join(feasibility["violations"])
    assert "'S2' is not assigned" in text
    assert "below its minimum" in text
    assert "mandatory skill 'py'" in text
    assert "S2 -> P1 is not kept" in text


def test_compare_counts_unranked_as_worse() -> None:
    def solution(sol_id: str, rank: int | None, project: str) -> models.Solution:
        return models.Solution(
            id=sol_id,
            run_id="r",
            alternative_number=1 if sol_id == "a" else 2,
            strategy_name="X",
            assignments=[models.Assignment("S1", project, preference_rank=rank)],
            indicators={"fairness": {"unranked_rank_value": 4}},
        )

    comparison = metrics.compare_two_solutions(
        solution("a", 1, "P1"), solution("b", None, "P2")
    )
    assert comparison["changed_assignments"][0]["rank_delta"] == 3
    assert comparison["rank_worsened_count"] == 1
