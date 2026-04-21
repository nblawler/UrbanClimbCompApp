import json
from flask import Blueprint, render_template, request, session, redirect, flash, jsonify

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


def _parse_colour_list(raw):
    """
    Parse and validate a JSON colour list.
    Returns (cleaned_list, error_string). cleaned_list is None on error.
    """
    try:
        items = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(items, list):
            raise ValueError("Expected a list")
        cleaned = []
        for entry in items:
            label = (entry.get("label") or "").strip()
            colour = (entry.get("colour") or "").strip()
            if not label:
                raise ValueError("Each entry must have a label")
            if not colour:
                raise ValueError("Each entry must have a colour")
            cleaned.append({"label": label, "colour": colour})
        return cleaned, None
    except (json.JSONDecodeError, ValueError, AttributeError) as e:
        return None, str(e)


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


@gym_settings_bp.route("/admin/gym/<int:gym_id>/settings", methods=["GET"])
def gym_settings(gym_id):
    guard = _require_gym_admin_login()
    if guard:
        return guard

    if not admin_can_manage_gym_id(gym_id):
        flash("You don't have access to that gym's settings.", "warning")
        return redirect("/admin")

    gym = Gym.query.get_or_404(gym_id)

    return render_template(
        "admin_gym_settings.html",
        gym=gym,
        grading_systems=GRADING_SYSTEMS,
        grading_system_labels=GRADING_SYSTEM_LABELS,
    )


@gym_settings_bp.route("/admin/gym/<int:gym_id>/settings/api", methods=["POST"])
def gym_settings_api(gym_id):
    """
    JSON API endpoint for all gym settings saves.
    Returns {"ok": true} or {"ok": false, "error": "..."}.
    No page refresh — called via fetch() from the template.
    """
    if not session.get("account_id"):
        return jsonify({"ok": False, "error": "Not logged in"}), 401

    if not admin_can_manage_gym_id(gym_id):
        return jsonify({"ok": False, "error": "Access denied"}), 403

    gym = Gym.query.get_or_404(gym_id)

    data = request.get_json(silent=True) or {}
    action = (data.get("action") or "").strip()

    if action == "save_grading_system":
        grading_system = (data.get("grading_system") or "").strip()
        if grading_system not in GRADING_SYSTEMS:
            return jsonify({"ok": False, "error": "Please select a valid grading system."})

        gym.grading_system = grading_system
        if grading_system != "colour":
            gym.grade_list = None
        db.session.commit()
        return jsonify({"ok": True, "message": "Grading system saved."})

    elif action == "save_grade_list":
        raw = data.get("grade_list") or []
        cleaned, err = _parse_colour_list(raw if isinstance(raw, str) else json.dumps(raw))
        if err:
            return jsonify({"ok": False, "error": f"Invalid grade list: {err}"})
        if not cleaned:
            return jsonify({"ok": False, "error": "Please add at least one grade."})
        gym.grade_list = cleaned
        db.session.commit()
        return jsonify({"ok": True, "message": "Grade list saved."})

    elif action == "save_hold_colour_list":
        raw = data.get("hold_colour_list") or []
        cleaned, err = _parse_colour_list(raw if isinstance(raw, str) else json.dumps(raw))
        if err:
            return jsonify({"ok": False, "error": f"Invalid hold colour list: {err}"})
        if not cleaned:
            return jsonify({"ok": False, "error": "Please add at least one hold colour."})
        gym.hold_colour_list = cleaned
        db.session.commit()
        return jsonify({"ok": True, "message": "Hold colour list saved."})

    else:
        return jsonify({"ok": False, "error": "Unknown action."})