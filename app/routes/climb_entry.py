from flask import Blueprint, render_template, request, session, redirect, flash, jsonify, abort
from datetime import datetime

from app.extensions import db
from app.models import Competition, Section, SectionClimb, Gym
from app.models.section_climb import CLIMB_STYLES, CLIMB_STYLE_LABELS
from app.helpers.admin import admin_can_manage_competition, admin_is_super
from app.helpers.gym import get_session_admin_gym_ids

climb_entry_bp = Blueprint("climb_entry", __name__)


def _require_admin_login():
    if not session.get("account_id"):
        return redirect("/login")
    return None


def _resolve_comp(comp_id_arg):
    """Resolve competition from arg or session."""
    comp_id = comp_id_arg or session.get("admin_comp_id")
    if not comp_id:
        return None
    comp = Competition.query.get(comp_id)
    if comp and admin_can_manage_competition(comp):
        session["admin_comp_id"] = comp.id
        return comp
    return None


@climb_entry_bp.route("/admin/climbs")
def climb_entry():
    guard = _require_admin_login()
    if guard:
        return guard

    comp_id = request.args.get("comp_id", type=int)
    current_comp = _resolve_comp(comp_id)

    if not current_comp:
        flash("Please select a competition first.", "warning")
        return redirect("/admin/comps/manage")

    gym = current_comp.gym
    if not gym:
        flash("This competition has no gym assigned.", "warning")
        return redirect("/admin/comps/manage")

    # Sections are gym-level
    sections = Section.query.filter_by(gym_id=gym.id).order_by(Section.name).all()

    # All climbs for this competition
    climbs = (
        SectionClimb.query
        .filter_by(competition_id=current_comp.id)
        .order_by(SectionClimb.climb_number)
        .all()
    )

    # Build a section lookup for display
    section_map = {s.id: s for s in sections}

    section_names_json = {s.id: s.name for s in sections}

    return render_template(
        "admin_climb_entry.html",
        current_comp=current_comp,
        gym=gym,
        sections=sections,
        section_map=section_map,
        climbs=climbs,
        climb_styles=CLIMB_STYLES,
        climb_style_labels=CLIMB_STYLE_LABELS,
        grading_system=gym.grading_system,
        grade_list=gym.grade_list or [],
        hold_colour_list=gym.hold_colour_list or [],
        section_names_json=section_names_json,
    )


@climb_entry_bp.route("/admin/climbs/add", methods=["POST"])
def climb_entry_add():
    guard = _require_admin_login()
    if guard:
        return guard

    comp_id = request.form.get("comp_id", type=int)
    current_comp = _resolve_comp(comp_id)
    if not current_comp:
        return jsonify({"ok": False, "error": "Competition not found."}), 404

    gym = current_comp.gym
    if not gym:
        return jsonify({"ok": False, "error": "No gym assigned."}), 400

    climb_raw  = (request.form.get("climb_number") or "").strip()
    section_id = request.form.get("section_id", type=int)
    colour     = (request.form.get("colour") or "").strip()
    grade      = (request.form.get("grade") or "").strip()
    styles     = request.form.getlist("styles")
    base_raw   = (request.form.get("base_points") or "").strip()

    valid_styles = set(CLIMB_STYLES)
    styles = [s for s in styles if s in valid_styles]

    # Validate
    if not climb_raw.isdigit():
        return jsonify({"ok": False, "error": "Climb number must be a whole number."})
    if not section_id:
        return jsonify({"ok": False, "error": "Please select a section."})
    if not colour:
        return jsonify({"ok": False, "error": "Please select a hold colour."})
    if not grade:
        return jsonify({"ok": False, "error": "Please select a grade."})
    if not styles:
        return jsonify({"ok": False, "error": "Please select at least one style."})
    if base_raw == "" or not base_raw.lstrip("-").isdigit():
        return jsonify({"ok": False, "error": "Please enter valid base points."})

    climb_number = int(climb_raw)
    base_points  = int(base_raw)

    if climb_number <= 0:
        return jsonify({"ok": False, "error": "Climb number must be positive."})
    if base_points < 0:
        return jsonify({"ok": False, "error": "Base points must be ≥ 0."})

    # Validate section belongs to this gym
    section = Section.query.get(section_id)
    if not section or section.gym_id != gym.id:
        return jsonify({"ok": False, "error": "Invalid section."})

    # Check for duplicate climb number within this competition
    dup = SectionClimb.query.filter_by(
        competition_id=current_comp.id,
        climb_number=climb_number,
    ).first()
    if dup:
        return jsonify({"ok": False, "error": f"Climb #{climb_number} already exists in this competition."})

    sc = SectionClimb(
        section_id=section.id,
        gym_id=gym.id,
        competition_id=current_comp.id,
        climb_number=climb_number,
        colour=colour,
        grade=grade,
        styles=styles,
        base_points=base_points,
        x_percent=None,
        y_percent=None,
    )
    db.session.add(sc)
    db.session.commit()

    return jsonify({
        "ok": True,
        "climb": {
            "id":           sc.id,
            "climb_number": sc.climb_number,
            "section_id":   sc.section_id,
            "section_name": section.name,
            "colour":       sc.colour,
            "grade":        sc.grade,
            "styles":       sc.styles,
            "base_points":  sc.base_points,
        }
    })


@climb_entry_bp.route("/admin/climbs/<int:climb_id>/edit", methods=["POST"])
def climb_entry_edit(climb_id):
    guard = _require_admin_login()
    if guard:
        return guard

    sc = SectionClimb.query.get_or_404(climb_id)
    comp_id = request.form.get("comp_id", type=int) or session.get("admin_comp_id")
    current_comp = _resolve_comp(comp_id)
    if not current_comp or sc.competition_id != current_comp.id:
        return jsonify({"ok": False, "error": "Not found."}), 404

    gym = current_comp.gym
    climb_raw  = (request.form.get("climb_number") or "").strip()
    section_id = request.form.get("section_id", type=int)
    colour     = (request.form.get("colour") or "").strip()
    grade      = (request.form.get("grade") or "").strip()
    styles     = request.form.getlist("styles")
    base_raw   = (request.form.get("base_points") or "").strip()

    valid_styles = set(CLIMB_STYLES)
    styles = [s for s in styles if s in valid_styles]

    if not climb_raw.isdigit():
        return jsonify({"ok": False, "error": "Climb number must be a whole number."})
    if not section_id:
        return jsonify({"ok": False, "error": "Please select a section."})
    if not colour:
        return jsonify({"ok": False, "error": "Please select a hold colour."})
    if not grade:
        return jsonify({"ok": False, "error": "Please select a grade."})
    if not styles:
        return jsonify({"ok": False, "error": "Please select at least one style."})
    if base_raw == "" or not base_raw.lstrip("-").isdigit():
        return jsonify({"ok": False, "error": "Please enter valid base points."})

    climb_number = int(climb_raw)
    base_points  = int(base_raw)

    if climb_number <= 0:
        return jsonify({"ok": False, "error": "Climb number must be positive."})
    if base_points < 0:
        return jsonify({"ok": False, "error": "Base points must be ≥ 0."})

    section = Section.query.get(section_id)
    if not section or section.gym_id != gym.id:
        return jsonify({"ok": False, "error": "Invalid section."})

    # Check for duplicate (exclude self)
    dup = SectionClimb.query.filter(
        SectionClimb.competition_id == current_comp.id,
        SectionClimb.climb_number == climb_number,
        SectionClimb.id != sc.id,
    ).first()
    if dup:
        return jsonify({"ok": False, "error": f"Climb #{climb_number} already exists."})

    sc.climb_number = climb_number
    sc.section_id   = section.id
    sc.colour       = colour
    sc.grade        = grade
    sc.styles       = styles
    sc.base_points  = base_points
    db.session.commit()

    return jsonify({
        "ok": True,
        "climb": {
            "id":           sc.id,
            "climb_number": sc.climb_number,
            "section_id":   sc.section_id,
            "section_name": section.name,
            "colour":       sc.colour,
            "grade":        sc.grade,
            "styles":       sc.styles,
            "base_points":  sc.base_points,
        }
    })


@climb_entry_bp.route("/admin/climbs/<int:climb_id>/delete", methods=["POST"])
def climb_entry_delete(climb_id):
    guard = _require_admin_login()
    if guard:
        return guard

    sc = SectionClimb.query.get_or_404(climb_id)
    comp_id = request.form.get("comp_id", type=int) or session.get("admin_comp_id")
    current_comp = _resolve_comp(comp_id)
    if not current_comp or sc.competition_id != current_comp.id:
        return jsonify({"ok": False, "error": "Not found."}), 404

    db.session.delete(sc)
    db.session.commit()
    return jsonify({"ok": True})


@climb_entry_bp.route("/admin/climbs/<int:climb_id>/place", methods=["POST"])
def climb_place(climb_id):
    guard = _require_admin_login()
    if guard:
        return guard

    sc = SectionClimb.query.get_or_404(climb_id)
    comp_id = request.form.get("comp_id", type=int) or session.get("admin_comp_id")
    current_comp = _resolve_comp(comp_id)
    if not current_comp or sc.competition_id != current_comp.id:
        return jsonify({"ok": False, "error": "Not found."}), 404

    try:
        x = float(request.form.get("x_percent", ""))
        y = float(request.form.get("y_percent", ""))
    except ValueError:
        return jsonify({"ok": False, "error": "Invalid coordinates."})

    sc.x_percent = x
    sc.y_percent = y
    db.session.commit()
    return jsonify({"ok": True, "x_percent": x, "y_percent": y})


@climb_entry_bp.route("/admin/climbs/<int:climb_id>/unplace", methods=["POST"])
def climb_unplace(climb_id):
    guard = _require_admin_login()
    if guard:
        return guard

    sc = SectionClimb.query.get_or_404(climb_id)
    comp_id = request.form.get("comp_id", type=int) or session.get("admin_comp_id")
    current_comp = _resolve_comp(comp_id)
    if not current_comp or sc.competition_id != current_comp.id:
        return jsonify({"ok": False, "error": "Not found."}), 404

    sc.x_percent = None
    sc.y_percent = None
    db.session.commit()
    return jsonify({"ok": True})