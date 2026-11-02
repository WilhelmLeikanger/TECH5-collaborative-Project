# Deliverable 3: System Architecture Specification & Design Rationale

## 1. Executive Architectural Overview

The **TECH5 Student-to-Project Assignment Decision Support System (DSS)** is an integrated application engineered for the Norwegian School of Economics (NHH). It solves the complex combinatorial allocation problem of assigning a cohort of students to semester research and consulting projects based on multidimensional criteria: **student ranked preferences**, **student skill proficiencies**, **project skill requirements**, **project capacity bounds**, and **administrative pre-allocation locks**.

In accordance with the updated project specification (Sections 5, 7, 8, and 15), the system is architected as **two decoupled, modular application components** running within a single integrated Python application:

1. **Component A (Visualization & Recommendation Component)**:
   - Manages user workflow, cohort data inspection, CRUD interfaces, benchmark scenario seeding, role switching (Admin, Coordinator, Student Viewer), multi-solution visual side-by-side trade-off comparisons, and evidence-backed recommendations.
2. **Component B (Assignment Engine)**:
   - Validates the input, builds a binary integer linear program in **Pyomo** and solves it with **HiGHS**, using the **Strategy Pattern** (`PreferenceOrientedSolver`, `SkillFitOrientedSolver`, `BalancedTradeoffSolver`) to choose the objective. It generates up to K distinct alternatives with no-good cuts, computes feasibility, efficiency, fairness and capacity indicators, flags dominated alternatives, and saves every run with a status and diagnostics.

The two components are mediated strictly by an **internal Python component contract** (`app/contract/contract.py`), backed by a shared relational SQLite 3NF persistence layer (`app/persistence`).

---

## 2. Component Diagram & Internal Interface

```mermaid
flowchart TD
    subgraph Component_A["Component A: Visualization & Recommendation (app/visualization)"]
        UI[Jinja2 HTML5 / CSS3 Responsive UI]
        WebRoutes[Flask Web Blueprint: visualization_web]
        RoleAuth[Role-Based Access Control: Admin / Coord / Student]
        RecPresenter[Recommendation & Trade-Off Presentation]
    end

    subgraph InternalContract["Internal Component Contract (app/contract/contract.py)"]
        ContractABC["AssignmentEngineContract (ABC)"]
        DTOs["RunRequest | ValidationOutcome | DiagnosticInfo | RunComparison"]
    end

    subgraph Component_B["Component B: Assignment Engine (app/engine)"]
        SolverEngine["AssignmentEngine (implements Contract)"]
        StrategyPattern["Strategy Pattern: Preference, Skill-Fit, Balanced"]
        ModelBuilder["AssignmentModel: Pyomo ILP (HiGHS) & No-Good Cuts"]
        MetricsEngine["metrics: Indicators, Gini, Dominance"]
        ValidationEngine["Domain Feasibility Validator"]
    end

    subgraph Persistence["Shared Relational Persistence (app/persistence)"]
        DBMgr["DatabaseManager (SQLite WAL Mode, Foreign Keys)"]
        Repos["Repository Pattern: Students, Projects, Skills, Runs"]
        Schema["3NF Relational Schema & Performance Indexes"]
    end

    UI --> WebRoutes
    WebRoutes --> RoleAuth
    WebRoutes --> RecPresenter
    WebRoutes -->|Invokes Python Methods| ContractABC
    ContractABC -.->|Implemented By| SolverEngine

    SolverEngine --> StrategyPattern
    StrategyPattern --> ModelBuilder
    SolverEngine --> MetricsEngine
    SolverEngine --> ValidationEngine

    WebRoutes --> Repos
    SolverEngine --> Repos
    Repos --> DBMgr
    DBMgr --> Schema
```

---

## 3. Design Rationale: Two-Component Modular Monolith vs. Microservices

### 3.1 Motivation for In-Process Two-Component Architecture
1. **Curricular Alignment & Feasibility**:
   - The primary pedagogical objective is object-oriented software design, relational database integration, and decision support. Microservices introduce substantial operational complexity (distributed networking, ports, HTTP status translation, retries, serialization boilerplate) that distracts from the core analytical problem.
2. **High Modularity Without Operational Overhead**:
   - Component A and Component B maintain strict separation of concerns. Component B has zero knowledge of Flask request objects, HTML templates, or cookies. It can be instantiated, validated, and tested headlessly in pure Python scripts or test harnesses.
3. **Type Safety & Compiler Diagnostics**:
   - In-process contract calls pass typed dataclasses (`RunRequest`, `ValidationOutcome`, `RunComparison`) directly. Type checkers (`mypy`, `pylint`) and IDEs provide immediate syntax and type validation, eliminating runtime JSON schema mismatch errors.
4. **Performance & Determinism**:
   - Direct method invocations eliminate JSON serialization/deserialization CPU cycles and network socket latency, allowing instant interactive comparison and optimization execution.

---

## 4. Object-Oriented Architecture & Design Patterns

### 4.1 Assignment Engine Classes (Component B)

The engine separates **what is allowed** from **what is best**. `AssignmentModel` builds one Pyomo model containing every hard constraint; each `AssignmentStrategy` only sets the objective on that model. `AssignmentEngine` implements the contract and runs the pipeline: validate, solve, compute indicators, save.

```mermaid
classDiagram
    direction TB

    class AssignmentEngineContract {
        <<abstract>>
        +execute_run(request) AssignmentRun
        +validate_inputs(request) ValidationOutcome
        +list_runs(limit) list
        +get_run(run_id) AssignmentRun
        +get_solution(solution_id) Solution
        +compare_solutions(a, b) RunComparison
    }

    class AssignmentEngine {
        -run_repo: RunRepository
        -strategies: dict~str, AssignmentStrategy~
        +execute_run(request) AssignmentRun
        +generate_alternatives(students, projects, fixed, strategy, k) AlternativeSet
        +compare_solutions(a, b) RunComparison
        -_build_solutions(...) list~Solution~
        -_finish(run, status, diagnostics) AssignmentRun
    }

    class AssignmentStrategy {
        <<abstract>>
        +name: str
        +description: str
        +calculate_cost_or_utility(student, project)* float
        +solve(students, projects, fixed, excluded) SolveResult
    }
    class PreferenceOrientedSolver {
        max sum(-rank + 0.001 * skill fit)
    }
    class SkillFitOrientedSolver {
        max sum(skill fit + 0.001 * preference score)
    }
    class BalancedTradeoffSolver {
        stage 1: min worst rank
        stage 2: max 0.5 * preference + 0.5 * skill fit
        +solve(...) SolveResult
    }

    class AssignmentModel {
        +x: Var[s, p] binary
        +y: Var[p] binary
        -_add_one_project_per_student()
        -_add_capacity_bounds()
        -_add_fixed_assignments(fixed)
        -_add_mandatory_skills()
        -_add_no_good_cuts(excluded)
        +set_objective(weights)
        +solve() str
        +chosen_pairs() list
    }

    class Alternative {
        +strategy_name: str
        +solver_status: str
        +assignments: list~Assignment~
        +objective_value: float
        +pairs() list
    }
    class AlternativeSet {
        +status: str
        +alternatives: list~Alternative~
        +diagnostics: dict
    }

    class metrics {
        <<module>>
        +compute_solution_indicators(...) dict
        +calculate_gini(ranks) float
        +find_dominating_solutions(...) dict
        +compare_two_solutions(a, b) dict
    }
    class validation {
        <<module>>
        +validate_cohort(...) ValidationResult
    }
    class RunRepository {
        +save_run(run)
        +get_run(run_id) AssignmentRun
    }

    AssignmentEngineContract <|-- AssignmentEngine
    AssignmentStrategy <|-- PreferenceOrientedSolver
    AssignmentStrategy <|-- SkillFitOrientedSolver
    AssignmentStrategy <|-- BalancedTradeoffSolver
    AssignmentEngine o-- "3" AssignmentStrategy : strategies
    AssignmentStrategy ..> AssignmentModel : builds and solves
    AssignmentEngine ..> AlternativeSet : generate_alternatives()
    AlternativeSet *-- "0..K" Alternative
    AssignmentEngine ..> validation : before solving
    AssignmentEngine ..> metrics : after solving
    AssignmentEngine --> RunRepository : saves every run
```

### 4.2 Assignment Engine Design Rationale

| Decision | Reason | Alternative rejected |
|---|---|---|
| Constraints in one `AssignmentModel`, objectives in strategies | Every strategy obeys exactly the same hard rules; a new strategy only defines an objective and cannot break feasibility | Each strategy building its own model, which duplicates constraints and lets them drift apart |
| Strategy Pattern with a strategy registry in `AssignmentEngine` | The user picks a strategy by key; adding one means adding a subclass, not editing the engine | `if/else` on the strategy name inside the engine |
| `BalancedTradeoffSolver` overrides `solve()` | It needs two solves (minimise the worst rank, then blend preference and skill fit with the worst rank fixed), which a single objective cannot express | One weighted sum with an arbitrary fairness weight |
| Pyomo with HiGHS (`appsi_highs`) | Same modelling tool as TECH4; HiGHS installs with pip on all platforms and proves infeasibility | PuLP, whose current version no longer bundles a solver |
| Validation before the solver | Data errors give named messages (`INVALID_INPUT`) instead of a solver failure, and simple impossibilities are reported without solving | Letting the solver discover everything |
| Four run statuses: `COMPLETED`, `INFEASIBLE`, `INVALID_INPUT`, `FAILED` | Separates "impossible" from "wrong input" from "broken", so the interface can explain each | One generic error status |
| No-good cuts plus a dominance check | Cuts guarantee distinct alternatives; the check flags an alternative that is not a real trade-off (`dominated_by`) instead of hiding it | Assuming distinct means Pareto-optimal |
| `metrics` as a module of pure functions | Indicators are calculated the same way for every solution and can be tested with hand-calculated examples | Indicator logic inside the strategies |
| Every run saved, including rejected and infeasible ones | Component A loads runs from the database, and the history shows why a run failed | Saving only completed runs |

The mathematical formulation behind these classes is in `docs/analytical_note/analytical_note.pdf`.

### 4.3 Abstract Contract Pattern (Component Boundary)
`AssignmentEngineContract` defines the public interface required by Component A. Concrete `AssignmentEngine` implements all contract methods, ensuring that any mock or alternative solver can be substituted without touching Component A.

### 4.4 Repository Pattern (Data Access Layer)
Persistence operations are encapsulated into domain repositories:
- `StudentRepository`: Manages `students`, `student_skills`, and `preferences`.
- `ProjectRepository`: Manages `projects` and `project_requirements`.
- `SkillRepository`: Manages defined competencies.
- `FixedAssignmentRepository`: Manages administrative pre-allocation locks.
- `RunRepository`: Manages `assignment_runs`, `solutions`, and `assignments`.

---

## 5. Relational Database Architecture

```mermaid
erDiagram
    students ||--o{ student_skills : "possesses"
    skills ||--o{ student_skills : "classified under"
    students ||--o{ preferences : "submits"
    projects ||--o{ preferences : "ranked in"
    projects ||--o{ project_requirements : "demands"
    skills ||--o{ project_requirements : "specified by"
    students ||--o{ fixed_assignments : "locked to"
    projects ||--o{ fixed_assignments : "hosts"
    assignment_runs ||--o{ solutions : "produces"
    solutions ||--o{ assignments : "allocates"
    students ||--o{ assignments : "assigned"
    projects ||--o{ assignments : "contains"

    students {
        TEXT id PK
        TEXT display_name
    }
    projects {
        TEXT id PK
        TEXT title
        TEXT description
        INTEGER min_capacity
        INTEGER max_capacity
    }
    skills {
        TEXT id PK
        TEXT name
        TEXT description
    }
    student_skills {
        TEXT student_id PK,FK
        TEXT skill_id PK,FK
        INTEGER level
    }
    project_requirements {
        TEXT project_id PK,FK
        TEXT skill_id PK,FK
        INTEGER minimum_level
        INTEGER is_mandatory
    }
    preferences {
        TEXT student_id PK,FK
        TEXT project_id PK,FK
        INTEGER rank
    }
    fixed_assignments {
        TEXT student_id PK,FK
        TEXT project_id FK
        TEXT status
        TEXT reason
    }
    assignment_runs {
        TEXT id PK
        TEXT created_at
        TEXT status
        TEXT configuration
        TEXT diagnostics
    }
    solutions {
        TEXT id PK
        TEXT run_id FK
        INTEGER alternative_number
        TEXT strategy_name
        REAL objective_value
        TEXT indicators
        TEXT warnings
        TEXT diagnostics
    }
    assignments {
        INTEGER id PK
        TEXT solution_id FK
        TEXT student_id FK
        TEXT project_id FK
        INTEGER is_fixed
        INTEGER preference_rank
        REAL skill_fit_score
        TEXT unmet_requirements
    }
```

---

## 6. Role-Based Access Control (RBAC)

The application models three explicit business roles in `app/visualization/auth.py`:
1. **Course Administrator (`ADMIN`)**: Full authorization. Can edit students, projects, skills, and fixed assignments, trigger optimization runs, and switch active roles.
2. **Program Coordinator (`COORDINATOR`)**: Operational authorization. Can manage fixed assignment locks, trigger solver runs, and inspect detailed recommendations and comparisons. Read-only on baseline student data.
3. **Student Viewer (`STUDENT`)**: Transparency view. Can browse anonymized dashboard metrics, project descriptions, and high-level allocation summaries. Mutating actions are disabled.
