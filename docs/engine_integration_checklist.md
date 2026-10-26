# Engine Integration Checklist (Team A → Team B)

This checklist lists what Component A (`app/visualization/`) actually reads
from the Assignment Engine. The method signatures are defined in
`app/contract/contract.py` and documented in
[`internal_component_contract.md`](internal_component_contract.md); this file
adds the concrete fields, keys and values our pages depend on.

`MockAssignmentEngine` (`app/contract/mocks.py`) already returns exactly this
structure, so it can be used as a reference implementation.

When `AssignmentEngine.execute_run()` stops raising `NotImplementedError`,
the web app switches from the mock to the real engine automatically
(`app/visualization/routes.py`, `trigger_run`).

## 1. `execute_run()` must save the run

- [ ] Call `repositories.RunRepository().save_run(run)` before returning,
      also for `INFEASIBLE` and `INVALID_INPUT` runs.
- Reason: the run, solution and comparison pages load runs from the
  database through `RunRepository`, not from the returned object. A run that
  is not saved leads to "Run not found" right after it is triggered.

## 2. `AssignmentRun`

| Field | Requirement |
|---|---|
| `id` | Unique string (UUID). |
| `status` | A **plain string**: `"COMPLETED"`, `"INFEASIBLE"`, `"INVALID_INPUT"`, `"WARNING"` or `"FAILED"`. Use `RunStatus.COMPLETED.value`, not the enum itself — templates compare `run.status == 'COMPLETED'`. |
| `created_at` | ISO 8601 string (the model default is fine). |
| `configuration` | Dict; the dashboard reads `configuration['primary_strategy']`. |
| `solutions` | List of `Solution`; at least two for a `COMPLETED` run so solutions can be compared. Empty for `INFEASIBLE`. |
| `diagnostics` | Dict, non-empty when the run is not `COMPLETED`. Keys shown in the UI (all optional): `message` (str), `reasons` (list of str), `errors` (list of str), `warnings` (list of str), `notes` (str). |

## 3. `Solution`

| Field | Requirement |
|---|---|
| `id`, `run_id` | Unique id; `run_id` of the parent run. |
| `alternative_number` | 1, 2, 3, … in the order generated. |
| `strategy_name` | Name of the strategy that produced the solution. |
| `assignments` | One `Assignment` per student. |
| `indicators` | Nested dict, see section 5. |

## 4. `Assignment`

| Field | Requirement |
|---|---|
| `student_id`, `project_id` | Existing ids. |
| `is_fixed` | `True` if the allocation comes from a fixed assignment. Every solution must keep all fixed assignments (tested in `test_component_integration.py`). |
| `preference_rank` | 1-based rank, or `None` if the project is not in the student's list. |
| `skill_fit_score` | Float in `[0.0, 1.0]`. |
| `unmet_requirements` | List of skill ids the student lacks for that project. |

## 5. `Solution.indicators`

The pages read these keys with `.get()`, so a missing key shows `N/A` or `0`
instead of crashing. All of them should be filled in.

```python
{
    "feasibility": {
        "is_feasible": bool,
        "violations": list[str],
        "assigned_count": int,
        "total_students": int,
    },
    "efficiency": {
        "mean_skill_fit": float,             # 0.0–1.0, shown as a percentage
        "unmet_soft_requirements_count": int,
        "total_skill_fit_sum": float,
    },
    "fairness": {
        "mean_rank": float,
        "worst_rank": int,
        "rank_gini": float,
        "rank_variance": float,
        "unranked_count": int,
        "unranked_pct": float,               # 0–100
        "rank_distribution": {"rank_1": int, "rank_2": int, ...},
    },
    "capacity": {
        "total_assigned": int,
        "total_capacity": int,
        "overall_utilization_pct": float,    # 0–100
        "projects": {
            "<project_id>": {
                "assigned_count": int,
                "min_capacity": int,
                "max_capacity": int,
                "utilization_pct": float,    # 0–100
            },
        },
    },
}
```

The recommendation logic (`app/engine/recommendations.py`) ranks solutions
by `fairness.unranked_count`, `fairness.mean_rank`, `fairness.worst_rank`
and `efficiency.mean_skill_fit`, so these four must be correct, not just
present.

## 6. `compare_solutions()` → `models.RunComparison`

Fields used by the comparison page:

- `is_distinct`
- `changed_students_count`
- `changed_assignments`
- `rank_improved_count`, `rank_worsened_count`
- `delta_mean_skill_fit`, `delta_mean_rank`
- `trade_off_summary` (list of str)

Raise `KeyError` if either solution id does not exist; the page handles it.

## 7. How to check

```powershell
.\.venv\Scripts\pytest tests/test_component_integration.py -v
```

These tests run the full workflow through the web app (scenario, run,
solution view, recommendations, comparison, fixed assignment and rerun,
infeasible cohort, restart). They pass with the mock today and should keep
passing once the real engine is in place.
