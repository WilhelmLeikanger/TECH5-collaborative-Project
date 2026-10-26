"""Integer linear programming solvers and Strategy Pattern for TECH5.

Team B's assignment method is binary integer linear programming (ILP),
modelled with Pyomo and solved with HiGHS (``appsi_highs``), as in TECH4.

The module has three layers:

1. ``AssignmentModel`` builds the ILP: decision variables and every hard
   constraint (one project per student, capacities, fixed assignments,
   mandatory skills, and no-good cuts that exclude earlier solutions).
2. ``AssignmentStrategy`` subclasses decide what "best" means by setting the
   objective: student preferences, skill fit, or a balanced compromise.
3. ``AssignmentEngine`` implements the component contract: it validates the
   input, generates up to K distinct alternatives (one strategy per
   alternative), computes indicators and saves the run.

Notation (see docs/analytical_note.tex): S = students, P = projects,
x[s,p] = 1 if student s is assigned to p, y[p] = 1 if project p has at least
one student, r[s,p] = preference rank (unranked = |P| + 1), u[s,p] =
preference score in [0, 1], sigma[s,p] = skill fit in [0, 1].
"""

import abc
import dataclasses
import logging
import sqlite3
import time
import uuid
from typing import Any

import pyomo.environ as pyo
from pyomo.opt import TerminationCondition

from app import config
from app.contract import contract
from app.domain import models
from app.domain import validation
from app.engine import metrics
from app.persistence import repositories

logger = logging.getLogger(__name__)

SOLVER_NAME = "appsi_highs"

# Weight of the secondary goal in every objective. It is small enough that it
# never outweighs one step of the primary goal, so it only decides between
# solutions that are equally good on the primary goal (deterministic ties).
TIE_BREAK_WEIGHT = 0.001

# Solver status values returned by AssignmentStrategy.solve().
STATUS_OPTIMAL = "OPTIMAL"
STATUS_FEASIBLE = "FEASIBLE"  # Time limit reached with a valid solution.
STATUS_INFEASIBLE = "INFEASIBLE"
STATUS_NOT_SOLVED = "NOT_SOLVED"
SOLVED_STATUSES = (STATUS_OPTIMAL, STATUS_FEASIBLE)

Pair = tuple[str, str]  # (student_id, project_id)
SolveResult = tuple[str, list[models.Assignment], float | None, dict[str, Any]]


def preference_cost(
    student: models.Student, project: models.Project, num_projects: int
) -> int:
    """Returns r[s,p]: the student's rank for the project, |P| + 1 if unranked.

    Args:
        student: The student.
        project: The project.
        num_projects: Number of projects |P| in the cohort.

    Returns:
        The preference rank used as a cost (1 = first choice).
    """
    rank = student.get_preference_rank(project.id)
    return rank if rank is not None else num_projects + 1


def preference_score(
    student: models.Student, project: models.Project, num_projects: int
) -> float:
    """Returns u[s,p] in [0, 1]: 1.0 for a first choice, 0.0 if unranked.

    Args:
        student: The student.
        project: The project.
        num_projects: Number of projects |P| in the cohort.

    Returns:
        The preference rank rescaled so that higher is better.
    """
    rank = preference_cost(student, project, num_projects)
    return (num_projects + 1 - rank) / max(num_projects, 1)


def skill_fit(student: models.Student, project: models.Project) -> float:
    """Returns sigma[s,p], the student's skill fit for the project.

    Args:
        student: The student.
        project: The project.

    Returns:
        Skill fit score in [0, 1] (see metrics.calculate_assignment_skill_fit).
    """
    score, _ = metrics.calculate_assignment_skill_fit(student, project)
    return score


class AssignmentModel:
    """The ILP with all hard constraints, shared by every strategy.

    Strategies add an objective (and optionally extra constraints) and then
    call solve().
    """

    def __init__(
        self,
        name: str,
        students: list[models.Student],
        projects: list[models.Project],
        fixed_assignments: list[models.FixedAssignment] | None = None,
        excluded_solutions: list[list[Pair]] | None = None,
    ) -> None:
        """Builds decision variables and hard constraints.

        Args:
            name: Model name, shown in solver logs.
            students: Students in the cohort.
            projects: Projects in the catalog.
            fixed_assignments: Locks that must hold in every solution.
            excluded_solutions: Earlier solutions, each excluded by a
                no-good cut so that the new solution is distinct.
        """
        self.students = students
        self.projects = projects
        self.pairs: list[Pair] = [
            (s.id, p.id) for s in students for p in projects
        ]

        m = pyo.ConcreteModel(name)
        # x[s,p] = 1 if student s is assigned to project p.
        m.x = pyo.Var(self.pairs, domain=pyo.Binary)
        # y[p] = 1 if project p runs, i.e. has at least one student.
        m.y = pyo.Var([p.id for p in projects], domain=pyo.Binary)
        self.model = m

        self._add_one_project_per_student()
        self._add_capacity_bounds()
        self._add_fixed_assignments(fixed_assignments or [])
        self._add_mandatory_skills()
        self._add_no_good_cuts(excluded_solutions or [])

    def _headcount(self, project_id: str) -> Any:
        """Returns the expression sum over s of x[s,p]."""
        return sum(self.model.x[s.id, project_id] for s in self.students)

    def _add_one_project_per_student(self) -> None:
        """Sum over p of x[s,p] = 1 for every student s."""
        m = self.model
        m.one_project = pyo.Constraint(
            [s.id for s in self.students],
            rule=lambda m, s_id: (
                sum(m.x[s_id, p.id] for p in self.projects) == 1
            ),
        )

    def _add_capacity_bounds(self) -> None:
        """min_p <= headcount_p <= max_p * y[p], and y[p] = 0 if empty.

        The minimum applies to every project (as in input validation). The
        y[p] variable links to the mandatory-skill constraint, so that a
        project with minimum 0 may stay empty instead of needing an expert.
        The maximum constraint is the big-M link of the fixed-charge problem
        with the tightest valid M = max_p.
        """
        m = self.model
        ids = [p.id for p in self.projects]
        by_id = {p.id: p for p in self.projects}
        m.min_cap = pyo.Constraint(
            ids,
            rule=lambda m, p_id: (
                self._headcount(p_id) >= by_id[p_id].min_capacity
            ),
        )
        m.max_cap = pyo.Constraint(
            ids,
            rule=lambda m, p_id: (
                self._headcount(p_id) <= by_id[p_id].max_capacity * m.y[p_id]
            ),
        )
        m.runs = pyo.Constraint(
            ids, rule=lambda m, p_id: self._headcount(p_id) >= m.y[p_id]
        )

    def _add_fixed_assignments(
        self, fixed_assignments: list[models.FixedAssignment]
    ) -> None:
        """x[s,p] = 1 for every administrative fixed assignment (s, p)."""
        m = self.model
        fixed = [
            (fa.student_id, fa.project_id)
            for fa in fixed_assignments
            if (fa.student_id, fa.project_id) in m.x
        ]
        m.fixed = pyo.Constraint(fixed, rule=lambda m, s, p: m.x[s, p] == 1)

    def _add_mandatory_skills(self) -> None:
        """At least one qualified student on every running project.

        For each project p and mandatory requirement k with level L:
            sum over students with level(s, k) >= L of x[s,p] >= y[p]

        This is a set-covering constraint. Assumption (pending instructor
        confirmation): a mandatory requirement is met when at least one
        assigned team member has the required level.
        """
        m = self.model
        m.mandatory = pyo.ConstraintList()
        for p in self.projects:
            for req in p.requirements:
                if not req.is_mandatory:
                    continue
                qualified = [
                    s
                    for s in self.students
                    if s.get_skill_level(req.skill_id) >= req.minimum_level
                ]
                m.mandatory.add(
                    sum(m.x[s.id, p.id] for s in qualified) >= m.y[p.id]
                )

    def _add_no_good_cuts(self, excluded_solutions: list[list[Pair]]) -> None:
        """Forbids repeating an earlier solution A: sum of x over A <= |A| - 1.

        Every student keeping their project would make the sum |A|, so the
        cut forces at least one student to change project.
        """
        m = self.model
        m.no_good = pyo.ConstraintList()
        for solution in excluded_solutions:
            pairs = [pair for pair in solution if pair in m.x]
            if pairs:
                m.no_good.add(sum(m.x[pair] for pair in pairs) <= len(pairs) - 1)

    def set_objective(
        self, weights: dict[Pair, float], sense: Any = pyo.maximize
    ) -> None:
        """Sets the objective sum of weight[s,p] * x[s,p].

        Args:
            weights: Weight per (student_id, project_id) pair.
            sense: pyo.maximize or pyo.minimize.
        """
        m = self.model
        if hasattr(m, "obj"):
            m.del_component("obj")
        m.obj = pyo.Objective(
            expr=sum(w * m.x[pair] for pair, w in weights.items()),
            sense=sense,
        )

    def solve(self) -> str:
        """Runs HiGHS and returns one of the STATUS_* constants.

        Solutions are only loaded when one exists; with the default
        load_solutions=True, Pyomo raises an error on infeasible models.
        """
        solver = pyo.SolverFactory(SOLVER_NAME)
        if not solver.available():
            raise RuntimeError(f"Solver '{SOLVER_NAME}' is not available.")
        solver.config.time_limit = config.Config.SOLVER_TIME_LIMIT_SECONDS
        results = solver.solve(self.model, load_solutions=False)
        condition = results.solver.termination_condition

        if condition == TerminationCondition.optimal:
            self.model.solutions.load_from(results)
            return STATUS_OPTIMAL
        if condition in (
            TerminationCondition.infeasible,
            TerminationCondition.infeasibleOrUnbounded,
        ):
            return STATUS_INFEASIBLE
        if condition == TerminationCondition.maxTimeLimit:
            try:
                self.model.solutions.load_from(results)
                return STATUS_FEASIBLE
            except (RuntimeError, ValueError):
                return STATUS_NOT_SOLVED
        return STATUS_NOT_SOLVED

    def chosen_pairs(self) -> list[Pair]:
        """Returns the (student_id, project_id) pairs with x[s,p] = 1."""
        chosen = []
        for pair in self.pairs:
            value = self.model.x[pair].value
            if value is not None and value > 0.5:
                chosen.append(pair)
        return chosen

    def objective_value(self) -> float:
        """Returns the value of the current objective."""
        return float(pyo.value(self.model.obj))

    def size(self) -> dict[str, int]:
        """Returns the number of variables and constraints."""
        return {
            "variables_count": self.model.nvariables(),
            "constraints_count": self.model.nconstraints(),
        }


class AssignmentStrategy(abc.ABC):
    """Abstract Base Class for student-to-project allocation strategies.

    A strategy defines what makes an assignment good by weighting each
    student-project pair (calculate_cost_or_utility). The hard constraints
    are the same for every strategy and live in AssignmentModel.
    """

    def __init__(self, name: str, description: str) -> None:
        """Initializes the strategy with descriptive metadata.

        Args:
            name: Machine-readable identifier for the strategy.
            description: Human-readable explanation of the objective function.
        """
        self.name = name
        self.description = description

    @abc.abstractmethod
    def calculate_cost_or_utility(
        self,
        student: models.Student,
        project: models.Project,
        max_preferences: int,
    ) -> float:
        """Computes the objective weight of assigning student to project.

        The model maximizes the sum of these weights, so higher is better.

        Args:
            student: The candidate student.
            project: The target semester project.
            max_preferences: Number of projects |P| (unranked = |P| + 1).

        Returns:
            Numerical weight reflecting desirability in optimization objective.
        """

    def solve(
        self,
        students: list[models.Student],
        projects: list[models.Project],
        fixed_assignments: list[models.FixedAssignment] | None = None,
        excluded_solutions: list[list[Pair]] | None = None,
    ) -> SolveResult:
        """Solves the assignment problem under this strategy's objective.

        Args:
            students: List of students in the cohort.
            projects: List of available semester projects.
            fixed_assignments: Pre-assigned student-project pairs that must hold.
            excluded_solutions: Previous solution allocations to cut out.

        Returns:
            A tuple of (solver_status, assignments, objective_value,
            diagnostics). Assignments are empty unless the status is
            OPTIMAL or FEASIBLE.
        """
        started = time.perf_counter()
        model = AssignmentModel(
            f"tech5_{self.name.lower()}",
            students,
            projects,
            fixed_assignments,
            excluded_solutions,
        )
        model.set_objective(self._weights(students, projects))
        status = model.solve()
        return self._result(
            model, status, students, projects, fixed_assignments, started
        )

    def _weights(
        self, students: list[models.Student], projects: list[models.Project]
    ) -> dict[Pair, float]:
        """Returns the objective weight of every student-project pair."""
        return {
            (s.id, p.id): self.calculate_cost_or_utility(s, p, len(projects))
            for s in students
            for p in projects
        }

    def _result(
        self,
        model: AssignmentModel,
        status: str,
        students: list[models.Student],
        projects: list[models.Project],
        fixed_assignments: list[models.FixedAssignment] | None,
        started: float,
        extra: dict[str, Any] | None = None,
    ) -> SolveResult:
        """Converts the solved model into the solve() return tuple."""
        diagnostics: dict[str, Any] = {
            "strategy": self.name,
            "solver": SOLVER_NAME,
            "solver_status": status,
            "solve_time_seconds": round(time.perf_counter() - started, 4),
            **model.size(),
            **(extra or {}),
        }
        if status not in SOLVED_STATUSES:
            return status, [], None, diagnostics

        assignments = build_assignments(
            model.chosen_pairs(), students, projects, fixed_assignments or []
        )
        return status, assignments, round(model.objective_value(), 6), diagnostics


class PreferenceOrientedSolver(AssignmentStrategy):
    """Minimizes total preference rank: as many high choices as possible.

    Objective: max sum of (-r[s,p] + TIE_BREAK_WEIGHT * sigma[s,p]) * x[s,p],
    i.e. minimize the sum of ranks; skill fit only breaks ties.
    """

    def __init__(self) -> None:
        super().__init__(
            "PREFERENCE",
            "Minimizes the sum of assigned preference ranks; skill fit breaks "
            "ties.",
        )

    def calculate_cost_or_utility(
        self,
        student: models.Student,
        project: models.Project,
        max_preferences: int,
    ) -> float:
        rank = preference_cost(student, project, max_preferences)
        return -rank + TIE_BREAK_WEIGHT * skill_fit(student, project)


class SkillFitOrientedSolver(AssignmentStrategy):
    """Maximizes total skill fit: the strongest possible project teams.

    Objective: max sum of (sigma[s,p] + TIE_BREAK_WEIGHT * u[s,p]) * x[s,p];
    preferences only break ties.
    """

    def __init__(self) -> None:
        super().__init__(
            "SKILL_FIT",
            "Maximizes the sum of student-project skill fit; preferences break "
            "ties.",
        )

    def calculate_cost_or_utility(
        self,
        student: models.Student,
        project: models.Project,
        max_preferences: int,
    ) -> float:
        return skill_fit(student, project) + TIE_BREAK_WEIGHT * preference_score(
            student, project, max_preferences
        )


class BalancedTradeoffSolver(AssignmentStrategy):
    """Protects the worst-off student, then balances preferences and skills.

    Stage 1 (minimax) finds the smallest possible worst rank r* among students
    who are not fixed (fixed students are excluded because their rank is not a
    choice). Stage 2 forbids ranks worse than r* and maximizes
    sum of (0.5 * u[s,p] + 0.5 * sigma[s,p]) * x[s,p].
    """

    PREFERENCE_WEIGHT = 0.5
    SKILL_WEIGHT = 0.5

    def __init__(self) -> None:
        super().__init__(
            "BALANCED",
            "Minimizes the worst assigned rank, then maximizes an equal blend "
            "of preference score and skill fit.",
        )

    def calculate_cost_or_utility(
        self,
        student: models.Student,
        project: models.Project,
        max_preferences: int,
    ) -> float:
        return self.PREFERENCE_WEIGHT * preference_score(
            student, project, max_preferences
        ) + self.SKILL_WEIGHT * skill_fit(student, project)

    def solve(
        self,
        students: list[models.Student],
        projects: list[models.Project],
        fixed_assignments: list[models.FixedAssignment] | None = None,
        excluded_solutions: list[list[Pair]] | None = None,
    ) -> SolveResult:
        started = time.perf_counter()
        fixed = fixed_assignments or []
        free = [s for s in students if s.id not in {fa.student_id for fa in fixed}]
        num_projects = len(projects)

        # Stage 1: minimize the worst rank z among non-fixed students.
        stage1 = AssignmentModel(
            "tech5_balanced_stage1", students, projects, fixed, excluded_solutions
        )
        m = stage1.model
        m.worst = pyo.Var(domain=pyo.NonNegativeReals)
        m.worst_def = pyo.ConstraintList()
        for s in free:
            for p in projects:
                rank = preference_cost(s, p, num_projects)
                m.worst_def.add(m.worst >= rank * m.x[s.id, p.id])
        m.obj = pyo.Objective(expr=m.worst, sense=pyo.minimize)
        status1 = stage1.solve()
        if status1 not in SOLVED_STATUSES:
            return self._result(stage1, status1, students, projects, fixed, started)
        best_worst_rank = round(pyo.value(m.worst))

        # Stage 2: keep r*, maximize the balanced score.
        stage2 = AssignmentModel(
            "tech5_balanced_stage2", students, projects, fixed, excluded_solutions
        )
        stage2.set_objective(self._weights(students, projects))
        for s in free:
            for p in projects:
                if preference_cost(s, p, num_projects) > best_worst_rank:
                    stage2.model.x[s.id, p.id].fix(0)
        status2 = stage2.solve()
        return self._result(
            stage2,
            status2,
            students,
            projects,
            fixed,
            started,
            extra={"worst_rank_bound": best_worst_rank},
        )


def build_assignments(
    pairs: list[Pair],
    students: list[models.Student],
    projects: list[models.Project],
    fixed_assignments: list[models.FixedAssignment],
) -> list[models.Assignment]:
    """Turns solver pairs into Assignment objects with rank and skill fit.

    Args:
        pairs: Chosen (student_id, project_id) pairs.
        students: Students in the cohort.
        projects: Projects in the catalog.
        fixed_assignments: Locks, used to flag fixed assignments.

    Returns:
        One Assignment per student, in cohort order.
    """
    project_by_id = {p.id: p for p in projects}
    fixed_pairs = {(fa.student_id, fa.project_id) for fa in fixed_assignments}
    project_of = dict(pairs)

    assignments: list[models.Assignment] = []
    for s in students:
        if s.id not in project_of:
            continue
        p = project_by_id[project_of[s.id]]
        fit, unmet = metrics.calculate_assignment_skill_fit(s, p)
        assignments.append(
            models.Assignment(
                student_id=s.id,
                project_id=p.id,
                is_fixed=(s.id, p.id) in fixed_pairs,
                preference_rank=s.get_preference_rank(p.id),
                skill_fit_score=fit,
                unmet_requirements=unmet,
            )
        )
    return assignments


@dataclasses.dataclass
class Alternative:
    """One distinct feasible solution produced by a strategy.

    Attributes:
        strategy_name: Strategy that produced it (e.g. "PREFERENCE").
        solver_status: OPTIMAL or FEASIBLE.
        assignments: One Assignment per student.
        objective_value: Objective value under that strategy.
        diagnostics: Solver diagnostics (time, model size, ...).
    """

    strategy_name: str
    solver_status: str
    assignments: list[models.Assignment]
    objective_value: float | None
    diagnostics: dict[str, Any]

    def pairs(self) -> list[Pair]:
        """Returns the (student_id, project_id) pairs of this solution."""
        return [(a.student_id, a.project_id) for a in self.assignments]


@dataclasses.dataclass
class AlternativeSet:
    """Result of generating up to K distinct alternatives.

    Attributes:
        status: OPTIMAL/FEASIBLE if at least one solution exists, else the
            status of the first solve (INFEASIBLE or NOT_SOLVED).
        alternatives: Distinct solutions, in the order they were found.
        diagnostics: Includes requested and distinct_alternatives counts and
            the reason generation stopped.
    """

    status: str
    alternatives: list[Alternative]
    diagnostics: dict[str, Any]


class AssignmentEngine(contract.AssignmentEngineContract):
    """Main Assignment Engine orchestrating validation, execution, and persistence.

    Implements the internal component contract (AssignmentEngineContract)
    connecting Component B to Component A.
    """

    def __init__(
        self, run_repo: repositories.RunRepository | None = None
    ) -> None:
        """Initializes the AssignmentEngine with persistence repositories.

        Args:
            run_repo: Optional repository for persisting runs and solutions.
        """
        self.run_repo = (
            run_repo if run_repo is not None else repositories.RunRepository()
        )
        self.strategies: dict[str, AssignmentStrategy] = {
            strategy.name: strategy
            for strategy in (
                PreferenceOrientedSolver(),
                SkillFitOrientedSolver(),
                BalancedTradeoffSolver(),
            )
        }

    def generate_alternatives(
        self,
        students: list[models.Student],
        projects: list[models.Project],
        fixed_assignments: list[models.FixedAssignment] | None = None,
        strategy_key: str = "PREFERENCE",
        max_alternatives: int = 3,
    ) -> AlternativeSet:
        """Generates up to K distinct alternatives, one strategy per solve.

        Alternative 1 uses the selected strategy; the next ones use the
        remaining strategies in turn. Each solve excludes all earlier
        solutions with no-good cuts. If a solve is infeasible, no further
        distinct assignment exists, so generation stops and reports the
        number found instead of inventing duplicates.

        Args:
            students: Students in the cohort.
            projects: Projects in the catalog.
            fixed_assignments: Locks that must hold in every alternative.
            strategy_key: Strategy for the first alternative.
            max_alternatives: K, the maximum number of alternatives.

        Returns:
            An AlternativeSet with the distinct solutions found.

        Raises:
            KeyError: If strategy_key is not a registered strategy.
        """
        primary = strategy_key.upper()
        if primary not in self.strategies:
            raise KeyError(
                f"Unknown strategy '{strategy_key}'. "
                f"Available: {', '.join(self.strategies)}."
            )
        order = [primary] + [k for k in self.strategies if k != primary]
        wanted = max(1, int(max_alternatives))

        alternatives: list[Alternative] = []
        excluded: list[list[Pair]] = []
        first_status = STATUS_NOT_SOLVED
        stop_reason = "Requested number of alternatives reached."

        for i in range(wanted):
            strategy = self.strategies[order[i % len(order)]]
            status, assignments, objective, diag = strategy.solve(
                students, projects, fixed_assignments, excluded
            )
            if i == 0:
                first_status = status
            if status not in SOLVED_STATUSES:
                if i == 0:
                    stop_reason = (
                        "No assignment satisfies all hard constraints."
                        if status == STATUS_INFEASIBLE
                        else "The solver stopped without a solution."
                    )
                else:
                    stop_reason = (
                        f"Only {len(alternatives)} distinct feasible "
                        "solution(s) exist."
                        if status == STATUS_INFEASIBLE
                        else "The solver stopped before finding another "
                        "solution."
                    )
                break
            alternative = Alternative(
                strategy.name, status, assignments, objective, diag
            )
            alternatives.append(alternative)
            excluded.append(alternative.pairs())

        return AlternativeSet(
            status=first_status,
            alternatives=alternatives,
            diagnostics={
                "requested_alternatives": wanted,
                "distinct_alternatives": len(alternatives),
                "strategy_order": order[: max(1, len(alternatives))],
                "stop_reason": stop_reason,
            },
        )

    def validate_inputs(
        self, request: contract.RunRequest
    ) -> contract.ValidationOutcome:
        """Validates cohort and constraints before attempting optimization.

        Args:
            request: The run request containing students, projects, etc.

        Returns:
            ValidationOutcome with validity, feasibility, errors, and warnings.
        """
        val_result = validation.validate_cohort(
            students=request.students,
            projects=request.projects,
            skills=request.skills,
            fixed_assignments=request.fixed_assignments,
        )
        return contract.ValidationOutcome(
            is_valid=val_result.is_valid,
            is_feasible=val_result.is_feasible,
            errors=list(val_result.errors),
            warnings=list(val_result.warnings),
            infeasibility_reasons=list(val_result.infeasibility_reasons),
        )

    def execute_run(
        self, request: contract.RunRequest
    ) -> models.AssignmentRun:
        """Validates, solves, evaluates and saves one assignment run.

        Every outcome is saved, also when the input is rejected or the
        problem is infeasible, so that Component A can always load the run:
        - INFEASIBLE: validation or the solver proved that no assignment
          satisfies the hard constraints (reasons in diagnostics["reasons"]).
        - INVALID_INPUT: the data has structural errors (diagnostics["errors"]).
        - FAILED: the solver or the database failed (diagnostics["message"]).
        - COMPLETED: one or more distinct alternatives with indicators.

        Args:
            request: The run request containing students, projects, etc.

        Returns:
            The saved AssignmentRun populated with alternative solutions.
        """
        run = models.AssignmentRun(
            id=str(uuid.uuid4()),
            configuration={
                "primary_strategy": request.strategy_key,
                "max_alternatives": request.max_alternatives,
                "engine": f"AssignmentEngine (ILP, Pyomo/{SOLVER_NAME})",
                "notes": request.notes,
            },
        )
        outcome = self.validate_inputs(request)
        base = {"warnings": outcome.warnings, "notes": request.notes}

        # Bad data is reported before infeasibility: an empty cohort or a
        # negative capacity is an input error, not a property of the problem.
        # Errors that only restate an infeasibility reason (e.g. more fixed
        # students than seats) leave the data valid but the problem unsolvable.
        structural_errors = [
            e for e in outcome.errors if e not in outcome.infeasibility_reasons
        ]
        if structural_errors:
            return self._finish(
                run,
                contract.RunStatus.INVALID_INPUT,
                {
                    **base,
                    "message": "The input data was rejected by validation.",
                    "errors": structural_errors,
                    "reasons": outcome.infeasibility_reasons,
                },
            )
        if not outcome.is_feasible:
            return self._finish(
                run,
                contract.RunStatus.INFEASIBLE,
                {
                    **base,
                    "message": "No feasible assignment exists for these inputs.",
                    "reasons": outcome.infeasibility_reasons,
                    "errors": outcome.errors,
                },
            )

        try:
            self._synchronize_entities(request)
            result = self.generate_alternatives(
                request.students,
                request.projects,
                request.fixed_assignments,
                request.strategy_key,
                request.max_alternatives,
            )
        except KeyError as exc:
            return self._finish(
                run,
                contract.RunStatus.INVALID_INPUT,
                {**base, "message": str(exc.args[0]), "errors": [str(exc.args[0])]},
            )
        except (sqlite3.Error, RuntimeError, ValueError) as exc:
            logger.exception("Assignment run %s failed", run.id)
            return self._finish(
                run,
                contract.RunStatus.FAILED,
                {**base, "message": f"Run failed: {type(exc).__name__}: {exc}"},
            )

        diagnostics = {**base, **result.diagnostics, "solver_status": result.status}
        if result.status == STATUS_INFEASIBLE:
            return self._finish(
                run,
                contract.RunStatus.INFEASIBLE,
                {
                    **diagnostics,
                    "message": "The solver proved that no assignment satisfies "
                    "all hard constraints.",
                    "reasons": [
                        "The combination of capacities, fixed assignments and "
                        "mandatory skills cannot be met; no single check "
                        "detects it before solving."
                    ],
                },
            )
        if not result.alternatives:
            return self._finish(
                run,
                contract.RunStatus.FAILED,
                {**diagnostics, "message": result.diagnostics["stop_reason"]},
            )

        run.solutions = self._build_solutions(run.id, request, result)
        diagnostics["solution_count"] = len(run.solutions)
        diagnostics["message"] = (
            f"{len(run.solutions)} distinct alternative(s) generated. "
            f"{result.diagnostics['stop_reason']}"
        )
        return self._finish(run, contract.RunStatus.COMPLETED, diagnostics)

    def _build_solutions(
        self,
        run_id: str,
        request: contract.RunRequest,
        result: AlternativeSet,
    ) -> list[models.Solution]:
        """Creates Solution objects with indicators and dominance warnings."""
        solutions: list[models.Solution] = []
        for number, alt in enumerate(result.alternatives, start=1):
            indicators = metrics.compute_solution_indicators(
                request.students,
                request.projects,
                alt.assignments,
                request.fixed_assignments,
            )
            solutions.append(
                models.Solution(
                    id=str(uuid.uuid4()),
                    run_id=run_id,
                    alternative_number=number,
                    strategy_name=alt.strategy_name,
                    objective_value=alt.objective_value,
                    assignments=alt.assignments,
                    indicators=indicators,
                    diagnostics=alt.diagnostics,
                )
            )

        for solution in solutions:
            dominating = metrics.find_dominating_solutions(solution, solutions)
            if dominating:
                numbers = [d.alternative_number for d in dominating]
                solution.diagnostics["dominated_by"] = numbers
                label = "alternative" if len(numbers) == 1 else "alternatives"
                solution.warnings.append(
                    f"Dominated by {label} "
                    f"{' and '.join(str(n) for n in numbers)}: each is at "
                    "least as good on mean rank and mean skill fit and better "
                    "on one of them, so this is not a real trade-off."
                )
        return solutions

    def _synchronize_entities(self, request: contract.RunRequest) -> None:
        """Saves cohort entities so that assignments satisfy foreign keys."""
        skill_repo = repositories.SkillRepository(self.run_repo.db)
        for skill in request.skills or []:
            skill_repo.save(skill)
        project_repo = repositories.ProjectRepository(self.run_repo.db)
        for project in request.projects:
            project_repo.save(project)
        student_repo = repositories.StudentRepository(self.run_repo.db)
        for student in request.students:
            student_repo.save(student)
        fixed_repo = repositories.FixedAssignmentRepository(self.run_repo.db)
        for fixed in request.fixed_assignments or []:
            fixed_repo.save(fixed)

    def _finish(
        self,
        run: models.AssignmentRun,
        status: contract.RunStatus,
        diagnostics: dict[str, Any],
    ) -> models.AssignmentRun:
        """Sets status and diagnostics, saves the run and returns it."""
        run.status = status.value
        run.diagnostics = diagnostics
        try:
            self.run_repo.save_run(run)
        except sqlite3.Error as exc:
            logger.exception("Could not save run %s", run.id)
            run.status = contract.RunStatus.FAILED.value
            run.diagnostics = {
                **diagnostics,
                "message": f"The run could not be saved: {exc}",
            }
        return run

    def run_assignment(
        self,
        students: list[models.Student],
        projects: list[models.Project],
        skills: list[models.Skill] | None = None,
        fixed_assignments: list[models.FixedAssignment] | None = None,
        strategy_name: str = "PREFERENCE",
        max_alternatives: int = 3,
        notes: str = "",
    ) -> models.AssignmentRun:
        """Convenience method delegating to execute_run via RunRequest.

        Args:
            students: Student profiles.
            projects: Project specifications.
            skills: Optional skills catalog.
            fixed_assignments: Optional pre-allocated locks.
            strategy_name: Strategy key name.
            max_alternatives: Max solutions to generate.
            notes: Administrative description or notes for the run.

        Returns:
            The created AssignmentRun.
        """
        req = contract.RunRequest(
            students=students,
            projects=projects,
            skills=skills or [],
            fixed_assignments=fixed_assignments or [],
            strategy_key=strategy_name,
            max_alternatives=max_alternatives,
            notes=notes,
        )
        return self.execute_run(req)

    def list_runs(self, limit: int = 20) -> list[models.AssignmentRun]:
        """Retrieves historical assignment runs from repository.

        Args:
            limit: Maximum number of runs to retrieve.

        Returns:
            List of historical AssignmentRun domain objects.
        """
        return self.run_repo.list_runs(limit=limit)

    def get_run(self, run_id: str) -> models.AssignmentRun | None:
        """Retrieves a specific run by identifier.

        Args:
            run_id: Unique run identifier.

        Returns:
            The AssignmentRun object, or None if not found.
        """
        return self.run_repo.get_run(run_id)

    def get_solution(self, solution_id: str) -> models.Solution | None:
        """Retrieves a specific solution and its student allocations.

        Args:
            solution_id: Unique solution identifier.

        Returns:
            The Solution object with assignments, or None if not found.
        """
        return self.run_repo.get_solution(solution_id)

    def compare_solutions(
        self, solution_a_id: str, solution_b_id: str
    ) -> models.RunComparison:
        """Compares two alternative solutions side-by-side using metrics module.

        Args:
            solution_a_id: Identifier of the baseline solution.
            solution_b_id: Identifier of the comparison solution.

        Returns:
            RunComparison containing delta metrics and changed assignments.

        Raises:
            KeyError: If either solution cannot be found.
        """
        sol_a = self.run_repo.get_solution(solution_a_id)
        sol_b = self.run_repo.get_solution(solution_b_id)
        if sol_a is None or sol_b is None:
            raise KeyError(
                f"Solutions not found: {solution_a_id}, {solution_b_id}"
            )
        comp_dict = metrics.compare_two_solutions(sol_a, sol_b)
        return models.RunComparison.from_dict(comp_dict)
