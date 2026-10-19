"""Web routes and controller logic for Component A (Visualization).

This module provides interactive CRUD interfaces, scenario seeders, optimization
triggering, multi-solution visual comparisons, and recommendations.
"""

import collections
import csv
import io
import sqlite3
from typing import Any

import flask

from app.contract import contract
from app.contract import mocks
from app.domain import models
from app.engine import recommendations
from app.engine import solvers
from app.persistence import repositories
from app.samples import demo_data
from app.visualization import auth

web_bp = flask.Blueprint("visualization_web", __name__)


@web_bp.app_context_processor
def inject_template_globals() -> dict[str, Any]:
    """Injects authorization helpers and active role into templates."""
    return {
        "current_role": auth.get_current_role(),
        "all_roles": auth.UserRole.ALL_ROLES,
        "can_edit": auth.can_edit_cohort_data(),
        "can_optimize": auth.can_trigger_optimization(),
    }


@web_bp.post("/set-role")
def set_role() -> flask.Response:
    """Updates the active user role prototype."""
    role = flask.request.form.get("role", auth.UserRole.ADMIN)
    auth.set_current_role(role)
    role_name = auth.UserRole.ALL_ROLES.get(role, role)
    flask.flash(f"Switched role to {role_name}.", "info")
    target = flask.request.referrer or flask.url_for(
        "visualization_web.dashboard"
    )
    return flask.redirect(target)


@web_bp.get("/")
def dashboard() -> str:
    """Renders main dashboard overview with cohort metrics and runs."""
    stud_repo = repositories.StudentRepository()
    proj_repo = repositories.ProjectRepository()
    fixed_repo = repositories.FixedAssignmentRepository()
    run_repo = repositories.RunRepository()

    students = stud_repo.list_all()
    projects = proj_repo.list_all()
    fixed_assignments = fixed_repo.list_all()
    runs = run_repo.list_runs(limit=10)

    total_capacity = sum(p.max_capacity for p in projects)
    latest_run = runs[0] if runs else None

    return flask.render_template(
        "dashboard.html",
        student_count=len(students),
        project_count=len(projects),
        total_capacity=total_capacity,
        fixed_count=len(fixed_assignments),
        recent_runs=runs,
        latest_run=latest_run,
    )


# -----------------------------------------------------------------------------
# Students CRUD
# -----------------------------------------------------------------------------


@web_bp.get("/students")
def list_students() -> str:
    """Lists all students with their proficiencies and ranked preferences."""
    students = repositories.StudentRepository().list_all()
    projects = repositories.ProjectRepository().list_all()
    skills = repositories.SkillRepository().list_all()
    return flask.render_template(
        "students.html",
        students=students,
        projects=projects,
        skills=skills,
    )


@web_bp.get("/students/new")
def new_student_form() -> str:
    """Displays form to create a new student."""
    projects = repositories.ProjectRepository().list_all()
    skills = repositories.SkillRepository().list_all()
    return flask.render_template(
        "student_form.html",
        student=None,
        projects=projects,
        skills=skills,
    )


def _check_valid_student_data(
    student_id: str,
    student_name: str,
    available_students: dict[str, str],
) -> bool:
    """Checks if the student data is valid
    Args:
        student_id: Target student identifier.
        student_name: Target student name.
        available_students: Map of existing students id and name.
    Returns:
        A list of invalid students.
    """
    

    valid = 1

    if student_id in available_students:
        valid = 0
    elif student_name in available_students.values():
        valid = 0


    return valid


def _parse_and_validate_preferences(
    student_id: str,
    pref_raw: str,
    available_projects: dict[str, models.Project],
) -> tuple[list[models.Preference], list[str]]:
    """Parses and validates comma-separated project preferences.

    Args:
        student_id: Target student identifier.
        pref_raw: Comma-separated project IDs string from user input.
        available_projects: Map of valid project ID to Project model.

    Returns:
        A tuple of (valid_preferences_list, invalid_project_ids_list).
    """
    raw_tokens = [p.strip() for p in pref_raw.split(",") if p.strip()]
    case_map = {p_id.upper(): p_id for p_id in available_projects}

    valid_prefs: list[models.Preference] = []
    invalid_ids: list[str] = []
    seen: set[str] = set()

    for token in raw_tokens:
        resolved_id = None
        if token in available_projects:
            resolved_id = token
        elif token.upper() in case_map:
            resolved_id = case_map[token.upper()]
        else:
            invalid_ids.append(token)
            continue

        if resolved_id not in seen:
            seen.add(resolved_id)
            valid_prefs.append(
                models.Preference(
                    student_id=student_id,
                    project_id=resolved_id,
                    rank=len(valid_prefs) + 1,
                )
            )

    return valid_prefs, invalid_ids


@web_bp.post("/students/new")
def create_student() -> flask.Response:
    """Processes creation of a new student."""
    if not auth.can_edit_cohort_data():
        flask.flash("Permission denied for current role.", "danger")
        return flask.redirect(flask.url_for("visualization_web.list_students"))

    s_id = flask.request.form.get("id", "").strip()
    name = flask.request.form.get("display_name", "").strip() or s_id

    available_students = {
        s.id: s.display_name for s in repositories.StudentRepository().list_all()
    }

    if not _check_valid_student_data(s_id,name, available_students):
        flask.flash(f"Student ID: {s_id}, or Name: {name} already exists.", "danger")
        return flask.redirect(
            flask.url_for("visualization_web.new_student_form")
        )

    if not s_id:
        flask.flash("Student ID cannot be empty.", "warning")
        return flask.redirect(
            flask.url_for("visualization_web.new_student_form")
        )

    # Parse skills
    skills_dict: dict[str, int] = {}
    valid_skills = {s.id for s in repositories.SkillRepository().list_all()}
    for key, val in flask.request.form.items():
        if key.startswith("skill_") and val:
            skill_id = key.replace("skill_", "")
            if skill_id in valid_skills:
                try:
                    lvl = int(val)
                    if lvl > 0:
                        skills_dict[skill_id] = lvl
                except ValueError:
                    pass

    # Parse and validate preferences against catalog
    available_projects = {
        p.id: p for p in repositories.ProjectRepository().list_all()
    }
    pref_raw = flask.request.form.get("preferences_text", "")
    preferences, invalid_proj_ids = _parse_and_validate_preferences(
        s_id, pref_raw, available_projects
    )

    if invalid_proj_ids:
        valid_list = (
            ", ".join(sorted(available_projects.keys())) or "None available"
        )
        flask.flash(
            f"Invalid project ID(s) in preferences: "
            f"{', '.join(invalid_proj_ids)}. "
            f"Available projects: {valid_list}.",
            "danger",
        )
        return flask.redirect(
            flask.url_for("visualization_web.new_student_form")
        )

    student = models.Student(
        id=s_id,
        display_name=name,
        skills=skills_dict,
        preferences=preferences,
    )
    try:
        repositories.StudentRepository().save(student)
    except sqlite3.IntegrityError as exc:
        flask.flash(
            f"Database constraint error while saving student: {exc}",
            "danger",
        )
        return flask.redirect(
            flask.url_for("visualization_web.new_student_form")
        )

    flask.flash(f"Student '{name}' ({s_id}) created successfully.", "success")
    return flask.redirect(flask.url_for("visualization_web.list_students"))


@web_bp.get("/students/<student_id>/edit")
def edit_student_form(student_id: str) -> Any:
    """Displays form to edit an existing student."""
    student = repositories.StudentRepository().get_by_id(student_id)
    if not student:
        flask.flash(f"Student '{student_id}' not found.", "danger")
        return flask.redirect(flask.url_for("visualization_web.list_students"))

    projects = repositories.ProjectRepository().list_all()
    skills = repositories.SkillRepository().list_all()
    return flask.render_template(
        "student_form.html",
        student=student,
        projects=projects,
        skills=skills,
    )


@web_bp.post("/students/<student_id>/edit")
def update_student(student_id: str) -> flask.Response:
    """Updates an existing student entity."""
    if not auth.can_edit_cohort_data():
        flask.flash("Permission denied for current role.", "danger")
        return flask.redirect(flask.url_for("visualization_web.list_students"))

    name = (
        flask.request.form.get("display_name", "").strip() or student_id
    )

    valid_skills = {s.id for s in repositories.SkillRepository().list_all()}
    skills_dict: dict[str, int] = {}
    for key, val in flask.request.form.items():
        if key.startswith("skill_") and val:
            skill_id = key.replace("skill_", "")
            if skill_id in valid_skills:
                try:
                    lvl = int(val)
                    if lvl > 0:
                        skills_dict[skill_id] = lvl
                except ValueError:
                    pass

    # Parse and validate preferences against catalog
    available_projects = {
        p.id: p for p in repositories.ProjectRepository().list_all()
    }
    pref_raw = flask.request.form.get("preferences_text", "")
    preferences, invalid_proj_ids = _parse_and_validate_preferences(
        student_id, pref_raw, available_projects
    )

    if invalid_proj_ids:
        valid_list = (
            ", ".join(sorted(available_projects.keys())) or "None available"
        )
        flask.flash(
            f"Invalid project ID(s) in preferences: "
            f"{', '.join(invalid_proj_ids)}. "
            f"Available projects: {valid_list}.",
            "danger",
        )
        return flask.redirect(
            flask.url_for(
                "visualization_web.edit_student_form", student_id=student_id
            )
        )

    student = models.Student(
        id=student_id,
        display_name=name,
        skills=skills_dict,
        preferences=preferences,
    )
    try:
        repositories.StudentRepository().save(student)
    except sqlite3.IntegrityError as exc:
        flask.flash(
            f"Database constraint error while updating student: {exc}",
            "danger",
        )
        return flask.redirect(
            flask.url_for(
                "visualization_web.edit_student_form", student_id=student_id
            )
        )

    flask.flash(f"Student '{student_id}' updated.", "success")
    return flask.redirect(flask.url_for("visualization_web.list_students"))


@web_bp.post("/students/<student_id>/delete")
def delete_student(student_id: str) -> flask.Response:
    """Deletes a student."""
    if not auth.can_edit_cohort_data():
        flask.flash("Permission denied.", "danger")
        return flask.redirect(flask.url_for("visualization_web.list_students"))

    repositories.StudentRepository().delete(student_id)
    flask.flash(f"Student '{student_id}' removed.", "info")
    return flask.redirect(flask.url_for("visualization_web.list_students"))


# -----------------------------------------------------------------------------
# Projects CRUD
# -----------------------------------------------------------------------------


@web_bp.get("/projects")
def list_projects() -> str:
    """Displays project catalog and capacity definitions."""
    projects = repositories.ProjectRepository().list_all()
    skills = repositories.SkillRepository().list_all()
    return flask.render_template(
        "projects.html", projects=projects, skills=skills
    )


@web_bp.get("/projects/new")
def new_project_form() -> str:
    """Displays form to create a semester project."""
    skills = repositories.SkillRepository().list_all()
    return flask.render_template(
        "project_form.html", project=None, skills=skills
    )


def _parse_project_form(
    project_id: str,
) -> tuple[models.Project | None, str | None]:
    """Reads the project form and builds a Project, or returns an error.

    Args:
        project_id: Identifier the project should be saved under.

    Returns:
        A tuple of (project, error_message). Exactly one of them is None.
    """
    form = flask.request.form
    title = form.get("title", "").strip()
    description = form.get("description", "").strip()

    if not title:
        return None, "Project title cannot be empty."

    # Capacities: must be whole numbers, min >= 0, max >= 1, min <= max.
    try:
        min_capacity = int(form.get("min_capacity", "0") or 0)
        max_capacity = int(form.get("max_capacity", "1") or 1)
    except ValueError:
        return None, "Capacities must be whole numbers."

    if min_capacity < 0:
        return None, "Minimum capacity cannot be negative."
    if max_capacity < 1:
        return None, "Maximum capacity must be at least 1."
    if min_capacity > max_capacity:
        return None, (
            f"Minimum capacity ({min_capacity}) cannot exceed "
            f"maximum capacity ({max_capacity})."
        )

    # Skill requirements: one row per catalogued skill, named
    # req_level_<skill_id> (0 to 5) and req_mand_<skill_id> (checkbox).
    valid_skills = {s.id for s in repositories.SkillRepository().list_all()}
    requirements: list[models.ProjectRequirement] = []
    for key, val in form.items():
        if not key.startswith("req_level_"):
            continue
        skill_id = key.replace("req_level_", "")
        if skill_id not in valid_skills:
            continue
        try:
            level = int(val or 0)
        except ValueError:
            return None, f"Invalid level for skill '{skill_id}'."
        if level < 0 or level > 5:
            return None, (
                f"Level for skill '{skill_id}' must be between 0 and 5."
            )
        if level == 0:
            continue  # 0 means "not required"
        is_mandatory = form.get(f"req_mand_{skill_id}") == "1"
        requirements.append(
            models.ProjectRequirement(
                project_id=project_id,
                skill_id=skill_id,
                minimum_level=level,
                is_mandatory=is_mandatory,
            )
        )

    project = models.Project(
        id=project_id,
        title=title,
        description=description,
        min_capacity=min_capacity,
        max_capacity=max_capacity,
        requirements=requirements,
    )
    return project, None


@web_bp.post("/projects/new")
def create_project() -> flask.Response:
    """Processes creation of a project."""
    if not auth.can_edit_cohort_data():
        flask.flash("Permission denied for current role.", "danger")
        return flask.redirect(flask.url_for("visualization_web.list_projects"))

    p_id = flask.request.form.get("id", "").strip()
    if not p_id:
        flask.flash("Project ID cannot be empty.", "warning")
        return flask.redirect(
            flask.url_for("visualization_web.new_project_form")
        )

    if repositories.ProjectRepository().get_by_id(p_id):
        flask.flash(f"Project ID '{p_id}' already exists.", "danger")
        return flask.redirect(
            flask.url_for("visualization_web.new_project_form")
        )

    project, error = _parse_project_form(p_id)
    if error:
        flask.flash(error, "danger")
        return flask.redirect(
            flask.url_for("visualization_web.new_project_form")
        )

    try:
        repositories.ProjectRepository().save(project)
    except sqlite3.IntegrityError as exc:
        flask.flash(
            f"Database constraint error while saving project: {exc}",
            "danger",
        )
        return flask.redirect(
            flask.url_for("visualization_web.new_project_form")
        )

    flask.flash(
        f"Project '{project.title}' ({p_id}) created successfully.", "success"
    )
    return flask.redirect(flask.url_for("visualization_web.list_projects"))


@web_bp.get("/projects/<project_id>/edit")
def edit_project_form(project_id: str) -> Any:
    """Displays form to edit a project."""
    project = repositories.ProjectRepository().get_by_id(project_id)
    if not project:
        flask.flash(f"Project '{project_id}' not found.", "danger")
        return flask.redirect(flask.url_for("visualization_web.list_projects"))

    skills = repositories.SkillRepository().list_all()
    return flask.render_template(
        "project_form.html", project=project, skills=skills
    )


@web_bp.post("/projects/<project_id>/edit")
def update_project(project_id: str) -> flask.Response:
    """Updates an existing project entity."""
    if not auth.can_edit_cohort_data():
        flask.flash("Permission denied for current role.", "danger")
        return flask.redirect(flask.url_for("visualization_web.list_projects"))

    repo = repositories.ProjectRepository()
    if not repo.get_by_id(project_id):
        flask.flash(f"Project '{project_id}' not found.", "danger")
        return flask.redirect(flask.url_for("visualization_web.list_projects"))

    edit_url = flask.url_for(
        "visualization_web.edit_project_form", project_id=project_id
    )

    project, error = _parse_project_form(project_id)
    if error:
        flask.flash(error, "danger")
        return flask.redirect(edit_url)

    try:
        repo.save(project)
    except sqlite3.IntegrityError as exc:
        flask.flash(
            f"Database constraint error while updating project: {exc}",
            "danger",
        )
        return flask.redirect(edit_url)

    flask.flash(f"Project '{project_id}' updated.", "success")
    return flask.redirect(flask.url_for("visualization_web.list_projects"))


@web_bp.post("/projects/<project_id>/delete")
def delete_project(project_id: str) -> flask.Response:
    """Deletes a project."""
    if not auth.can_edit_cohort_data():
        flask.flash("Permission denied.", "danger")
        return flask.redirect(flask.url_for("visualization_web.list_projects"))

    # Deleting a project cascades in the database: student preferences and
    # fixed assignments pointing at it disappear too. Tell the user so the
    # side effect is visible rather than silent.
    affected_students = [
        s.id
        for s in repositories.StudentRepository().list_all()
        if any(pref.project_id == project_id for pref in s.preferences)
    ]
    affected_locks = [
        fa.student_id
        for fa in repositories.FixedAssignmentRepository().list_all()
        if fa.project_id == project_id
    ]

    deleted = repositories.ProjectRepository().delete(project_id)
    if not deleted:
        flask.flash(f"Project '{project_id}' not found.", "warning")
        return flask.redirect(flask.url_for("visualization_web.list_projects"))

    message = f"Project '{project_id}' removed."
    if affected_students:
        message += (
            f" It was removed from the preferences of "
            f"{len(affected_students)} student(s)."
        )
    if affected_locks:
        message += (
            f" {len(affected_locks)} fixed assignment(s) to it were "
            f"also removed."
        )
    flask.flash(message, "warning" if (affected_students or affected_locks) else "info")
    return flask.redirect(flask.url_for("visualization_web.list_projects"))

# -----------------------------------------------------------------------------
# Skills CRUD & Preferences Editor
# -----------------------------------------------------------------------------


@web_bp.get("/skills")
def list_skills() -> str:
    """Lists skill catalog records."""
    skills = repositories.SkillRepository().list_all()
    return flask.render_template("skills.html", skills=skills)


@web_bp.post("/skills/new")
def create_skill() -> flask.Response:
    """Creates a new skill record.

    Raises:
        NotImplementedError: To be implemented by Team A.
    """
    raise NotImplementedError(
        "TODO (Team A): Implement skill creation"
    )


@web_bp.post("/skills/<skill_id>/delete")
def delete_skill(skill_id: str) -> flask.Response:
    """Deletes a skill record.

    Raises:
        NotImplementedError: To be implemented by Team A.
    """
    raise NotImplementedError(
        "TODO (Team A): Implement skill deletion"
    )


@web_bp.get("/preferences")
def preferences_overview() -> str:
    """Displays cross-table preference matrix."""
    students = repositories.StudentRepository().list_all()
    projects = repositories.ProjectRepository().list_all()
    return flask.render_template(
        "preferences.html", students=students, projects=projects
    )


# -----------------------------------------------------------------------------
# Fixed Assignments & Reruns
# -----------------------------------------------------------------------------


@web_bp.get("/fixed-assignments")
def list_fixed_assignments() -> str:
    """Lists administrative pre-allocated constraints."""
    fixed = repositories.FixedAssignmentRepository().list_all()
    students = repositories.StudentRepository().list_all()
    projects = repositories.ProjectRepository().list_all()
    return flask.render_template(
        "fixed_assignments.html",
        fixed_assignments=fixed,
        students=students,
        projects=projects,
    )


@web_bp.post("/fixed-assignments/new")
def create_fixed_assignment() -> flask.Response:
    """Locks a student to a designated project."""
    target = flask.url_for("visualization_web.list_fixed_assignments")
    if not auth.can_edit_cohort_data():
        flask.flash("Permission denied.", "danger")
        return flask.redirect(target)

    s_id = flask.request.form.get("student_id", "").strip()
    p_id = flask.request.form.get("project_id", "").strip()
    reason = flask.request.form.get("reason", "").strip()
    status = flask.request.form.get("status", "APPROVED").strip()

    if not s_id or not p_id:
        flask.flash("Student and Project must be selected.", "warning")
        return flask.redirect(target)

    repositories.FixedAssignmentRepository().save(
        models.FixedAssignment(
            student_id=s_id, project_id=p_id, status=status, reason=reason
        )
    )
    flask.flash(
        f"Fixed assignment created: Student {s_id} locked to Project {p_id}. "
        "A new run is required to recalculate allocations.",
        "success",
    )
    return flask.redirect(target)


@web_bp.post("/fixed-assignments/<student_id>/delete")
def delete_fixed_assignment(student_id: str) -> flask.Response:
    """Removes a locked allocation."""
    target = flask.url_for("visualization_web.list_fixed_assignments")
    if not auth.can_edit_cohort_data():
        flask.flash("Permission denied.", "danger")
        return flask.redirect(target)

    repositories.FixedAssignmentRepository().delete(student_id)
    flask.flash(f"Fixed assignment for student '{student_id}' removed.", "info")
    return flask.redirect(target)


# -----------------------------------------------------------------------------
# Scenarios & Data Seeding
# -----------------------------------------------------------------------------


@web_bp.get("/scenarios")
def scenarios_page() -> str:
    """Displays benchmark scenarios loader."""
    return flask.render_template("scenarios.html")


@web_bp.post("/load-scenario/<scenario_name>")
def load_scenario(scenario_name: str) -> flask.Response:
    """Populates database with a designated reproducible benchmark cohort."""
    repo_run = repositories.RunRepository()
    repo_run.clear_all_cohort_data()

    if scenario_name == "standard":
        data = demo_data.get_standard_scenario()
    elif scenario_name == "fixed":
        data = demo_data.get_fixed_assignment_scenario()
    elif scenario_name == "infeasible":
        data = demo_data.get_infeasible_scenario()
    elif scenario_name == "conflicting":
        data = demo_data.get_conflicting_fixed_scenario()
    else:
        flask.flash(f"Unknown scenario: '{scenario_name}'.", "warning")
        return flask.redirect(flask.url_for("visualization_web.scenarios_page"))

    skill_repo = repositories.SkillRepository()
    for s in data["skills"]:
        skill_repo.save(s)

    proj_repo = repositories.ProjectRepository()
    for p in data["projects"]:
        proj_repo.save(p)

    stud_repo = repositories.StudentRepository()
    for st in data["students"]:
        stud_repo.save(st)

    fixed_repo = repositories.FixedAssignmentRepository()
    for fa in data.get("fixed_assignments", []):
        fixed_repo.save(fa)

    flask.flash(
        f"Benchmark '{data['scenario_name']}' loaded successfully! "
        f"{data['description']}",
        "success",
    )
    return flask.redirect(flask.url_for("visualization_web.dashboard"))


@web_bp.post("/clear-data")
def clear_data() -> flask.Response:
    """Clears all cohort and constraint data."""
    repositories.RunRepository().clear_all_cohort_data()
    flask.flash("All cohort data cleared.", "info")
    return flask.redirect(flask.url_for("visualization_web.dashboard"))


# -----------------------------------------------------------------------------
# Runs & Solutions
# -----------------------------------------------------------------------------


@web_bp.post("/runs/trigger")
def trigger_run() -> flask.Response:
    """Submits current cohort data to Service A to generate allocations."""
    if not auth.can_trigger_optimization():
        flask.flash("Permission denied for current role.", "danger")
        return flask.redirect(flask.url_for("visualization_web.dashboard"))

    max_alts = int(flask.request.form.get("max_alternatives", 3))
    strat = flask.request.form.get("primary_strategy", "PREFERENCE")

    students = repositories.StudentRepository().list_all()
    projects = repositories.ProjectRepository().list_all()
    skills = repositories.SkillRepository().list_all()
    fixed = repositories.FixedAssignmentRepository().list_all()

    engine: contract.AssignmentEngineContract = solvers.AssignmentEngine()
    req = contract.RunRequest(
        students=students,
        projects=projects,
        skills=skills,
        fixed_assignments=fixed,
        strategy_key=strat,
        max_alternatives=max_alts,
    )
    val_outcome = engine.validate_inputs(req)
    for warning_msg in val_outcome.warnings:
        flask.flash(f"Warning: {warning_msg}", "warning")
    try:
        run = engine.execute_run(req)
    except NotImplementedError:
        flask.flash(
            "Notice: AssignmentEngine.execute_run() is not yet implemented by "
            "Team B. Running with MockAssignmentEngine for demonstration.",
            "info",
        )
        mock_engine = mocks.MockAssignmentEngine()
        run = mock_engine.execute_run(req)

    if run.status == "COMPLETED":
        flask.flash(
            f"Optimization run completed! Found {len(run.solutions)} "
            "distinct alternative solutions.",
            "success",
        )
    elif run.status == "INFEASIBLE":
        flask.flash(
            "Solver could not find a feasible solution under the specified "
            "hard constraints. Inspect diagnostics below.",
            "warning",
        )
    else:
        flask.flash(f"Run ended with status: {run.status}.", "danger")

    return flask.redirect(
        flask.url_for("visualization_web.view_run", run_id=run.id)
    )


@web_bp.get("/runs/<run_id>")
def view_run(run_id: str) -> Any:
    """Displays results, alternative solutions, and diagnostics of a run."""
    run = repositories.RunRepository().get_run(run_id)
    if not run:
        flask.flash(f"Run '{run_id}' not found.", "danger")
        return flask.redirect(flask.url_for("visualization_web.dashboard"))

    recs = recommendations.generate_recommendations(run.solutions)

    return flask.render_template(
        "run_detail.html",
        run=run,
        recommendations=recs,
    )


@web_bp.get("/solutions/<solution_id>")
def view_solution(solution_id: str) -> Any:
    """Displays complete student assignment details for a solution."""
    repo = repositories.RunRepository()
    solution = repo.get_solution(solution_id)
    if not solution:
        flask.flash(f"Solution '{solution_id}' not found.", "danger")
        return flask.redirect(flask.url_for("visualization_web.dashboard"))

    run = repo.get_run(solution.run_id)
    students = {s.id: s for s in repositories.StudentRepository().list_all()}
    projects = {p.id: p for p in repositories.ProjectRepository().list_all()}

    return flask.render_template(
        "solution_detail.html",
        solution=solution,
        run=run,
        students=students,
        projects=projects,
    )


@web_bp.post("/solutions/<solution_id>/lock-and-rerun")
def lock_and_rerun(solution_id: str) -> flask.Response:
    """Locks a student assignment from a solution and triggers a new run."""
    student_id = flask.request.form.get("student_id")
    project_id = flask.request.form.get("project_id")

    if not student_id or not project_id:
        flask.flash("Student and Project required.", "warning")
        return flask.redirect(
            flask.url_for(
                "visualization_web.view_solution", solution_id=solution_id
            )
        )

    # Persist as fixed assignment
    fixed_repo = repositories.FixedAssignmentRepository()
    fixed_repo.save(
        models.FixedAssignment(
            student_id=student_id,
            project_id=project_id,
            status="APPROVED",
            reason=f"Locked interactively from Solution {solution_id[:8]}.",
        )
    )

    # Immediately trigger rerun via Component B (Assignment Engine)
    students = repositories.StudentRepository().list_all()
    projects = repositories.ProjectRepository().list_all()
    skills = repositories.SkillRepository().list_all()
    fixed = fixed_repo.list_all()

    engine: contract.AssignmentEngineContract = solvers.AssignmentEngine()
    req = contract.RunRequest(
        students=students,
        projects=projects,
        skills=skills,
        fixed_assignments=fixed,
    )
    try:
        new_run = engine.execute_run(req)
    except NotImplementedError:
        new_run = mocks.MockAssignmentEngine().execute_run(req)

    flask.flash(
        f"Student {student_id} locked to Project {project_id}. "
        "New run generated!",
        "success",
    )
    return flask.redirect(
        flask.url_for("visualization_web.view_run", run_id=new_run.id)
    )

@web_bp.post("/clear-solutions")
def clear_solutions() -> flask.Response:
    """Clears all solutions in the current run."""
    repositories.RunRepository().clear_all_current_solutions()
    flask.flash("All solutions cleared.", "info")
    return flask.redirect(flask.url_for("visualization_web.dashboard"))

# -----------------------------------------------------------------------------
# Comparison & Recommendations
# -----------------------------------------------------------------------------


@web_bp.get("/compare")
def compare_solutions() -> Any:
    """Renders side-by-side comparative analysis of two solutions.

    Raises:
        NotImplementedError: To be implemented by Team A.
    """
    raise NotImplementedError(
        "TODO (Team A): Implement side-by-side solution comparison controller"
    )


@web_bp.get("/recommendations/<run_id>")
def view_recommendations(run_id: str) -> Any:
    """Displays transparent recommendations with evidence and advice."""
    run = repositories.RunRepository().get_run(run_id)
    if not run:
        flask.flash(f"Run '{run_id}' not found.", "danger")
        return flask.redirect(flask.url_for("visualization_web.dashboard"))

    recs = recommendations.generate_recommendations(run.solutions)

    return flask.render_template(
        "recommendations.html",
        run=run,
        recommendations=recs,
    )
