# Team A Plan: Visualization and Recommendation

**Team A members:** Wilhelm Leikanger, Alexander Bangsund, Henrik Wassiluk
**Component:** `app/visualization/` (Flask/Jinja web interface)
**Repository:** https://github.com/WilhelmLeikanger/TECH5-collaborative-Project
(shared with Team B)

## 1. Scope

Team A builds the part of the application through which the decision-maker
works: managing projects and skills, starting assignment runs, viewing and
comparing alternative solutions, and presenting transparent recommendations.

We develop against the agreed contract (`app/contract/contract.py`) and the
`MockAssignmentEngine` until Team B's real engine is ready, so no Team A task
is blocked by Team B.

## 2. Week 1: Understand, Design, and Divide

**Milestone (from the brief):** Team A can create projects and skills and
view mock solution comparisons.

| Owner | Task | Files | Done when |
|---|---|---|---|
| Wilhelm | **Skills management**: `create_skill`, `delete_skill`; skill catalog table and create form. | `routes.py` (`create_skill`, `delete_skill`), `templates/skills.html` | `test_skills_crud_workflow` passes; a skill can be added and deleted in the browser |
| Alexander | **Projects management**: `create_project`, `update_project`, `delete_project`, including parsing skill requirements (minimum level 1–5, mandatory or not) and capacity validation (min ≤ max). Project table and create/edit form. | `routes.py` (`create_project`, `update_project`, `delete_project`), `templates/projects.html`, `templates/project_form.html` | `test_projects_crud_workflow` passes; a project with requirements can be created, edited and deleted |
| Henrik | **Solution comparison (mock)**: `compare_solutions` controller (reads `sol1` / `sol2`, calls `engine.compare_solutions`) and side-by-side view: changed students, Δ skill fit, Δ mean rank, Δ unranked, trade-off summary. | `routes.py` (`compare_solutions`), `templates/compare.html` | `test_compare_solutions_view` passes; two mock solutions can be compared in the browser |

**Shared Week 1 tasks**

| Owner | Task |
|---|---|
| Wilhelm | Add all Team A and Team B members as collaborators; agree with Team B that the contract does not change without both teams agreeing. |
| All | Clone the repo, set up `.venv`, run `pytest` and `python run.py`; read the existing students CRUD (`create_student`, `update_student`) as the pattern for our own code. |
| Wilhelm (all contribute) | **Week 1 progress report**: what was done, who did what, test status, problems, plan for Week 2. |

## 3. Week 2: Make It Real

**Milestone:** the application solves real cohorts, keeps multiple
alternatives, and shows live comparisons without mock output.

| Owner | Task |
|---|---|
| Henrik | Recommendations: make the recommendation view explain *what improves, what gets worse, who is affected, and which alternative we recommend and why*, separating calculated evidence from advice. |
| Alexander | Fairness and results display: rank distribution, students outside their preferences, worst individual outcome, and the extra fairness indicator agreed with Team B. |
| Wilhelm | Integration: switch from the mock to Team B's `AssignmentEngine`, run end-to-end tests together with Team B, fix contract mismatches. |
| Wilhelm (all contribute) | **Week 2 progress report.** |

## 4. Week 3: Make It Robust and Explainable

**Milestone:** full end-to-end functionality verified: data entry, runs,
distinct alternatives, comparison, recommendations, fixed assignments,
reruns, and infeasibility handling.

| Owner | Task |
|---|---|
| Alexander | Edge cases in the UI: invalid capacities, unknown IDs, missing skills, conflicting fixed assignments — clear error messages, no crashes. Edge-case tests. |
| Henrik | Reproducible demonstration (`docs/reproducible_demonstration.md`): trade-off between alternatives, recommendation, fixed assignment + rerun, infeasible case. |
| Wilhelm | Component/class diagrams and design rationale for Team A; database model and representative SQL queries (together with Team B). |
| All | Team reflection (`docs/team_reflection_and_rubric.md` — rewrite it to describe our real work): responsibilities, communication, Git, AI use, refactoring, integration, lessons. |

## 5. Deliverables overview (from the brief)

| # | Deliverable | Responsible |
|---|---|---|
| 1 | Progress reports after Weeks 1 and 2 | Wilhelm, input from all |
| 2 | Integrated working application with persistence | Both teams |
| 3 | Repository evidence of progressive development and review | Everyone (branches + PRs) |
| 4 | Component and class diagrams with design rationale | Wilhelm (Team A part) |
| 5 | Database model and representative SQL queries | Wilhelm + Team B |
| 6 | Analytical note | Team B (Team A reviews the recommendation criteria) |
| 7 | Evidence of component, database, integration and edge-case testing | Alexander + Team B |
| 8 | Reproducible demonstration | Henrik |
| 9 | Team reflection | All |

## 6. Way of working

- **Git:** one branch per task (`feature/skills`, `feature/projects`,
  `feature/compare`, …); merge to `main` via pull request after one review
  by another member. Nobody pushes directly to `main`.
- **Before every PR:** `pytest` passes, and code follows the Google Python
  Style Guide used in the starter kit (type hints, docstrings, ≤ 80 chars).
- **Contract:** `app/contract/contract.py` is only changed after agreement
  with Team B.
- **Integration early:** try Team B's engine as soon as any part of it works;
  don't wait until Week 3.
- **Check-ins:** short status in the group chat twice a week; a short meeting
  at the end of each week before the progress report.
