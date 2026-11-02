# Deliverable 4: Internal Component Contract & Integration Specification

## 1. Architectural Agreement & Scope

In accordance with Section 8 of the TECH5 Project Specification, the interaction between **Component A** (*Visualization and Recommendation Component*) and **Component B** (*Assignment Engine*) is governed by an explicit in-process Python contract.

```
+-------------------------------------------------------------------------+
|                                                                         |
|            COMPONENT A: VISUALIZATION & RECOMMENDATION                  |
|    - Dashboard, Cohort CRUD, Scenarios, Solution Views, Recommendations |
|                                                                         |
+-------------------------------------------------------------------------+
                                    |
                                    | Python Method Invocations
                                    | (contract.AssignmentEngineContract)
                                    v
+-------------------------------------------------------------------------+
|                                                                         |
|                     INTERNAL COMPONENT CONTRACT                         |
|         app/contract/contract.py: AssignmentEngineContract             |
|   - validate_inputs(request: RunRequest) -> ValidationOutcome          |
|   - execute_run(request: RunRequest) -> AssignmentRun                   |
|   - list_runs(limit: int) -> list[AssignmentRun]                        |
|   - get_run(run_id: str) -> AssignmentRun | None                        |
|   - get_solution(solution_id: str) -> Solution | None                   |
|   - compare_solutions(sol_a_id: str, sol_b_id: str) -> RunComparison   |
|                                                                         |
+-------------------------------------------------------------------------+
                                    ^
                                    | Implements Abstract Contract
                                    |
+-------------------------------------------------------------------------+
|                                                                         |
|                     COMPONENT B: ASSIGNMENT ENGINE                      |
|           app/engine/solvers.py: AssignmentEngine                       |
|   - Strategy Pattern: PREFERENCE, SKILL_FIT, BALANCED                   |
|   - Pyomo/HiGHS Integer Linear Programming & No-Good Cuts               |
|   - Analytical Indicators (Gini, Mean Rank, Skill Fit, Utilization)     |
|                                                                         |
+-------------------------------------------------------------------------+
                                    |
                                    v
+-------------------------------------------------------------------------+
|                   SHARED RELATIONAL PERSISTENCE                         |
|                      SQLite 3NF Schema (app/persistence)                |
+-------------------------------------------------------------------------+
```

This design guarantees complete decoupling:
1. **Zero Network Overhead**: Eliminates serialization latency, HTTP status code translation, connection timeouts, and port conflicts.
2. **Type Safety & Compiler Diagnostics**: All DTOs and return types are fully type-annotated (`typing` and Python 3.10+ union types).
3. **Independent Testability**: Component B can be tested completely headlessly without web servers, and Component A can be tested with mock engine implementations.

---

## 2. Shared Data Definitions & Identifiers

All entities share standardized identifiers across both components:

| Entity | Identifier Format | Definition |
|---|---|---|
| **Student** | `str` (e.g. `"S01"`) | Candidate participant with defined skill proficiencies and ranked preferences. |
| **Project** | `str` (e.g. `"P1"`) | Target semester offering with capacity limits and required/desired skills. |
| **Skill** | `str` (e.g. `"python"`) | Competency keyword indexed in catalog. |
| **Run** | `str` (UUIDv4) | Single administrative optimization event. |
| **Solution** | `str` (UUIDv4) | One feasible, distinct allocation alternative produced within a run (dominated alternatives are kept but flagged). |
| **Fixed Lock** | `(student_id, project_id)` | Administrative lock that must be satisfied in all generated solutions. |

---

## 3. Data Transfer Objects (DTOs)

### 3.1 `RunRequest`
The structured input submitted by Component A to initiate validation or execution:

```python
@dataclasses.dataclass(frozen=True)
class RunRequest:
    students: list[models.Student]
    projects: list[models.Project]
    skills: list[models.Skill] | None = None
    fixed_assignments: list[models.FixedAssignment] | None = None
    strategy_key: str = "PREFERENCE"
    max_alternatives: int = 3
    notes: str = ""
```

### 3.2 `ValidationOutcome`
Returned by `validate_inputs` before any solver execution:

```python
@dataclasses.dataclass(frozen=True)
class ValidationOutcome:
    is_valid: bool = True
    is_feasible: bool = True
    errors: list[str] = dataclasses.field(default_factory=list)
    warnings: list[str] = dataclasses.field(default_factory=list)
    infeasibility_reasons: list[str] = dataclasses.field(default_factory=list)
```

### 3.3 `RunStatus`
Execution lifecycle enum:

```python
class RunStatus(enum.Enum):
    COMPLETED = "COMPLETED"
    INFEASIBLE = "INFEASIBLE"
    INVALID_INPUT = "INVALID_INPUT"
    WARNING = "WARNING"
    FAILED = "FAILED"
```

`WARNING` is part of the enum but is **not returned by the engine**. Non-fatal issues are reported in `run.diagnostics["warnings"]` of a `COMPLETED` run instead, so that a run that was solved is always `COMPLETED`. Section 5 defines when each status is used.

### 3.4 `RunComparison`
The side-by-side comparison structure returned by `compare_solutions`:

```python
@dataclasses.dataclass(frozen=True)
class RunComparison:
    solution_a_id: str
    solution_a_alt: int
    solution_b_id: str
    solution_b_alt: int
    delta_metrics: dict[str, Any]
    changed_assignments: list[dict[str, Any]]
    changed_students_count: int
    total_students: int
    is_distinct: bool
    rank_improved_count: int
    rank_worsened_count: int
    delta_mean_skill_fit: float
    delta_mean_rank: float
    delta_unranked_count: int
    trade_off_summary: list[str]
```

---

## 4. Contract Operations & Signatures

```python
class AssignmentEngineContract(abc.ABC):
    """Abstract interface defining Component B contract."""

    @abc.abstractmethod
    def validate_inputs(self, request: RunRequest) -> ValidationOutcome:
        """Validates cohort and constraints before attempting optimization."""

    @abc.abstractmethod
    def execute_run(self, request: RunRequest) -> models.AssignmentRun:
        """Executes the assignment algorithm and generates solutions."""

    @abc.abstractmethod
    def list_runs(self, limit: int = 20) -> list[models.AssignmentRun]:
        """Retrieves historical assignment runs."""

    @abc.abstractmethod
    def get_run(self, run_id: str) -> models.AssignmentRun | None:
        """Retrieves a specific run by identifier."""

    @abc.abstractmethod
    def get_solution(self, solution_id: str) -> models.Solution | None:
        """Retrieves a specific solution and its student allocations."""

    @abc.abstractmethod
    def compare_solutions(
        self, solution_a_id: str, solution_b_id: str
    ) -> models.RunComparison:
        """Compares two alternative solutions side-by-side."""
```

---

## 5. Engine Behaviour (Component B Guarantees)

This section states what `AssignmentEngine` (`app/engine/solvers.py`) promises for each contract operation. The exact fields and keys that Component A reads are listed in [`engine_integration_checklist.md`](engine_integration_checklist.md); the engine satisfies that checklist.

### 5.1 Operations

| Operation | Behaviour | Errors |
|---|---|---|
| `validate_inputs(request)` | Runs `validation.validate_cohort` only; never calls the solver and writes nothing to the database. | None; problems are returned in the `ValidationOutcome`. |
| `execute_run(request)` | Validates, solves, computes indicators and **saves the run in every outcome**, then returns it with one of the four statuses below. | Never raises for bad data, infeasibility or solver failure; these become run statuses. |
| `list_runs(limit)` | Most recent runs first, at most `limit`. | None. |
| `get_run(run_id)` / `get_solution(solution_id)` | Loads from the database, including assignments and indicators. | Returns `None` if the id is unknown. |
| `compare_solutions(a_id, b_id)` | Compares two saved solutions with `metrics.compare_two_solutions`; `a` is the baseline. | Raises `KeyError` if either solution is not found. |

### 5.2 Steps of `execute_run`

1. **Validate.** Data errors (empty cohort, unknown ids, tied ranks, skill levels outside 1 to 5, invalid capacities, ...) give `INVALID_INPUT`. Data errors are reported before infeasibility.
2. **Check simple impossibilities.** Total capacity below the cohort size, minimum capacities above it, more fixed students than seats, or a mandatory skill no student has give `INFEASIBLE` without solving.
3. **Synchronise entities.** Students, projects and fixed assignments in the request are written to their tables so that saved assignments keep foreign-key integrity.
4. **Generate alternatives.** Alternative 1 uses `request.strategy_key`; the next ones use the remaining strategies (`PREFERENCE`, `SKILL_FIT`, `BALANCED`). No-good cuts exclude every earlier solution, so alternatives are always distinct. Generation stops at `max_alternatives` or when no further distinct solution exists.
5. **Evaluate.** Each solution gets feasibility, efficiency, fairness and capacity indicators. A solution that is no better on mean rank and mean skill fit than another, and worse on one, gets `diagnostics["dominated_by"]` and a warning.
6. **Save** the run and return it.

### 5.3 Run statuses

| Status | When | `solutions` | Diagnostics always present |
|---|---|---|---|
| `COMPLETED` | At least one feasible alternative was found | 1 to `max_alternatives` | `message`, `solver_status`, `requested_alternatives`, `distinct_alternatives`, `strategy_order`, `stop_reason`, `solution_count`, `warnings` |
| `INFEASIBLE` | Validation or the solver proved that no assignment satisfies the hard constraints | Empty | `message`, `reasons` (plus `solver_status` when the solver proved it) |
| `INVALID_INPUT` | The input data has errors, or the strategy key is unknown | Empty | `message`, `errors` |
| `FAILED` | The solver stopped without a solution (e.g. the 15-second time limit) or the database failed | Empty | `message` |

All statuses also carry `warnings` (list) and `notes` (str) in `run.diagnostics`, and `run.configuration` holds `primary_strategy`, `max_alternatives`, `engine` and `notes`.

### 5.4 Solution output

| Field | Content |
|---|---|
| `strategy_name` | `PREFERENCE`, `SKILL_FIT` or `BALANCED` |
| `alternative_number` | 1, 2, 3 in the order generated |
| `assignments` | One per student, with `preference_rank` (`None` if unranked) and `skill_fit_score` |
| `objective_value` | Objective under the solution's own strategy (not comparable across strategies) |
| `indicators` | `feasibility`, `efficiency`, `fairness`, `capacity`; key list in the integration checklist, formulas in the analytical note |
| `diagnostics` | `strategy`, `solver`, `solver_status`, `solve_time_seconds`, `variables_count`, `constraints_count`; `dominated_by` (list of alternative numbers) when dominated |
| `warnings` | e.g. "Dominated by alternatives 1 and 2" |

`DiagnosticInfo` in `contract.py` is not used by the engine; diagnostics are plain dictionaries so that they can be stored as JSON and shown directly in the interface.

---

## 6. Persistence Responsibilities

1. **Relational Database Ownership**:
   - `RunRepository` is encapsulated within Component B for storing `AssignmentRun`, `Solution`, and `Assignment` records.
   - To preserve relational foreign-key integrity in SQLite, `AssignmentEngine.execute_run()` ensures that any in-memory `Student`, `Project`, and `FixedAssignment` records supplied in `RunRequest` are synchronized to their respective tables before committing solution allocations.
2. **Immutable Runs and Solutions**:
   - Assignment runs and their generated alternative solutions are append-only.
   - When inputs or fixed constraints are revised, the system does not mutate previous runs; it executes a new run with a unique run identifier, preserving complete historical auditability.

---

## 7. Integration Usage Example

```python
from app.contract import contract
from app.engine import solvers

# 1. Instantiate engine via interface
engine: contract.AssignmentEngineContract = solvers.AssignmentEngine()

# 2. Formulate RunRequest
request = contract.RunRequest(
    students=cohort_students,
    projects=catalog_projects,
    fixed_assignments=[
        models.FixedAssignment(student_id="S02", project_id="P2", reason="Fellowship")
    ],
    strategy_key="PREFERENCE",
    max_alternatives=3,
)

# 3. Pre-run validation
outcome = engine.validate_inputs(request)
if not outcome.is_valid or not outcome.is_feasible:
    print(f"Validation failed: {outcome.errors} | Infeasibility: {outcome.infeasibility_reasons}")
else:
    # 4. Execute optimization run
    run = engine.execute_run(request)
    print(f"Run {run.id} status: {run.status}, Generated {len(run.solutions)} solutions.")

    # 5. Compare two alternatives
    if len(run.solutions) >= 2:
        comparison = engine.compare_solutions(run.solutions[0].id, run.solutions[1].id)
        print(f"Shifted students: {comparison.changed_students_count}")
        print(f"Trade-offs: {comparison.trade_off_summary}")
```
