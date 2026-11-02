# Testing, Quality Assurance and Edge Cases

This document is the testing evidence for the TECH5 Student-to-Project
Assignment application (brief, Section 11, deliverable 7). Every number below
comes from the current `main` branch and can be reproduced with the commands
in Section 7.

**Result: 98 automated tests, 98 passed, 0 failed, 0 expected failures.**
All 15 tests that the starter kit marked as expected failures (12 for Team B,
3 for Team A) now pass.

---

## 1. Test strategy

The application is tested in four layers, from single functions up to the
full web workflow:

| Layer | What is tested | Test files |
|---|---|---|
| 1. Unit | Domain models, input validation, solver strategies, analytical indicators | `test_models.py`, `test_validation.py`, `test_assignment_strategies.py`, `test_solvers.py`, `test_metrics.py` |
| 2. Persistence | SQLite schema creation and repository round trips | `test_persistence.py` |
| 3. Engine and contract | `execute_run` outcomes and statuses, the component contract, the exact output Team A's pages read | `test_engine_run.py`, `test_contract_and_engine.py`, `test_engine_output_contract.py`, `test_edge_cases.py` |
| 4. Integration and UI | Flask routes and pages, full workflows through the web app with the real engine | `test_visualization_component.py`, `test_component_integration.py`, `test_milestone_end_to_end.py` |

Principles we followed:

- **Each test uses its own temporary database** (`tmp_path`), so tests never
  depend on each other or on local data.
- **Engine tests call the solver directly**, without the web app, so a
  failure points to the engine rather than the UI.
- **Integration tests go through the web app**, the way a user would, and
  load results from the database the way the pages do.
- **Expected values are calculated by hand** where possible (for example the
  Gini coefficient and the rank of unranked assignments), not copied from the
  program's own output.
- **Starter tests that only checked that the engine was not yet implemented**
  (four tests expecting `NotImplementedError`) were rewritten to test the real
  engine. Each one says so in its docstring.

---

## 2. Test inventory

| Test file | Tests | Covers | Main author |
|---|:---:|---|---|
| `test_assignment_strategies.py` | 18 | Every strategy respects all hard constraints; PREFERENCE minimises total rank; BALANCED lowers the worst rank; SKILL_FIT prefers qualified students; fixed assignments in all alternatives; mandatory skills (team level, and an empty project needs no expert); unranked projects; distinct alternatives; exactly one or two feasible solutions; infeasibility proven by the solver; unknown strategy | Team B |
| `test_engine_run.py` | 18 | Every run status (COMPLETED, INFEASIBLE, INVALID_INPUT); every run is saved; dominated alternatives flagged; Gini against hand-calculated values; unranked rank = \|P\| + 1; feasibility re-check names each violation; comparison of unranked students; tied or repeated preferences and skill levels outside 1–5 rejected; fewer than three preferences warns but solves | Team B |
| `test_edge_cases.py` | 15 | The 15 edge cases from the starter specification (Section 4 below) | Starter, enabled by Team B |
| `test_validation.py` | 6 | Empty cohort, duplicate identifiers, capacity errors, contradictory fixed assignments, fixed assignments over capacity, total capacity deficit | Starter |
| `test_metrics.py` | 4 | Gini, skill fit, solution indicators, solution comparison | Starter, enabled by Team B |
| `test_solvers.py` | 4 | Strategy base class is abstract; three strategies registered; `execute_run` solves the standard scenario; alternatives are distinct | Starter, updated by Team B |
| `test_contract_and_engine.py` | 5 | The engine and the mock both implement the contract; validation, execution, retrieval and comparison through the contract | Starter, updated by Team B |
| `test_engine_output_contract.py` | 5 | The exact fields, indicator keys and status strings Team A's pages read (`docs/engine_integration_checklist.md`) | Team A |
| `test_component_integration.py` | 3 | Full lifecycle through the web app (scenario, run, solution, recommendations, comparison, fixed assignment and rerun); infeasible cohort; data surviving an app restart | Starter, extended by Team A |
| `test_milestone_end_to_end.py` | 1 | The Week 3 milestone driven only through the UI forms: data entry, run, alternatives, comparison, recommendations, fixed assignment, infeasibility | Team A |
| `test_visualization_component.py` | 12 | Dashboard, roles, student, skill and project forms, scenario loading and reloading, comparison page, fairness section, recommendations | Starter, extended by Team A |
| `test_persistence.py` | 3 | Schema creation and repository round trips for skills, students, projects, runs and solutions | Starter |
| `test_models.py` | 4 | Serialisation and helper methods of the domain models | Starter |
| **Total** | **98** | | |

---

## 3. Do the tests catch errors?

A test suite is only useful if it fails when the code is wrong. We checked this
by removing each hard constraint from the solver in turn
(`app/engine/solvers.py`, `AssignmentModel`), running the full suite, and
restoring the code afterwards:

| Constraint removed | Tests that then fail |
|---|:---:|
| One project per student | 20 |
| Minimum and maximum capacity | 12 |
| Fixed assignments | 5 |
| Mandatory skills | 4 |
| No-good cuts (distinct alternatives) | 8 |

Every constraint is protected by at least four tests. With the code restored,
all 98 tests pass again.

We also solved the standard and the fixed-assignment scenarios five times
each: every repeat produced exactly the same alternatives, so results are
reproducible for the demonstration.

---

## 4. Edge cases

### 4.1 Edge cases listed in the brief (Section 9)

For each situation the brief requires a defined response: reject the input,
warn the user, produce an explicitly allowed partial result, or report
infeasibility. None ends in an unexplained crash.

| Situation (brief, Section 9) | Response | Behaviour | Verified by |
|---|---|---|---|
| Incomplete preferences (fewer than 3 ranked) | Warn | The run proceeds; projects the student did not rank count as outside their preferences | `test_fewer_than_three_preferences_warns_but_solves`, `test_edge_case_1_incomplete_preferences` |
| Tied preferences (one student uses a rank twice, or ranks a project twice) | Reject | Status `INVALID_INPUT` with the student named | `test_tied_ranks_are_invalid_input_not_a_crash`, `test_same_project_ranked_twice_is_invalid_input` |
| Missing skills | Accept | The student can be assigned; skill fit is 0 for requirements they lack | `test_edge_case_3_zero_skills_student` |
| Unknown identifiers | Reject | Named validation error; the solver is not run | `test_edge_case_5_unknown_project_in_preferences`, `test_duplicate_identifiers`, `test_invalid_input_is_rejected_and_saved` |
| Unknown skill in a profile or requirement | Warn | The run proceeds; the warning names the skill | `test_edge_case_6_unknown_skill_in_entity` |
| Invalid capacities (max ≤ 0, min < 0, min > max) | Reject | Named validation error | `test_capacity_errors`, `test_edge_case_9_capacity_inversion` |
| Skill or requirement levels outside 1–5 | Reject | Status `INVALID_INPUT` | `test_student_skill_level_outside_1_to_5_is_invalid[0]` and `[6]`, `test_requirement_level_outside_1_to_5_is_invalid` |
| Conflicting fixed assignments (one student fixed twice) | Reject | Named validation error | `test_contradictory_fixed_assignments`, `test_edge_case_10_conflicting_fixed_locks_same_student` |
| More fixed students than a project's capacity | Infeasible | Status `INFEASIBLE` with the reason | `test_fixed_over_capacity_stays_infeasible_with_reason`, `test_edge_case_11_fixed_locks_exceeding_project_capacity` |
| Unavailable mandatory skill | Infeasible | Proven before solving when no student has the skill | `test_edge_case_12_unavailable_mandatory_skill` |
| Too much demand (many students want the same project) | Accept | Capacities hold; the solver decides who gets the seat | `test_edge_case_2_tied_preferences_across_students` |
| Too little capacity (more students than seats), or minimums above the cohort size | Infeasible | Proven before solving, with the reason | `test_edge_case_7_aggregate_capacity_deficit`, `test_edge_case_8_aggregate_min_capacity_excess`, `test_infeasible_cohort_integration` |
| Student receives an unranked project | Accept | Allowed; counted as rank \|P\| + 1 and as outside preferences | `test_unranked_project_gets_no_rank`, `test_unranked_assignment_counts_as_num_projects_plus_one`, `test_compare_counts_unranked_as_worse` |
| Infeasible problem found only by the solver | Infeasible | Status `INFEASIBLE`, no solutions, run saved with diagnostics | `test_infeasibility_found_only_by_solver`, `test_infeasible_found_by_solver_is_saved_with_reasons`, `test_edge_case_13_no_feasible_solution` |
| Only one feasible solution | Partial | The one solution is returned; the stop reason says no other distinct solution exists | `test_single_feasible_solution_is_reported_honestly`, `test_edge_case_14_single_feasible_solution`, `test_recommendations_single_solution` |
| Duplicate alternatives | Prevented | Impossible by construction (no-good cuts) | `test_alternatives_are_pairwise_distinct`, `test_edge_case_15_duplicate_avoidance_no_good_cuts`, `test_distinct_alternatives_generation` |
| Equal aggregate scores | Accept | A small tie-break term makes results deterministic; dominated alternatives are flagged | `test_dominated_alternative_is_flagged`; repeat check in Section 3 |
| Invalid database records | Reject | Validation rejects data that the schema would refuse (for example tied ranks), so the database write no longer fails | `test_tied_ranks_are_invalid_input_not_a_crash` |
| Database failures | Fail | `execute_run` catches database errors and saves the run as `FAILED` with a message | Not covered by an automated test (Section 6) |

### 4.2 The 15 starter edge cases

`tests/test_edge_cases.py` contains the 15 edge cases from the starter
specification. All 15 pass; cases 1–4 and 13–15 were expected failures in the
starter kit and pass now that the engine is implemented.

| # | Edge case | Response |
|:---:|---|---|
| 1 | Student ranks only some projects | Warn, then solve |
| 2 | All students want the same project | Capacity respected |
| 3 | Student without skills | Assigned with skill fit 0 |
| 4 | Project without requirements | Skill fit 1 (neutral) |
| 5 | Preference for an unknown project | Reject |
| 6 | Unknown skill in a profile or requirement | Warn |
| 7 | More students than seats | Infeasible |
| 8 | Minimum capacities above the cohort size | Infeasible |
| 9 | Minimum capacity above maximum | Reject |
| 10 | One student fixed to two projects | Reject |
| 11 | More fixed students than a project's capacity | Infeasible |
| 12 | Mandatory skill no student has | Infeasible |
| 13 | Infeasible model | Status `INFEASIBLE`, no solutions |
| 14 | Exactly one feasible solution | One solution, reported honestly |
| 15 | Exactly two feasible solutions, three requested | Two distinct solutions |

---

## 5. Contract and integration evidence

- **Contract compliance:** `test_contract_and_engine.py` checks that both the
  real `AssignmentEngine` and the `MockAssignmentEngine` implement
  `AssignmentEngineContract`, and that validation, execution, retrieval and
  comparison work through the contract.
- **What the pages read:** `test_engine_output_contract.py` (Team A) runs the
  real engine and checks every field, indicator key and status string listed
  in `docs/engine_integration_checklist.md`, loading the run from the
  database as the pages do.
- **Full workflows:** `test_component_integration.py` and
  `test_milestone_end_to_end.py` drive the web app: entering data or loading
  a scenario, triggering a run, viewing solutions, comparison and
  recommendations, adding a fixed assignment and rerunning (the lock holds in
  every new alternative), an infeasible cohort, and an app restart on the
  same database.
- **No mock in the results:** since `execute_run` is implemented, the web app
  no longer falls back to the mock; every integration test above runs on the
  real Pyomo/HiGHS engine.

---

## 6. Known gaps

- **Database failures** are handled in `execute_run` (the run is saved as
  `FAILED` with a message), but no automated test simulates a failing
  database.
- **Solver time limit:** the 15-second limit and the resulting status are not
  tested, because the demo cohorts solve in well under a second.
- **Missing capacities:** the schema requires a maximum capacity
  (`NOT NULL`), and the model defaults to 1, but no test enters a project
  without one.
- **Scale:** solve times are only measured on the demo cohorts (10 students,
  4 projects). Larger cohorts have not been benchmarked.
- **Determinism** is checked by repeating runs (Section 3), not by an
  automated test.

---

## 7. How to reproduce

```powershell
.\.venv\Scripts\activate
pip install -r requirements.txt
pytest -v
```

Expected: `98 passed, 0 failed`.

Full log from the current `main`:

```text
============================= test session starts ==============================
platform linux -- Python 3.13.16, pytest-9.1.1, pluggy-1.7.0 
rootdir: TECH5-collaborative-Project
collecting ... collected 98 items

tests/test_assignment_strategies.py::test_engine_registers_three_strategies PASSED
tests/test_assignment_strategies.py::test_every_strategy_respects_hard_constraints[PREFERENCE] PASSED
tests/test_assignment_strategies.py::test_every_strategy_respects_hard_constraints[SKILL_FIT] PASSED
tests/test_assignment_strategies.py::test_every_strategy_respects_hard_constraints[BALANCED] PASSED
tests/test_assignment_strategies.py::test_preference_minimizes_total_rank PASSED
tests/test_assignment_strategies.py::test_balanced_protects_worst_off_student PASSED
tests/test_assignment_strategies.py::test_skill_fit_prefers_qualified_students PASSED
tests/test_assignment_strategies.py::test_fixed_assignment_holds_in_all_alternatives PASSED
tests/test_assignment_strategies.py::test_mandatory_skill_needs_one_qualified_member PASSED
tests/test_assignment_strategies.py::test_empty_project_needs_no_mandatory_skill PASSED
tests/test_assignment_strategies.py::test_unranked_project_gets_no_rank PASSED
tests/test_assignment_strategies.py::test_alternatives_are_pairwise_distinct PASSED
tests/test_assignment_strategies.py::test_first_alternative_uses_selected_strategy PASSED
tests/test_assignment_strategies.py::test_single_feasible_solution_is_reported_honestly PASSED
tests/test_assignment_strategies.py::test_exactly_two_solutions_exist PASSED
tests/test_assignment_strategies.py::test_infeasible_problem_returns_no_solutions PASSED
tests/test_assignment_strategies.py::test_infeasibility_found_only_by_solver PASSED
tests/test_assignment_strategies.py::test_unknown_strategy_is_rejected PASSED
tests/test_component_integration.py::test_full_lifecycle_integration PASSED
tests/test_component_integration.py::test_infeasible_cohort_integration PASSED
tests/test_component_integration.py::test_data_survives_app_restart PASSED
tests/test_contract_and_engine.py::test_engine_implements_contract PASSED
tests/test_contract_and_engine.py::test_contract_validate_inputs PASSED
tests/test_contract_and_engine.py::test_contract_execute_and_retrieval PASSED
tests/test_contract_and_engine.py::test_contract_compare_solutions PASSED
tests/test_contract_and_engine.py::test_mock_engine_provided PASSED
tests/test_edge_cases.py::test_edge_case_1_incomplete_preferences PASSED
tests/test_edge_cases.py::test_edge_case_2_tied_preferences_across_students PASSED
tests/test_edge_cases.py::test_edge_case_3_zero_skills_student PASSED
tests/test_edge_cases.py::test_edge_case_4_zero_requirements_project PASSED
tests/test_edge_cases.py::test_edge_case_5_unknown_project_in_preferences PASSED
tests/test_edge_cases.py::test_edge_case_6_unknown_skill_in_entity PASSED
tests/test_edge_cases.py::test_edge_case_7_aggregate_capacity_deficit PASSED
tests/test_edge_cases.py::test_edge_case_8_aggregate_min_capacity_excess PASSED
tests/test_edge_cases.py::test_edge_case_9_capacity_inversion PASSED
tests/test_edge_cases.py::test_edge_case_10_conflicting_fixed_locks_same_student PASSED
tests/test_edge_cases.py::test_edge_case_11_fixed_locks_exceeding_project_capacity PASSED
tests/test_edge_cases.py::test_edge_case_12_unavailable_mandatory_skill PASSED
tests/test_edge_cases.py::test_edge_case_13_no_feasible_solution PASSED
tests/test_edge_cases.py::test_edge_case_14_single_feasible_solution PASSED
tests/test_edge_cases.py::test_edge_case_15_duplicate_avoidance_no_good_cuts PASSED
tests/test_engine_output_contract.py::test_run_is_saved_with_plain_string_status PASSED
tests/test_engine_output_contract.py::test_solutions_and_assignments PASSED
tests/test_engine_output_contract.py::test_indicator_keys PASSED
tests/test_engine_output_contract.py::test_compare_solutions_fields PASSED
tests/test_engine_output_contract.py::test_infeasible_run_is_saved_with_diagnostics PASSED
tests/test_engine_run.py::test_completed_run_has_indicators_and_strategy_order PASSED
tests/test_engine_run.py::test_dominated_alternative_is_flagged PASSED
tests/test_engine_run.py::test_infeasible_found_by_validation_is_saved_with_reasons PASSED
tests/test_engine_run.py::test_infeasible_found_by_solver_is_saved_with_reasons PASSED
tests/test_engine_run.py::test_invalid_input_is_rejected_and_saved PASSED
tests/test_engine_run.py::test_unknown_strategy_is_invalid_input PASSED
tests/test_engine_run.py::test_gini_matches_analytical_note_examples PASSED
tests/test_engine_run.py::test_unranked_assignment_counts_as_num_projects_plus_one PASSED
tests/test_engine_run.py::test_feasibility_check_reports_violations PASSED
tests/test_engine_run.py::test_compare_counts_unranked_as_worse PASSED
tests/test_engine_run.py::test_tied_ranks_are_invalid_input_not_a_crash PASSED
tests/test_engine_run.py::test_same_project_ranked_twice_is_invalid_input PASSED
tests/test_engine_run.py::test_student_skill_level_outside_1_to_5_is_invalid[0] PASSED
tests/test_engine_run.py::test_student_skill_level_outside_1_to_5_is_invalid[6] PASSED
tests/test_engine_run.py::test_requirement_level_outside_1_to_5_is_invalid PASSED
tests/test_engine_run.py::test_fewer_than_three_preferences_warns_but_solves PASSED
tests/test_engine_run.py::test_bad_data_is_invalid_even_when_also_infeasible PASSED
tests/test_engine_run.py::test_fixed_over_capacity_stays_infeasible_with_reason PASSED
tests/test_metrics.py::test_gini_calculation PASSED
tests/test_metrics.py::test_skill_fit_calculation PASSED
tests/test_metrics.py::test_solution_indicators_calculation PASSED
tests/test_metrics.py::test_solution_comparison PASSED
tests/test_milestone_end_to_end.py::test_milestone_end_to_end PASSED
tests/test_models.py::test_skill_serialization PASSED
tests/test_models.py::test_student_model_helpers PASSED
tests/test_models.py::test_project_model PASSED
tests/test_models.py::test_solution_and_assignment PASSED
tests/test_persistence.py::test_schema_init_and_skill_repo PASSED
tests/test_persistence.py::test_student_and_project_persistence PASSED
tests/test_persistence.py::test_run_and_solution_persistence PASSED
tests/test_solvers.py::test_assignment_strategy_abc PASSED
tests/test_solvers.py::test_assignment_engine_initialization PASSED
tests/test_solvers.py::test_execute_run_completes_standard_scenario PASSED
tests/test_solvers.py::test_distinct_alternatives_generation PASSED
tests/test_validation.py::test_empty_cohort_validation PASSED
tests/test_validation.py::test_duplicate_identifiers PASSED
tests/test_validation.py::test_capacity_errors PASSED
tests/test_validation.py::test_contradictory_fixed_assignments PASSED
tests/test_validation.py::test_fixed_exceeding_capacity PASSED
tests/test_validation.py::test_aggregate_capacity_deficit PASSED
tests/test_visualization_component.py::test_dashboard_loads PASSED
tests/test_visualization_component.py::test_role_switcher PASSED
tests/test_visualization_component.py::test_student_and_fixed_assignment_workflow PASSED
tests/test_visualization_component.py::test_scenario_loading_and_optimization_trigger PASSED
tests/test_visualization_component.py::test_scenario_reload_clears_runs PASSED
tests/test_visualization_component.py::test_create_student_preference_validation PASSED
tests/test_visualization_component.py::test_skills_crud_workflow PASSED
tests/test_visualization_component.py::test_projects_crud_workflow PASSED
tests/test_visualization_component.py::test_compare_solutions_view PASSED
tests/test_visualization_component.py::test_solution_page_fairness_section PASSED
tests/test_visualization_component.py::test_recommendations_single_solution PASSED
tests/test_visualization_component.py::test_recommendations_show_affected_students PASSED

============================== 98 passed in 8.68s ==============================
```
