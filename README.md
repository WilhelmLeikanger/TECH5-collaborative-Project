# TECH5: Student-to-Project Assignment Decision Support System
### Student Starter Carcass & Collaborative Project Kit
**NHH Norwegian School of Economics — Department of Business and Management Science**  
*Course: TECH5 Programming & Application Design*

---

## 1. Project Overview & Starter Kit Structure

Welcome to the **TECH5 Collaborative Project: Student-to-Project Assignment Decision Support System (DSS)**.

This repository provides the starter carcass for a three-week collaborative software engineering project. The application assists academic coordinators and decision-makers in assigning students to semester research and capstone projects under multiple competing criteria:
- **Student ranked preferences** (satisfaction / rank regret)
- **Student technical proficiencies & project skill requirements** (competence matching)
- **Project capacity constraints** (minimum and maximum headcount quotas)
- **Administrative fixed assignments** (pre-allocated student-to-project locks)

In accordance with the course specification (`assignment/projectTech5.tex`), the application avoids microservices, network communication, web APIs, or distributed deployment. It is structured as **two modular, independently testable components** running within a single in-process Python application:

```text
TECH5Project/
├── app/
│   ├── contract/             # Shared in-process Python component contract
│   │   ├── contract.py       # AssignmentEngineContract ABC & strongly typed DTOs
│   │   └── mocks.py          # Complete MockAssignmentEngine for immediate testing
│   ├── domain/               # Shared domain entities & validation rules
│   │   ├── models.py         # Student, Project, Skill, Preference, Run, Solution
│   │   └── validation.py     # Cohort validation & feasibility pre-checks
│   ├── engine/               # Component B: Assignment Engine (Team B Tasks)
│   │   ├── solvers.py        # ILP engine (Pyomo + HiGHS), three strategies, alternatives
│   │   ├── metrics.py        # Analytical indicators & fairness metrics
│   │   └── recommendations.py# Transparent recommendations per policy priority
│   ├── persistence/          # Shared relational persistence layer
│   │   ├── database.py       # Thread-safe SQLite database manager (WAL mode)
│   │   ├── repositories.py   # Student, Project, Skill, Run repositories
│   │   └── schema.sql        # 3NF relational database schema
│   └── visualization/        # Component A: Visualization & UI (Team A)
│       ├── routes.py         # Flask views, controllers, & engine dispatch
│       ├── templates/        # Jinja2 responsive HTML5 templates
│       └── static/           # CSS stylesheets & client assets
├── assignment/               # Course assignment brief (projectTech5.tex)
├── docs/                     # Deliverable documentation & student templates
│   ├── analytical_note_template.md   # Deliverable 6 template for student report
│   ├── analytical_formulation.md     # Analytical requirements & modeling guide
│   ├── architecture.md               # System architecture & design patterns
│   ├── database_design.md            # ERD, 3NF schema, & analytical SQL queries
│   ├── internal_component_contract.md# Component contract documentation
│   ├── reproducible_demonstration.md # Demonstration walkthrough script
│   ├── team_reflection_and_rubric.md # Collaboration reflection & rubric guide
│   └── testing_and_quality.md        # Test matrix & edge case documentation
├── tests/                    # Automated test suite (98 pytest tests)
├── requirements.txt          # Python dependencies
└── run.py                    # Application launch entry point
```

---

## 2. Team Division & Responsibilities

The project is designed for two collaborating student development teams:

### Team A: Visualization and Recommendation Component (`app/visualization/`)
- **Workflow & UI**: Complete web interface for inspecting cohorts, editing student/project/skill data, creating fixed assignments, and triggering assignment runs.
- **Pluggable Mock Engine**: The starter kit includes a fully operational `MockAssignmentEngine` (`app/contract/mocks.py`). Team A can develop, test, and demonstrate all UI workflows, visual comparisons, and recommendations **immediately**, without waiting for Team B to complete the solver!
- **Comparison & Recommendations**: Side-by-side comparison of alternative solutions, student re-allocation diffs, and transparent recommendations across multiple administrative priorities.

### Team B: Assignment Engine (`app/engine/`)
- **Optimization Formulation (`app/engine/solvers.py`)**:
  - Implement an approved mathematical assignment methodology (implemented: Integer Linear Programming in Pyomo, solved with HiGHS).
  - Implement concrete strategies inheriting from `AssignmentStrategy` (`PreferenceOrientedSolver`, `SkillFitOrientedSolver`, `BalancedTradeoffSolver`).
  - Implement generation of distinct Pareto alternatives via no-good integer cuts.
  - Implement `AssignmentEngine.run_assignment(...)` and `AssignmentEngine.execute_run(...)`.
- **Analytical Indicators (`app/engine/metrics.py`)**:
  - Implement `calculate_skill_fit(...)` (normalized skill-fit score $\sigma_{s,p}$).
  - Implement `calculate_gini(...)` (Gini coefficient of preference ranks $G$).
  - Implement `calculate_solution_indicators(...)` (feasibility, efficiency, fairness, capacity utilization).
  - Implement `compare_solutions(...)` (comparative indicator deltas and changed student allocations).
- **Analytical Note**: Complete the formal mathematical report using [`docs/analytical_note_template.md`](docs/analytical_note_template.md).

---

## 3. Quickstart & Installation

### 3.1 Prerequisites
- Python 3.10+ (tested up to Python 3.14)
- Git

### 3.2 Setup Virtual Environment
```powershell
# 1. Clone repository & enter workspace
git clone https://github.com/WilhelmLeikanger/TECH5-collaborative-Project.git
cd TECH5-collaborative-Project

# 2. Create and activate virtual environment
python -m venv .venv
.\.venv\Scripts\activate      # Windows PowerShell
# source .venv/bin/activate   # macOS / Linux

# 3. Install dependencies
pip install -r requirements.txt

# 4. Launch the application
python run.py
```

Navigate to **`http://127.0.0.1:5000`** in your browser.

> [!IMPORTANT]
> Step 3 is required: the engine imports `pyomo` and uses the HiGHS solver
> (`highspy`). In an environment without them, every test that touches the
> engine fails at collection with `ModuleNotFoundError: No module named 'pyomo'`.
> No solver licence or separate install is needed; `pip` installs everything.

For a step-by-step walkthrough of the four demo scenarios, see
[`docs/reproducible_demonstration.md`](docs/reproducible_demonstration.md).

---

## 4. Automated Testing & Verification

```powershell
# Run the complete test suite
.\.venv\Scripts\pytest -v
```

Expected result on `main`: **98 passed, 0 failed, 0 expected failures**.
All tests in the starter kit that were marked as expected failures now pass,
and none are skipped.

| Test file | Tests | What it checks |
|---|---|---|
| `test_solvers.py`, `test_assignment_strategies.py` | 22 | Hard constraints, each strategy's objective, distinct alternatives |
| `test_engine_run.py`, `test_contract_and_engine.py` | 23 | `execute_run`, run statuses, contract methods |
| `test_metrics.py` | 4 | Skill fit, Gini, indicators, comparison |
| `test_edge_cases.py`, `test_validation.py` | 21 | Infeasible input, ties, invalid skill levels, conflicting locks |
| `test_engine_output_contract.py` | 5 | Every field and indicator key the UI reads is present |
| `test_component_integration.py` | 3 | Full workflow through the UI with the real engine, data survives an app restart |
| `test_milestone_end_to_end.py` | 1 | Every milestone item in order, starting from data typed into the forms |
| `test_visualization_component.py` | 12 | Pages, compare view, fairness section, recommendations, scenario reload |
| `test_models.py`, `test_persistence.py` | 7 | Domain models and SQLite repositories |

---


