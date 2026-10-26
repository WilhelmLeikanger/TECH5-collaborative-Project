"""Unit tests for the ILP model, the three strategies and alternatives.

These tests call the solver directly (no database, no web app), so they
check Team B's assignment method independently of execute_run.
"""

import collections

import pytest

from app.domain import models
from app.engine import solvers
from app.samples import demo_data


def _student(
    s_id: str, prefs: list[str], skills: dict[str, int] | None = None
) -> models.Student:
    """Builds a student whose preferences are ranked in list order."""
    return models.Student(
        id=s_id,
        display_name=s_id,
        skills=skills or {},
        preferences=[
            models.Preference(s_id, p_id, rank)
            for rank, p_id in enumerate(prefs, start=1)
        ],
    )


def _project(
    p_id: str,
    min_cap: int = 0,
    max_cap: int = 1,
    requirements: list[models.ProjectRequirement] | None = None,
) -> models.Project:
    return models.Project(
        id=p_id,
        title=p_id,
        min_capacity=min_cap,
        max_capacity=max_cap,
        requirements=requirements or [],
    )


def _engine() -> solvers.AssignmentEngine:
    """Engine with a dummy repository: these tests never touch the DB."""
    return solvers.AssignmentEngine(run_repo=object())


def _assert_feasible(
    assignments: list[models.Assignment],
    students: list[models.Student],
    projects: list[models.Project],
) -> None:
    """Checks one project per student and every capacity bound."""
    assert sorted(a.student_id for a in assignments) == sorted(
        s.id for s in students
    )
    headcount = collections.Counter(a.project_id for a in assignments)
    for p in projects:
        assert p.min_capacity <= headcount[p.id] <= p.max_capacity


def _rank_conflict_case() -> tuple[list, list]:
    """4 students, 4 one-seat projects where the lowest total rank (7)
    forces someone into their 4th choice, but worst rank 3 is possible."""
    students = [
        _student("S1", ["D", "C", "A", "B"]),
        _student("S2", ["D", "C", "A", "B"]),
        _student("S3", ["A", "C", "B", "D"]),
        _student("S4", ["C", "A", "D", "B"]),
    ]
    projects = [_project(p) for p in "ABCD"]
    return students, projects


def test_engine_registers_three_strategies() -> None:
    assert set(_engine().strategies) == {"PREFERENCE", "SKILL_FIT", "BALANCED"}


@pytest.mark.parametrize(
    "strategy", ["PREFERENCE", "SKILL_FIT", "BALANCED"]
)
def test_every_strategy_respects_hard_constraints(strategy: str) -> None:
    scenario = demo_data.get_standard_scenario()
    solver = _engine().strategies[strategy]
    status, assignments, objective, diag = solver.solve(
        scenario["students"], scenario["projects"]
    )
    assert status == solvers.STATUS_OPTIMAL
    assert objective is not None
    assert diag["variables_count"] > 0
    _assert_feasible(assignments, scenario["students"], scenario["projects"])


def test_preference_minimizes_total_rank() -> None:
    students, projects = _rank_conflict_case()
    _, assignments, _, _ = solvers.PreferenceOrientedSolver().solve(
        students, projects
    )
    ranks = [a.preference_rank for a in assignments]
    assert sum(ranks) == 7
    assert max(ranks) == 4


def test_balanced_protects_worst_off_student() -> None:
    students, projects = _rank_conflict_case()
    _, assignments, _, diag = solvers.BalancedTradeoffSolver().solve(
        students, projects
    )
    assert max(a.preference_rank for a in assignments) == 3
    assert diag["worst_rank_bound"] == 3


def test_skill_fit_prefers_qualified_students() -> None:
    # Both students want P1 first, but only S2 has the SQL it requires.
    req = models.ProjectRequirement("P1", "sql", minimum_level=3)
    projects = [_project("P1", requirements=[req]), _project("P2")]
    students = [
        _student("S1", ["P1", "P2"], {"sql": 1}),
        _student("S2", ["P1", "P2"], {"sql": 4}),
    ]
    _, assignments, _, _ = solvers.SkillFitOrientedSolver().solve(
        students, projects
    )
    project_of = {a.student_id: a.project_id for a in assignments}
    assert project_of["S2"] == "P1"


def test_fixed_assignment_holds_in_all_alternatives() -> None:
    scenario = demo_data.get_fixed_assignment_scenario()
    result = _engine().generate_alternatives(
        scenario["students"],
        scenario["projects"],
        scenario["fixed_assignments"],
        max_alternatives=3,
    )
    assert len(result.alternatives) == 3
    for alt in result.alternatives:
        s02 = [a for a in alt.assignments if a.student_id == "S02"]
        assert s02[0].project_id == "P2"
        assert s02[0].is_fixed
        assert all(not a.is_fixed for a in alt.assignments if a is not s02[0])


def test_mandatory_skill_needs_one_qualified_member() -> None:
    req = models.ProjectRequirement(
        "P1", "rust", minimum_level=4, is_mandatory=True
    )
    projects = [
        _project("P1", min_cap=2, max_cap=2, requirements=[req]),
        _project("P2", max_cap=2),
    ]
    # The only expert (S3) prefers P2, so only the hard constraint moves
    # S3 to P1 instead of two of the students who prefer P1.
    students = [
        _student("S1", ["P1", "P2"], {"rust": 1}),
        _student("S2", ["P1", "P2"], {"rust": 2}),
        _student("S3", ["P2", "P1"], {"rust": 5}),
        _student("S4", ["P1", "P2"]),
    ]
    _, assignments, _, _ = solvers.PreferenceOrientedSolver().solve(
        students, projects
    )
    on_p1 = {a.student_id for a in assignments if a.project_id == "P1"}
    assert "S3" in on_p1


def test_empty_project_needs_no_mandatory_skill() -> None:
    # P2 needs an expert nobody has; it may stay empty since its minimum is 0.
    req = models.ProjectRequirement(
        "P2", "rust", minimum_level=5, is_mandatory=True
    )
    projects = [_project("P1", max_cap=2), _project("P2", requirements=[req])]
    students = [_student("S1", ["P2", "P1"]), _student("S2", ["P2", "P1"])]
    status, assignments, _, _ = solvers.PreferenceOrientedSolver().solve(
        students, projects
    )
    assert status == solvers.STATUS_OPTIMAL
    assert {a.project_id for a in assignments} == {"P1"}


def test_unranked_project_gets_no_rank() -> None:
    # S1 ranks only P1, which is full of fixed students.
    projects = [_project("P1", max_cap=1), _project("P2", max_cap=1)]
    students = [_student("S1", ["P1"]), _student("S2", ["P1", "P2"])]
    fixed = [models.FixedAssignment("S2", "P1")]
    _, assignments, _, _ = solvers.PreferenceOrientedSolver().solve(
        students, projects, fixed
    )
    s1 = [a for a in assignments if a.student_id == "S1"][0]
    assert s1.project_id == "P2"
    assert s1.preference_rank is None


def test_alternatives_are_pairwise_distinct() -> None:
    scenario = demo_data.get_standard_scenario()
    result = _engine().generate_alternatives(
        scenario["students"], scenario["projects"], max_alternatives=4
    )
    assert result.diagnostics["distinct_alternatives"] == 4
    seen = [frozenset(alt.pairs()) for alt in result.alternatives]
    assert len(set(seen)) == len(seen)


def test_first_alternative_uses_selected_strategy() -> None:
    scenario = demo_data.get_standard_scenario()
    result = _engine().generate_alternatives(
        scenario["students"],
        scenario["projects"],
        strategy_key="skill_fit",
        max_alternatives=2,
    )
    names = [alt.strategy_name for alt in result.alternatives]
    assert names == ["SKILL_FIT", "PREFERENCE"]


def test_single_feasible_solution_is_reported_honestly() -> None:
    result = _engine().generate_alternatives(
        [_student("S1", ["P1"])],
        [_project("P1", min_cap=1, max_cap=1)],
        max_alternatives=3,
    )
    assert result.status == solvers.STATUS_OPTIMAL
    assert result.diagnostics["distinct_alternatives"] == 1
    assert "Only 1 distinct" in result.diagnostics["stop_reason"]


def test_exactly_two_solutions_exist() -> None:
    projects = [_project("P1", 1, 1), _project("P2", 1, 1)]
    students = [_student("S1", ["P1", "P2"]), _student("S2", ["P1", "P2"])]
    result = _engine().generate_alternatives(
        students, projects, max_alternatives=3
    )
    assert result.diagnostics["distinct_alternatives"] == 2


def test_infeasible_problem_returns_no_solutions() -> None:
    # P1 needs at least 2 students, but only 1 exists.
    result = _engine().generate_alternatives(
        [_student("S1", ["P1"])],
        [_project("P1", min_cap=2, max_cap=2)],
    )
    assert result.status == solvers.STATUS_INFEASIBLE
    assert result.alternatives == []
    assert "No assignment" in result.diagnostics["stop_reason"]


def test_infeasibility_found_only_by_solver() -> None:
    # Validation passes (someone has the skill), but both projects need the
    # single qualified student, so only the solver can prove infeasibility.
    def req(p_id: str) -> models.ProjectRequirement:
        return models.ProjectRequirement(
            p_id, "sql", minimum_level=5, is_mandatory=True
        )

    projects = [
        _project("P1", min_cap=1, max_cap=3, requirements=[req("P1")]),
        _project("P2", min_cap=1, max_cap=3, requirements=[req("P2")]),
    ]
    students = [
        _student("S1", ["P1", "P2"], {"sql": 5}),
        _student("S2", ["P1", "P2"]),
        _student("S3", ["P2", "P1"]),
    ]
    result = _engine().generate_alternatives(students, projects)
    assert result.status == solvers.STATUS_INFEASIBLE
    assert result.alternatives == []


def test_unknown_strategy_is_rejected() -> None:
    with pytest.raises(KeyError):
        _engine().generate_alternatives(
            [_student("S1", ["P1"])], [_project("P1")], strategy_key="RANDOM"
        )
