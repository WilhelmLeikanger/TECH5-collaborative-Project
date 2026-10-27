"""Calculators for feasibility, efficiency, fairness, and trade-offs.

Each indicator follows the definitions in the analytical note
(docs/analytical_note.tex, Section 3). Indicators are computed from the
solution itself, independently of the solver, so a solver or data error is
reported as a feasibility violation instead of passing unnoticed.

Ranks: an assignment to a project the student did not rank has
preference_rank None. For rank statistics it counts as |P| + 1, worse than
any ranked project, and it is also counted in unranked_count.
"""

import collections
import statistics
from typing import Any

from app.domain import models

# Indicator values are rounded to this many decimals for display and storage.
DECIMALS = 4


def calculate_assignment_skill_fit(
    student: models.Student,
    project: models.Project,
) -> tuple[float, list[str]]:
    """Calculates skill fit score and unmet requirements for a student-project pair.

    The score is the average, over all of the project's requirements, of how
    much of the required level the student reaches:

        fit = mean over requirements k of min(1, student_level_k / required_k)

    A requirement is unmet when the student's level is below the required
    level. A project without requirements is a neutral, perfect fit (1.0).

    Args:
        student: The student under evaluation.
        project: The project under evaluation.

    Returns:
        A tuple of (skill_fit_score [0.0, 1.0], list_of_unmet_skill_ids).
    """
    if not project.requirements:
        return 1.0, []

    total = 0.0
    unmet: list[str] = []
    for req in project.requirements:
        level = student.get_skill_level(req.skill_id)
        required = max(req.minimum_level, 1)
        total += min(1.0, level / required)
        if level < required:
            unmet.append(req.skill_id)

    return round(total / len(project.requirements), DECIMALS), unmet


def calculate_gini(values: list[float]) -> float:
    """Calculates the Gini coefficient of inequality for non-negative values.

        G = sum_i sum_j |v_i - v_j| / (2 * n * sum_i v_i)

    0.0 means every value is equal; values closer to 1.0 mean that the total
    is concentrated on a few elements. Applied to preference ranks, a high G
    means that disappointment is concentrated on a few students.

    Args:
        values: Non-negative numerical series (e.g. assigned preference ranks).

    Returns:
        The Gini coefficient in [0.0, 1.0); 0.0 for an empty or all-zero list.
    """
    n = len(values)
    total = sum(values)
    if n == 0 or total == 0:
        return 0.0
    abs_diffs = sum(abs(a - b) for a in values for b in values)
    return round(abs_diffs / (2 * n * total), DECIMALS)


def rank_value(rank: int | None, num_projects: int) -> int:
    """Returns the rank used in statistics: |P| + 1 for unranked projects."""
    return rank if rank is not None else num_projects + 1


def compute_solution_indicators(
    students: list[models.Student],
    projects: list[models.Project],
    assignments: list[models.Assignment],
    fixed_assignments: list[models.FixedAssignment] | None = None,
) -> dict[str, Any]:
    """Computes comprehensive feasibility, efficiency, and fairness indicators.

    Evaluates a solution across the four dimensions defined in the specification:
    1. Feasibility: Exact student count, capacity bounds, fixed assignments,
       mandatory skills.
    2. Efficiency: Mean skill-fit score, unmet desired (soft) requirements.
    3. Fairness: Rank distribution, mean rank, worst rank, unranked count, Gini.
    4. Capacity: Total assigned, total capacity, overall utilization, per-project.

    Args:
        students: Complete cohort of students.
        projects: Catalog of semester projects.
        assignments: List of student-to-project allocations.
        fixed_assignments: Optional list of locked pre-allocations.

    Returns:
        Dictionary with "feasibility", "efficiency", "fairness" and
        "capacity" groups (keys listed in docs/engine_integration_checklist.md).
    """
    return {
        "feasibility": _feasibility(
            students, projects, assignments, fixed_assignments or []
        ),
        "efficiency": _efficiency(students, projects, assignments),
        "fairness": _fairness(students, projects, assignments),
        "capacity": _capacity(projects, assignments),
    }


def _feasibility(
    students: list[models.Student],
    projects: list[models.Project],
    assignments: list[models.Assignment],
    fixed_assignments: list[models.FixedAssignment],
) -> dict[str, Any]:
    """Re-checks every hard constraint and lists violations by name."""
    violations: list[str] = []
    student_by_id = {s.id: s for s in students}
    project_by_id = {p.id: p for p in projects}

    count_per_student = collections.Counter(a.student_id for a in assignments)
    for s in students:
        if count_per_student[s.id] == 0:
            violations.append(f"Student '{s.id}' is not assigned.")
        elif count_per_student[s.id] > 1:
            violations.append(
                f"Student '{s.id}' is assigned {count_per_student[s.id]} times."
            )
    for a in assignments:
        if a.project_id not in project_by_id:
            violations.append(
                f"Student '{a.student_id}' is assigned to unknown project "
                f"'{a.project_id}'."
            )

    members = _members(assignments)
    for p in projects:
        headcount = len(members[p.id])
        if headcount < p.min_capacity:
            violations.append(
                f"Project '{p.id}' has {headcount} student(s), below its "
                f"minimum of {p.min_capacity}."
            )
        if headcount > p.max_capacity:
            violations.append(
                f"Project '{p.id}' has {headcount} student(s), above its "
                f"maximum of {p.max_capacity}."
            )
        if headcount == 0:
            continue
        for req in p.requirements:
            if not req.is_mandatory:
                continue
            covered = any(
                s_id in student_by_id
                and student_by_id[s_id].get_skill_level(req.skill_id)
                >= req.minimum_level
                for s_id in members[p.id]
            )
            if not covered:
                violations.append(
                    f"Project '{p.id}' has no student with mandatory skill "
                    f"'{req.skill_id}' at level {req.minimum_level}."
                )

    project_of = {a.student_id: a.project_id for a in assignments}
    for fa in fixed_assignments:
        if project_of.get(fa.student_id) != fa.project_id:
            violations.append(
                f"Fixed assignment {fa.student_id} -> {fa.project_id} is not "
                "kept."
            )

    return {
        "is_feasible": not violations,
        "violations": violations,
        "assigned_count": len(assignments),
        "total_students": len(students),
    }


def _efficiency(
    students: list[models.Student],
    projects: list[models.Project],
    assignments: list[models.Assignment],
) -> dict[str, Any]:
    """Mean skill fit and the number of unmet desired requirements."""
    student_by_id = {s.id: s for s in students}
    project_by_id = {p.id: p for p in projects}
    fits = [a.skill_fit_score for a in assignments]

    unmet_soft = 0
    for a in assignments:
        student = student_by_id.get(a.student_id)
        project = project_by_id.get(a.project_id)
        if student is None or project is None:
            continue
        for req in project.requirements:
            if (
                not req.is_mandatory
                and student.get_skill_level(req.skill_id) < req.minimum_level
            ):
                unmet_soft += 1

    return {
        "mean_skill_fit": round(statistics.mean(fits), DECIMALS) if fits else 0.0,
        "unmet_soft_requirements_count": unmet_soft,
        "total_skill_fit_sum": round(sum(fits), DECIMALS),
    }


def _fairness(
    students: list[models.Student],
    projects: list[models.Project],
    assignments: list[models.Assignment],
) -> dict[str, Any]:
    """Rank distribution, mean and worst rank, unranked count, Gini, variance."""
    num_projects = len(projects)
    ranks = [rank_value(a.preference_rank, num_projects) for a in assignments]
    unranked = sum(a.preference_rank is None for a in assignments)
    n = len(assignments)

    distribution: dict[str, int] = {}
    for rank in sorted({a.preference_rank for a in assignments} - {None}):
        distribution[f"rank_{rank}"] = sum(
            a.preference_rank == rank for a in assignments
        )
    if unranked:
        distribution["rank_unranked"] = unranked

    return {
        "mean_rank": round(statistics.mean(ranks), DECIMALS) if ranks else 0.0,
        "worst_rank": max(ranks) if ranks else 0,
        "rank_gini": calculate_gini([float(r) for r in ranks]),
        "rank_variance": (
            round(statistics.pvariance(ranks), DECIMALS) if ranks else 0.0
        ),
        "unranked_count": unranked,
        "unranked_pct": round(100.0 * unranked / n, 1) if n else 0.0,
        "top_choice_count": sum(r == 1 for r in ranks),
        "rank_distribution": distribution,
        "unranked_rank_value": num_projects + 1,
        "total_students": len(students),
    }


def _capacity(
    projects: list[models.Project],
    assignments: list[models.Assignment],
) -> dict[str, Any]:
    """Overall and per-project capacity utilization."""
    members = _members(assignments)
    total_capacity = sum(p.max_capacity for p in projects)
    per_project: dict[str, dict[str, Any]] = {}
    for p in projects:
        assigned = len(members[p.id])
        per_project[p.id] = {
            "assigned_count": assigned,
            "min_capacity": p.min_capacity,
            "max_capacity": p.max_capacity,
            "utilization_pct": (
                round(100.0 * assigned / p.max_capacity, 1)
                if p.max_capacity > 0
                else 0.0
            ),
            "at_minimum": assigned == p.min_capacity,
            "at_maximum": assigned == p.max_capacity,
        }
    return {
        "total_assigned": len(assignments),
        "total_capacity": total_capacity,
        "overall_utilization_pct": (
            round(100.0 * len(assignments) / total_capacity, 1)
            if total_capacity
            else 0.0
        ),
        "projects": per_project,
    }


def _members(assignments: list[models.Assignment]) -> dict[str, list[str]]:
    """Maps project id to the ids of the students assigned to it."""
    members: dict[str, list[str]] = collections.defaultdict(list)
    for a in assignments:
        members[a.project_id].append(a.student_id)
    return members


def dominates(solution_a: models.Solution, solution_b: models.Solution) -> bool:
    """Returns True if A dominates B on mean rank and mean skill fit.

    A dominates B when A is at least as good on both (lower or equal mean
    rank, higher or equal mean skill fit) and strictly better on at least
    one. A dominated alternative offers no real trade-off.
    """
    rank_a = solution_a.indicators.get("fairness", {}).get("mean_rank")
    rank_b = solution_b.indicators.get("fairness", {}).get("mean_rank")
    fit_a = solution_a.indicators.get("efficiency", {}).get("mean_skill_fit")
    fit_b = solution_b.indicators.get("efficiency", {}).get("mean_skill_fit")
    if None in (rank_a, rank_b, fit_a, fit_b):
        return False
    at_least_as_good = rank_a <= rank_b and fit_a >= fit_b
    strictly_better = rank_a < rank_b or fit_a > fit_b
    return at_least_as_good and strictly_better


def find_dominating_solutions(
    solution: models.Solution, solutions: list[models.Solution]
) -> list[models.Solution]:
    """Returns every other solution that dominates this one."""
    return [
        other
        for other in solutions
        if other.id != solution.id and dominates(other, solution)
    ]


def compare_two_solutions(
    solution_a: models.Solution,
    solution_b: models.Solution,
) -> dict[str, Any]:
    """Performs detailed trade-off comparison between two alternative solutions.

    Deltas are B - A. A positive skill-fit delta is an improvement; positive
    rank, worst-rank and Gini deltas are deteriorations.

    Args:
        solution_a: Baseline solution.
        solution_b: Alternative solution to compare against A.

    Returns:
        Dictionary documenting differences, changed assignments, and
        trade-offs, in the shape of models.RunComparison.from_dict().
    """
    ind_a, ind_b = solution_a.indicators, solution_b.indicators
    unranked_value = ind_a.get("fairness", {}).get("unranked_rank_value") or (
        _max_rank(solution_a, solution_b) + 1
    )
    map_b = {a.student_id: a for a in solution_b.assignments}

    changed: list[dict[str, Any]] = []
    improved = worsened = 0
    for asg_a in solution_a.assignments:
        asg_b = map_b.get(asg_a.student_id)
        if asg_b is None or asg_a.project_id == asg_b.project_id:
            continue
        rank_delta = rank_value(asg_b.preference_rank, unranked_value - 1) - (
            rank_value(asg_a.preference_rank, unranked_value - 1)
        )
        if rank_delta < 0:
            improved += 1
            rank_change = "IMPROVED"
        elif rank_delta > 0:
            worsened += 1
            rank_change = "WORSENED"
        else:
            rank_change = "SAME"
        changed.append(
            {
                "student_id": asg_a.student_id,
                "student_name": asg_a.student_id,
                "project_a": asg_a.project_id,
                "project_a_id": asg_a.project_id,
                "project_a_title": asg_a.project_id,
                "rank_a": asg_a.preference_rank,
                "skill_fit_a": asg_a.skill_fit_score,
                "project_b": asg_b.project_id,
                "project_b_id": asg_b.project_id,
                "project_b_title": asg_b.project_id,
                "rank_b": asg_b.preference_rank,
                "skill_fit_b": asg_b.skill_fit_score,
                "rank_delta": rank_delta,
                "rank_change": rank_change,
            }
        )

    def delta(group: str, key: str, decimals: int = DECIMALS) -> float:
        value_a = ind_a.get(group, {}).get(key, 0) or 0
        value_b = ind_b.get(group, {}).get(key, 0) or 0
        return round(value_b - value_a, decimals)

    delta_metrics = {
        "mean_skill_fit_delta": delta("efficiency", "mean_skill_fit"),
        "mean_rank_delta": delta("fairness", "mean_rank"),
        "worst_rank_delta": delta("fairness", "worst_rank"),
        "gini_delta": delta("fairness", "rank_gini"),
        "unranked_count_delta": int(delta("fairness", "unranked_count")),
        "unmet_soft_requirements_delta": int(
            delta("efficiency", "unmet_soft_requirements_count")
        ),
    }

    return {
        "solution_a_id": solution_a.id,
        "solution_a_alt": solution_a.alternative_number,
        "solution_b_id": solution_b.id,
        "solution_b_alt": solution_b.alternative_number,
        "is_distinct": bool(changed),
        "changed_students_count": len(changed),
        "total_students": len(solution_a.assignments),
        "changed_assignments": changed,
        "rank_improved_count": improved,
        "rank_worsened_count": worsened,
        "delta_metrics": delta_metrics,
        "delta_mean_skill_fit": delta_metrics["mean_skill_fit_delta"],
        "delta_mean_rank": delta_metrics["mean_rank_delta"],
        "delta_unranked_count": delta_metrics["unranked_count_delta"],
        "trade_off_summary": _trade_off_summary(
            solution_a, solution_b, delta_metrics, len(changed), improved, worsened
        ),
    }


def _max_rank(*solutions: models.Solution) -> int:
    """Highest ranked preference in the solutions (fallback for |P|)."""
    ranks = [
        a.preference_rank
        for s in solutions
        for a in s.assignments
        if a.preference_rank is not None
    ]
    return max(ranks, default=0)


def _trade_off_summary(
    solution_a: models.Solution,
    solution_b: models.Solution,
    deltas: dict[str, Any],
    changed: int,
    improved: int,
    worsened: int,
) -> list[str]:
    """Plain-language statements of what improves and what becomes worse."""
    a, b = solution_a.alternative_number, solution_b.alternative_number
    if changed == 0:
        return [f"Alternatives {a} and {b} assign every student identically."]

    lines = [
        f"{changed} student(s) change project from alternative {a} to {b}: "
        f"{improved} get a better-ranked project, {worsened} a worse one."
    ]
    fit = deltas["mean_skill_fit_delta"]
    rank = deltas["mean_rank_delta"]
    if fit:
        lines.append(
            f"Mean skill fit {'rises' if fit > 0 else 'falls'} by "
            f"{abs(fit) * 100:.1f} percentage points."
        )
    if rank:
        lines.append(
            f"Mean preference rank {'worsens' if rank > 0 else 'improves'} by "
            f"{abs(rank):.2f}."
        )
    if deltas["worst_rank_delta"]:
        worst = deltas["worst_rank_delta"]
        lines.append(
            f"The worst individual rank {'worsens' if worst > 0 else 'improves'} "
            f"by {abs(worst):.0f}."
        )
    if dominates(solution_a, solution_b):
        lines.append(
            f"Alternative {a} dominates {b}: it is at least as good on both "
            "mean rank and skill fit, so there is no real trade-off."
        )
    elif dominates(solution_b, solution_a):
        lines.append(
            f"Alternative {b} dominates {a}: it is at least as good on both "
            "mean rank and skill fit, so there is no real trade-off."
        )
    elif fit and rank:
        lines.append(
            "This is a genuine trade-off between student preferences and "
            "skill fit."
        )
    return lines
