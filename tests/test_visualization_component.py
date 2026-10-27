"""Tests for Component A (Visualization & Recommendation) web workflows."""

import pathlib

import pytest

from app import create_app
from app.domain import models
from app.persistence import repositories


def test_dashboard_loads(tmp_path: pathlib.Path) -> None:
    """Verifies dashboard overview page rendering."""
    db_file = tmp_path / "test_ui_dash.db"
    app = create_app({"TESTING": True, "DATABASE_PATH": db_file})
    client = app.test_client()

    dash_res = client.get("/")
    assert dash_res.status_code == 200
    assert b"TECH5" in dash_res.data


def test_role_switcher(tmp_path: pathlib.Path) -> None:
    """Verifies role switching in session."""
    db_file = tmp_path / "test_role.db"
    app = create_app({"TESTING": True, "DATABASE_PATH": db_file})
    client = app.test_client()

    res = client.post(
        "/set-role", data={"role": "STUDENT"}, follow_redirects=True
    )
    assert res.status_code == 200
    assert b"Switched role to Student Viewer" in res.data


def test_student_and_fixed_assignment_workflow(
    tmp_path: pathlib.Path,
) -> None:
    """Verifies student and fixed-assignment creation workflows."""
    db_file = tmp_path / "test_stud_fixed.db"
    app = create_app({"TESTING": True, "DATABASE_PATH": db_file})
    client = app.test_client()

    # Seed project P1 so student preference foreign key is valid
    repositories.ProjectRepository().save(
        models.Project(
            id="P1",
            title="FinTech Risk",
            min_capacity=1,
            max_capacity=2,
        )
    )

    # Create student
    res_stud = client.post(
        "/students/new",
        data={
            "id": "S1",
            "display_name": "Alice Chen",
            "preferences_text": "P1",
        },
        follow_redirects=True,
    )
    assert res_stud.status_code == 200
    assert b"Alice Chen" in res_stud.data

    # Create fixed assignment
    res_fixed = client.post(
        "/fixed-assignments/new",
        data={
            "student_id": "S1",
            "project_id": "P1",
            "status": "APPROVED",
            "reason": "Pre-matched",
        },
        follow_redirects=True,
    )
    assert res_fixed.status_code == 200
    assert b"Pre-matched" in res_fixed.data


def test_scenario_loading_and_optimization_trigger(
    tmp_path: pathlib.Path,
) -> None:
    """Verifies benchmark scenario loading, execution, and result views."""
    db_file = tmp_path / "test_scenario_run.db"
    app = create_app({"TESTING": True, "DATABASE_PATH": db_file})
    client = app.test_client()

    # Load standard benchmark
    load_res = client.post("/load-scenario/standard", follow_redirects=True)
    assert load_res.status_code == 200
    assert b"Standard Cohort" in load_res.data

    # Trigger optimization run (falls back to mock engine if solver unimplemented)
    run_res = client.post(
        "/runs/trigger",
        data={
            "primary_strategy": "PREFERENCE",
            "max_alternatives": 3,
        },
        follow_redirects=True,
    )
    assert run_res.status_code == 200
    assert b"Optimization run completed" in run_res.data

    runs = repositories.RunRepository().list_runs(limit=1)
    assert len(runs) == 1
    run = runs[0]
    assert len(run.solutions) >= 1

    # View solution detail
    sol_id = run.solutions[0].id
    sol_res = client.get(f"/solutions/{sol_id}")
    assert sol_res.status_code == 200
    assert b"Student-to-Project Allocations" in sol_res.data

    # View recommendations
    recs_res = client.get(f"/recommendations/{run.id}")
    assert recs_res.status_code == 200
    assert b"Transparent Decision Recommendations" in recs_res.data
    assert b"Calculated Evidence" in recs_res.data
    assert b"Policy Advice" in recs_res.data


def test_create_student_preference_validation(
    tmp_path: pathlib.Path,
) -> None:
    """Verifies preference validation when creating a student."""
    db_file = tmp_path / "test_stud_pref_val.db"
    app = create_app({"TESTING": True, "DATABASE_PATH": db_file})
    client = app.test_client()

    repositories.ProjectRepository().save(
        models.Project(
            id="P1",
            title="Project One",
            min_capacity=1,
            max_capacity=2,
        )
    )

    # 1. Invalid project preference should flash error and not crash
    res_invalid = client.post(
        "/students/new",
        data={
            "id": "S10",
            "display_name": "Test Student",
            "preferences_text": "P99, P100",
        },
        follow_redirects=True,
    )
    assert res_invalid.status_code == 200
    assert b"Invalid project ID(s) in preferences" in res_invalid.data

    # 2. Case-insensitive project preference should normalize and succeed
    res_valid = client.post(
        "/students/new",
        data={
            "id": "S10",
            "display_name": "Test Student",
            "preferences_text": "p1",
        },
        follow_redirects=True,
    )
    assert res_valid.status_code == 200
    assert b"created successfully" in res_valid.data

    saved = repositories.StudentRepository().get_by_id("S10")
    assert saved is not None
    assert len(saved.preferences) == 1
    assert saved.preferences[0].project_id == "P1"


def test_skills_crud_workflow(tmp_path: pathlib.Path) -> None:
    """Verifies skill creation, validation and deletion workflow."""
    db_file = tmp_path / "test_skill.db"
    app = create_app({"TESTING": True, "DATABASE_PATH": db_file})
    client = app.test_client()
    skill_data = {
        "id": "py",
        "name": "Python",
        "description": "Python Programming",
    }

    # 1. Valid skill is created and shown in the catalog
    res = client.post("/skills/new", data=skill_data, follow_redirects=True)
    assert res.status_code == 200
    assert b"created successfully" in res.data
    saved = repositories.SkillRepository().get_by_id("py")
    assert saved is not None
    assert saved.name == "Python"

    # 2. Duplicate identifier is rejected
    res_dup = client.post(
        "/skills/new", data=skill_data, follow_redirects=True
    )
    assert b"already exists" in res_dup.data

    # 3. Missing name is rejected and nothing is saved
    res_empty = client.post(
        "/skills/new", data={"id": "sql", "name": ""}, follow_redirects=True
    )
    assert b"required" in res_empty.data
    assert repositories.SkillRepository().get_by_id("sql") is None

    # 4. Student viewers cannot create skills
    client.post("/set-role", data={"role": "STUDENT"})
    client.post(
        "/skills/new", data={"id": "r", "name": "R"}, follow_redirects=True
    )
    assert repositories.SkillRepository().get_by_id("r") is None
    client.post("/set-role", data={"role": "ADMIN"})

    # 5. Deleting removes the skill; deleting again reports not found
    res_del = client.post("/skills/py/delete", follow_redirects=True)
    assert b"removed" in res_del.data
    assert repositories.SkillRepository().get_by_id("py") is None
    res_missing = client.post("/skills/py/delete", follow_redirects=True)
    assert b"not found" in res_missing.data


def test_projects_crud_workflow(tmp_path: pathlib.Path) -> None:
    """Verifies project addition workflow (Team A implementation)."""
    db_file = tmp_path / "test_proj.db"
    app = create_app({"TESTING": True, "DATABASE_PATH": db_file})
    client = app.test_client()

    client.post(
        "/projects/new",
        data={
            "id": "P1",
            "title": "FinTech Risk",
            "description": "Risk analytics",
            "min_capacity": 1,
            "max_capacity": 2,
        },
        follow_redirects=True,
    )



def test_compare_solutions_view(tmp_path: pathlib.Path) -> None:
    """Verifies solution comparison controller (Team A implementation)."""
    db_file = tmp_path / "test_comp.db"
    app = create_app({"TESTING": True, "DATABASE_PATH": db_file})
    client = app.test_client()

    res = client.get("/compare?sol1=S1&sol2=S2")

    assert res.status_code == 200
    assert b"Baseline solution (A) could not be found." in res.data
    assert b"Comparison solution (B) could not be found." in res.data


def test_solution_page_fairness_section(tmp_path: pathlib.Path) -> None:
    """Verifies the fairness section names students outside preferences."""
    db_file = tmp_path / "test_fairness.db"
    app = create_app({"TESTING": True, "DATABASE_PATH": db_file})
    client = app.test_client()

    # P1 has one seat and P2 two; all three students want only P1.
    # Two of them must end up on P2, a project they did not rank.
    with app.app_context():
        proj_repo = repositories.ProjectRepository()
        proj_repo.save(
            models.Project(
                id="P1", title="Popular", min_capacity=0, max_capacity=1
            )
        )
        proj_repo.save(
            models.Project(
                id="P2", title="Spare", min_capacity=0, max_capacity=2
            )
        )
        stud_repo = repositories.StudentRepository()
        for sid in ("S1", "S2", "S3"):
            stud_repo.save(
                models.Student(
                    id=sid,
                    display_name=f"Student {sid}",
                    preferences=[
                        models.Preference(
                            student_id=sid, project_id="P1", rank=1
                        )
                    ],
                )
            )

    client.post(
        "/runs/trigger",
        data={"primary_strategy": "PREFERENCE", "max_alternatives": 1},
        follow_redirects=True,
    )
    with app.app_context():
        run_repo = repositories.RunRepository()
        run = run_repo.list_runs(limit=1)[0]
        assert run.status == "COMPLETED"
        sol = run_repo.get_solution(run.solutions[0].id)

    res = client.get(f"/solutions/{sol.id}")
    assert res.status_code == 200
    html = res.data.decode()

    assert "Fairness: How Outcomes Are Spread" in html
    assert "Distribution of assigned preference ranks" in html
    assert "Gini coefficient" in html
    # Two students landed outside their preferences and must be named.
    assert "got a project they did not rank" in html
    unranked = [a for a in sol.assignments if a.preference_rank is None]
    assert len(unranked) == 2
    for asg in unranked:
        assert f"Student {asg.student_id}" in html
    assert "Not ranked" in html
