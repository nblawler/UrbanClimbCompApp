import json
from flask import Blueprint, render_template, request, session, redirect, flash

from app.extensions import db
from app.models import Gym
from app.models.gym import GRADING_SYSTEMS, GRADING_SYSTEM_LABELS
from app.helpers.admin import admin_is_super, admin_can_manage_gym_id
from app.helpers.gym import get_session_admin_gym_ids

gym_settings_bp = Blueprint("gym_settings", __name__)


def _require_gym_admin_login():
    if not session.get("account_id"):
        return redirect("/login")
    if admin_is_super():
        return None
    gym_ids = get_session_admin_gym_ids() or []
    if not gym_ids:
        flash("You don't have admin access.", "warning")
        return redirect("/")
    return None


def _get_admin_gyms():
    if admin_is_super():
        return Gym.query.order_by(Gym.name).all()
    allowed_gym_ids = get_session_admin_gym_ids()
    if not allowed_gym_ids:
        return []
    return Gym.query.filter(Gym.id.in_(allowed_gym_ids)).order_by(Gym.name).all()


@gym_settings_bp.route("/admin/gym/settings")
def gym_settings_picker():
    guard = _require_gym_admin_login()
    if guard:
        return guard

    gyms = _get_admin_gyms()

    if not gyms:
        flash("You don't have admin access to any gyms.", "warning")
        return redirect("/admin")

    if len(gyms) == 1:
        return redirect(f"/admin/gym/{gyms[0].id}/settings")

    return render_template("admin_gym_settings_picker.html", gyms=gyms)


@gym_settings_bp.route("/admin/gym/<int:gym_id>/settings", methods=["GET", "POST"])
def gym_settings(gym_id):
    guard = _require_gym_admin_login()
    if guard:
        return guard

    if not admin_can_manage_gym_id(gym_id):
        flash("You don't have access to that gym's settings.", "warning")
        return redirect("/admin")

    gym = Gym.query.get_or_404(gym_id)

    message = None
    error = None

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()

        if action == "save_gym_settings":
            grading_system = (request.form.get("grading_system") or "").strip()

            if grading_system not in GRADING_SYSTEMS:
                error = "Please select a valid grading system."
            else:
                gym.grading_system = grading_system

                # If switching away from colour, clear the grade list
                if grading_system != "colour":
                    gym.grade_list = None

                db.session.commit()
                message = f"Grading system saved for {gym.name}."

        elif action == "save_grade_list":
            # Receives JSON string of the ordered grade list from the drag UI
            raw = (request.form.get("grade_list_json") or "").strip()
            try:
                grade_list = json.loads(raw)
                # Validate structure — each entry must have label and colour
                if not isinstance(grade_list, list):
                    raise ValueError("Expected a list")
                cleaned = []
                for entry in grade_list:
                    label = (entry.get("label") or "").strip()
                    colour = (entry.get("colour") or "").strip()
                    if not label:
                        raise ValueError("Each grade must have a label")
                    if not colour:
                        raise ValueError("Each grade must have a colour")
                    cleaned.append({"label": label, "colour": colour})

                if len(cleaned) == 0:
                    error = "Please add at least one grade."
                else:
                    gym.grade_list = cleaned
                    db.session.commit()
                    message = f"Grade list saved for {gym.name}."

            except (json.JSONDecodeError, ValueError, AttributeError) as e:
                error = f"Invalid grade list: {e}"

        else:
            error = "Unknown action."

    return render_template(
        "admin_gym_settings.html",
        gym=gym,
        grading_systems=GRADING_SYSTEMS,
        grading_system_labels=GRADING_SYSTEM_LABELS,
        message=message,
        error=error,
    )