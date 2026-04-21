from flask import Blueprint, render_template, request, session, jsonify, abort, redirect, flash
from sqlalchemy import case, asc, desc

from datetime import timezone, datetime
from zoneinfo import ZoneInfo
import sys

from app.extensions import db
from app.models import Competition, Competitor, Score, Section, SectionClimb, Gym, DoublesTeam, GymAdmin
from app.helpers.admin import admin_can_manage_competition, admin_is_super
from app.helpers.climb import parse_boundary_points, boundary_to_json
from app.helpers.competition import get_current_comp
from app.helpers.gym import get_session_admin_gym_ids
from app.helpers.leaderboard_cache import invalidate_leaderboard_cache
from app.helpers.url import slugify

admin_bp = Blueprint("admin", __name__)


def _is_logged_in():
    return bool(session.get("account_id"))


def _has_any_admin_access():
    if not _is_logged_in():
        return False

    if admin_is_super():
        return True

    gym_ids = get_session_admin_gym_ids() or []
    return bool(gym_ids)


def _require_admin_login():
    if not session.get("account_id"):
        return redirect("/login")

    if not _has_any_admin_access():
        flash("You don't have admin access.", "warning")
        return redirect("/")

    return None


def _resolve_admin_current_comp():
    admin_comp_id = session.get("admin_comp_id")
    if admin_comp_id:
        comp = Competition.query.get(admin_comp_id)
        if comp:
            return comp
        session.pop("admin_comp_id", None)

    return get_current_comp()


def _get_admin_gyms_and_super():
    is_super = admin_is_super()

    if is_super:
        gyms = Gym.query.order_by(Gym.name).all()
    else:
        allowed_gym_ids = get_session_admin_gym_ids()
        gyms = (
            Gym.query.filter(Gym.id.in_(allowed_gym_ids)).order_by(Gym.name).all()
            if allowed_gym_ids else []
        )

    return gyms, is_super


def _build_admin_competitions_query(is_super):
    comps_query = Competition.query

    if not is_super:
        allowed_gym_ids = get_session_admin_gym_ids()
        if allowed_gym_ids:
            comps_query = comps_query.filter(Competition.gym_id.in_(allowed_gym_ids))
        else:
            comps_query = comps_query.filter(False)

    now = datetime.utcnow()

    start_is_null = case((Competition.start_at.is_(None), 1), else_=0)
    is_past = case((Competition.start_at < now, 1), else_=0)

    upcoming_sort = case(
        (Competition.start_at >= now, Competition.start_at),
        else_=None
    )
    past_sort = case(
        (Competition.start_at < now, Competition.start_at),
        else_=None
    )

    comps_query = comps_query.order_by(
        start_is_null.asc(),
        is_past.asc(),
        asc(upcoming_sort),
        desc(past_sort),
        Competition.created_at.desc() if hasattr(Competition, "created_at") else Competition.id.desc(),
    )

    return comps_query


def _parse_admin_comp_datetimes(start_date, start_time, end_date, end_time):
    melb_tz = ZoneInfo("Australia/Melbourne")

    def parse_dt(date_str, time_str):
        if not date_str:
            return None
        try:
            if not time_str:
                time_str = "00:00"
            local_naive = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
            local_aware = local_naive.replace(tzinfo=melb_tz)
            utc_aware = local_aware.astimezone(timezone.utc)
            return utc_aware.replace(tzinfo=None)
        except ValueError:
            return None

    start_at = parse_dt(start_date, start_time)
    end_at = parse_dt(end_date, end_time)
    return start_at, end_at


def _handle_create_comp_post(is_super):
    name = (request.form.get("name") or "").strip()
    slug_raw = (request.form.get("slug") or "").strip().lower()

    start_date = (request.form.get("start_date") or "").strip()
    start_time = (request.form.get("start_time") or "").strip()
    end_date = (request.form.get("end_date") or "").strip()
    end_time = (request.form.get("end_time") or "").strip()

    is_active_flag = bool(request.form.get("is_active"))

    if not name:
        return None, "Competition name is required."

    slug_val = slug_raw or slugify(name)
    existing_slug = Competition.query.filter_by(slug=slug_val).first()
    if existing_slug:
        slug_val = f"{slug_val}-{int(datetime.utcnow().timestamp())}"

    start_at, end_at = _parse_admin_comp_datetimes(start_date, start_time, end_date, end_time)

    gym_id_raw = (request.form.get("gym_id") or "").strip()
    if not gym_id_raw.isdigit():
        return None, "Please select a gym."

    gym_id = int(gym_id_raw)

    if not is_super:
        allowed = get_session_admin_gym_ids() or []
        if gym_id not in allowed:
            return None, "You are not allowed to create a competition for that gym."

    gym = Gym.query.get(gym_id)
    if not gym:
        return None, "Selected gym not found."

    comp = Competition(
        name=name,
        gym_name=gym.name if gym else None,
        gym=gym,
        slug=slug_val,
        start_at=start_at,
        end_at=end_at,
        is_active=is_active_flag,
    )
    db.session.add(comp)
    db.session.commit()

    if is_active_flag:
        all_comps = Competition.query.all()
        for c in all_comps:
            c.is_active = (c.id == comp.id)
        db.session.commit()

    return f"Competition '{comp.name}' created.", None


def _handle_manage_comp_post():
    action = (request.form.get("action") or "").strip()

    if action == "set_active":
        raw_id = (request.form.get("competition_id") or "").strip()
        if not raw_id.isdigit():
            return "Invalid competition id."

        cid = int(raw_id)
        comp = Competition.query.get(cid)
        if not comp:
            return "Competition not found."
        if not admin_can_manage_competition(comp):
            return "You are not allowed to manage this competition."

        all_comps = Competition.query.all()
        for c in all_comps:
            c.is_active = (c.id == comp.id)
        db.session.commit()
        return None

    if action == "archive":
        raw_id = (request.form.get("competition_id") or "").strip()
        if not raw_id.isdigit():
            return "Invalid competition id."

        cid = int(raw_id)
        comp = Competition.query.get(cid)
        if not comp:
            return "Competition not found."
        if not admin_can_manage_competition(comp):
            return "You are not allowed to manage this competition."

        comp.is_active = False
        db.session.commit()
        return None

    return "Unknown competition action."


@admin_bp.route("/admin", methods=["GET", "POST"])
def admin_page():
    guard = _require_admin_login()
    if guard:
        return guard

    gyms, _ = _get_admin_gyms_and_super()

    message = None
    error = None
    search_results = None
    search_query = ""
    lookup_competitor_id = ""
    doubles_lookup = None
    is_admin = True
    is_super = admin_is_super()

    current_comp = _resolve_admin_current_comp()

    if current_comp and not admin_can_manage_competition(current_comp):
        if session.get("admin_comp_id"):
            session.pop("admin_comp_id", None)
            error = "You don't have access to manage that competition. Please choose a different competition."
        current_comp = None

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()

        current_comp = _resolve_admin_current_comp()

        if current_comp and not admin_can_manage_competition(current_comp):
            if session.get("admin_comp_id"):
                session.pop("admin_comp_id", None)
            current_comp = None
            error = "You don't have access to manage that competition. Please choose a different competition."

        if action == "reset_all":
            if not is_super:
                abort(403)

            Score.query.delete()
            SectionClimb.query.delete()
            Competitor.query.delete()
            Section.query.delete()
            db.session.commit()
            invalidate_leaderboard_cache()
            message = "All competitors, scores, sections, and section climbs have been deleted."

        elif action == "update_competition":
            if not current_comp:
                error = "No competition selected."
            else:
                name = (request.form.get("name") or "").strip()
                gym_id_raw = (request.form.get("gym_id") or "").strip()

                start_date = (request.form.get("start_date") or "").strip()
                start_time = (request.form.get("start_time") or "").strip()
                end_date = (request.form.get("end_date") or "").strip()
                end_time = (request.form.get("end_time") or "").strip()

                if not name:
                    error = "Competition name is required."
                elif not gym_id_raw.isdigit():
                    error = "Please select a valid gym."
                else:
                    gym_id = int(gym_id_raw)

                    if not admin_is_super():
                        allowed = get_session_admin_gym_ids() or []
                        if gym_id not in allowed:
                            error = "You are not allowed to assign this gym."

                    gym = Gym.query.get(gym_id)

                    if not error and not gym:
                        error = "Gym not found."

                    if not error:
                        start_at, end_at = _parse_admin_comp_datetimes(
                            start_date, start_time, end_date, end_time
                        )

                        current_comp.name = name
                        current_comp.gym = gym
                        current_comp.gym_name = gym.name
                        current_comp.gym_id = gym.id
                        current_comp.start_at = start_at
                        current_comp.end_at = end_at

                        db.session.commit()
                        invalidate_leaderboard_cache()
                        message = f"Competition '{current_comp.name}' updated successfully."

        elif action == "delete_competitor":
            if not current_comp:
                error = "No competition selected. Go to Gym Admin → Manage Competition first."
            else:
                raw_id = request.form.get("competitor_id", "").strip()
                if not raw_id.isdigit():
                    error = "Please provide a valid competitor number."
                else:
                    cid = int(raw_id)
                    comp = Competitor.query.get(cid)
                    if not comp:
                        error = f"Competitor {cid} not found."
                    elif comp.competition_id != current_comp.id:
                        error = f"Competitor {cid} is not in the currently selected competition."
                    else:
                        Score.query.filter_by(competitor_id=cid).delete()
                        db.session.delete(comp)
                        db.session.commit()
                        invalidate_leaderboard_cache()
                        message = f"Competitor {cid} and their scores have been deleted."

        elif action == "create_competitor":
            if not current_comp:
                error = "No competition selected. Go to Gym Admin → Manage Competition first."
            else:
                name = request.form.get("new_name", "").strip()
                gender = request.form.get("new_gender", "Inclusive").strip()

                if not name:
                    error = "Competitor name is required."
                else:
                    if gender not in ("Male", "Female", "Inclusive", "Doubles"):
                        gender = "Inclusive"

                    comp = Competitor(
                        name=name,
                        gender=gender,
                        competition_id=current_comp.id,
                    )
                    db.session.add(comp)
                    db.session.commit()
                    invalidate_leaderboard_cache()
                    message = f"Competitor created: {comp.name} (#{comp.id}, {comp.gender})"

        elif action == "create_section":
            if not current_comp:
                error = "No competition selected. Go to Gym Admin → Manage Competition first."
            else:
                name = request.form.get("section_name", "").strip()
                if not name:
                    error = "Please provide a section name."
                else:
                    slug = slugify(name)

                    existing = (
                        Section.query
                        .filter_by(competition_id=current_comp.id, slug=slug)
                        .first()
                    )
                    if existing:
                        slug = f"{slug}-{int(datetime.utcnow().timestamp())}"

                    s = Section(
                        name=name,
                        slug=slug,
                        start_climb=0,
                        end_climb=0,
                        competition_id=current_comp.id,
                        gym_id=current_comp.gym_id,
                    )

                    db.session.add(s)
                    db.session.commit()
                    message = f"Section created: {name}. You can now add climbs via Edit."

        elif action == "search_competitor":
            if not current_comp:
                error = "No competition selected. Go to Gym Admin → Manage Competition first."
            else:
                search_query = request.form.get("search_name", "").strip()
                if not search_query:
                    error = "Please enter a name to search."
                else:
                    pattern = f"%{search_query}%"
                    search_results = (
                        Competitor.query
                        .filter(
                            Competitor.competition_id == current_comp.id,
                            Competitor.name.ilike(pattern),
                        )
                        .order_by(Competitor.name, Competitor.id)
                        .all()
                    )
                    if not search_results:
                        message = f"No competitors found matching '{search_query}'."

        elif action == "lookup_doubles_status":
            if not current_comp:
                error = "No competition selected. Go to Gym Admin → Manage Competition first."
            else:
                lookup_competitor_id = (request.form.get("lookup_competitor_id") or "").strip()

                if not lookup_competitor_id:
                    error = "Please enter a competitor number."
                elif not lookup_competitor_id.isdigit():
                    error = "Competitor number must be a whole number."
                else:
                    cid = int(lookup_competitor_id)

                    comp_row = (
                        Competitor.query
                        .filter(
                            Competitor.id == cid,
                            Competitor.competition_id == current_comp.id,
                        )
                        .first()
                    )

                    if not comp_row:
                        message = f"No competitor found for number {cid} in this competition."
                    else:
                        team = DoublesTeam.query.filter(
                            DoublesTeam.competition_id == current_comp.id,
                            (
                                (DoublesTeam.competitor_a_id == comp_row.id) |
                                (DoublesTeam.competitor_b_id == comp_row.id)
                            )
                        ).first()

                        partner = None
                        if team:
                            partner_id = (
                                team.competitor_b_id
                                if team.competitor_a_id == comp_row.id
                                else team.competitor_a_id
                            )
                            partner = (
                                Competitor.query
                                .filter(
                                    Competitor.id == partner_id,
                                    Competitor.competition_id == current_comp.id,
                                )
                                .first()
                            )

                        doubles_lookup = {
                            "id": comp_row.id,
                            "name": comp_row.name,
                            "email": comp_row.email,
                            "gender": comp_row.gender,
                            "in_team": bool(team),
                            "team_id": team.id if team else None,
                            "partner_id": partner.id if partner else None,
                            "partner_name": partner.name if partner else None,
                            "partner_email": partner.email if partner else None,
                        }

        else:
            error = "Unknown admin action."

    current_comp = _resolve_admin_current_comp()

    if current_comp and not admin_can_manage_competition(current_comp):
        if session.get("admin_comp_id"):
            session.pop("admin_comp_id", None)
        current_comp = None
        if not error:
            error = "You don't have access to manage that competition. Please choose a different competition."

    if current_comp:
        sections = (
            Section.query
            .filter(Section.competition_id == current_comp.id)
            .order_by(Section.name)
            .all()
        )
    else:
        sections = []

    return render_template(
        "admin.html",
        message=message,
        error=error,
        sections=sections,
        search_results=search_results,
        search_query=search_query,
        lookup_competitor_id=lookup_competitor_id,
        doubles_lookup=doubles_lookup,
        is_admin=is_admin,
        current_comp=current_comp,
        is_super=is_super,
        gyms=gyms,
    )


@admin_bp.route("/admin/comp/<slug>")
def admin_comp(slug):
    guard = _require_admin_login()
    if guard:
        return guard

    comp = Competition.query.filter_by(slug=slug).first_or_404()

    if not admin_can_manage_competition(comp):
        abort(403)

    session["admin_comp_id"] = comp.id

    sections = (
        Section.query
        .filter_by(competition_id=comp.id)
        .order_by(Section.name.asc())
        .all()
    )

    gyms, _ = _get_admin_gyms_and_super()

    return render_template(
        "admin.html",
        is_admin=True,
        current_comp=comp,
        sections=sections,
        search_results=None,
        search_query="",
        lookup_competitor_id="",
        doubles_lookup=None,
        message=None,
        error=None,
        is_super=admin_is_super(),
        gyms=gyms,
    )


@admin_bp.route("/admin/api/comp/<int:comp_id>/section-boundaries")
def admin_api_comp_section_boundaries(comp_id):
    guard = _require_admin_login()
    if guard:
        return guard

    comp = Competition.query.get_or_404(comp_id)
    if not admin_can_manage_competition(comp):
        return jsonify({"ok": False, "error": "Forbidden"}), 403

    sections = Section.query.filter(Section.competition_id == comp.id).all()

    out = {}
    for s in sections:
        pts = parse_boundary_points(s.boundary_points_json)
        out[str(s.id)] = pts

    return jsonify({"ok": True, "boundaries": out})


@admin_bp.route("/admin/section/<int:section_id>/edit", methods=["GET", "POST"])
def edit_section(section_id):
    guard = _require_admin_login()
    if guard:
        return guard

    section = Section.query.get_or_404(section_id)

    comp_id = None

    if request.method == "POST":
        raw = (request.form.get("comp_id") or "").strip()
        if raw.isdigit():
            comp_id = int(raw)
    else:
        comp_id = request.args.get("comp_id", type=int)

    if not comp_id:
        comp_id = session.get("admin_comp_id")

    if not comp_id:
        return redirect("/admin/comps/manage")

    current_comp = Competition.query.get(comp_id)
    if not current_comp:
        return redirect("/admin/comps/manage")

    session["admin_comp_id"] = current_comp.id

    if section.competition_id != current_comp.id:
        abort(404)

    if not admin_can_manage_competition(current_comp):
        abort(403)

    def _score_query_for_climb_number(climb_number: int):
        q = Score.query.filter(Score.climb_number == climb_number)
        if hasattr(Score, "competition_id"):
            q = q.filter(Score.competition_id == current_comp.id)
        if hasattr(Score, "gym_id") and current_comp.gym_id:
            q = q.filter(Score.gym_id == current_comp.gym_id)
        if hasattr(Score, "section_id"):
            q = q.filter(Score.section_id == section.id)
        return q

    def _delete_scores_for_climb_number(climb_number: int):
        _score_query_for_climb_number(climb_number).delete(synchronize_session=False)

    def _delete_scores_for_climb_numbers(climb_numbers: list[int]):
        if not climb_numbers:
            return
        q = Score.query.filter(Score.climb_number.in_(climb_numbers))
        if hasattr(Score, "competition_id"):
            q = q.filter(Score.competition_id == current_comp.id)
        if hasattr(Score, "gym_id") and current_comp.gym_id:
            q = q.filter(Score.gym_id == current_comp.gym_id)
        if hasattr(Score, "section_id"):
            q = q.filter(Score.section_id == section.id)
        q.delete(synchronize_session=False)

    error = None
    message = None
    failed_climb_id = None
    failed_values = {}

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()

        # DEBUG: log all form data on every POST
        print(f"[edit_section] action={action!r} form={dict(request.form)}", file=sys.stderr)

        if action == "save_section":
            name = request.form.get("name", "").strip()
            if not name:
                error = "Section name is required."
            else:
                section.name = name
                db.session.commit()
                invalidate_leaderboard_cache()
                message = "Section name updated."

        elif action == "update_climb":
            climb_id_raw = (request.form.get("climb_id") or "").strip()
            print(f"[edit_section] update_climb climb_id_raw={climb_id_raw!r}", file=sys.stderr)

            if not climb_id_raw.isdigit():
                error = "Invalid climb selection."
            else:
                sc = SectionClimb.query.get(int(climb_id_raw))

                if not sc or sc.section_id != section.id:
                    error = "Climb not found in this section."
                else:
                    if current_comp.gym_id and getattr(sc, "gym_id", None) and sc.gym_id != current_comp.gym_id:
                        abort(403)

                    climb_raw  = (request.form.get("climb_number") or "").strip()
                    colour     = (request.form.get("colour") or "").strip()
                    grade      = (request.form.get("grade") or "").strip()
                    styles     = request.form.getlist("styles")
                    base_raw   = (request.form.get("base_points") or "").strip()

                    valid_styles = {"balance", "power", "coordination"}
                    styles = [s for s in styles if s in valid_styles]

                    print(f"[edit_section] sc.id={sc.id} climb_raw={climb_raw!r} colour={colour!r} grade={grade!r} styles={styles} base_raw={base_raw!r}", file=sys.stderr)

                    def _capture_failed():
                        return {
                            "climb_number": climb_raw,
                            "colour":       colour,
                            "grade":        grade,
                            "styles":       styles,
                            "base_points":  base_raw,
                        }

                    if not climb_raw.isdigit():
                        error = "Please enter a valid climb number."
                        failed_climb_id = sc.id
                        failed_values = _capture_failed()
                    elif base_raw == "":
                        error = "Please enter base points."
                        failed_climb_id = sc.id
                        failed_values = _capture_failed()
                    elif not base_raw.lstrip("-").isdigit():
                        error = "Base points must be a whole number."
                        failed_climb_id = sc.id
                        failed_values = _capture_failed()
                    elif not colour:
                        error = "Please select a hold colour."
                        failed_climb_id = sc.id
                        failed_values = _capture_failed()
                    elif not grade:
                        error = "Please enter a grade."
                        failed_climb_id = sc.id
                        failed_values = _capture_failed()
                    elif not styles:
                        error = "Please select at least one style."
                        failed_climb_id = sc.id
                        failed_values = _capture_failed()
                    else:
                        new_climb_number = int(climb_raw)
                        new_base = int(base_raw)

                        if new_climb_number <= 0:
                            error = "Climb number must be positive."
                            failed_climb_id = sc.id
                            failed_values = _capture_failed()
                        elif new_base < 0:
                            error = "Base points must be ≥ 0."
                            failed_climb_id = sc.id
                            failed_values = _capture_failed()
                        else:
                            if new_climb_number != sc.climb_number:
                                dup_q = SectionClimb.query.filter_by(
                                    section_id=section.id,
                                    climb_number=new_climb_number,
                                )
                                if hasattr(SectionClimb, "gym_id") and current_comp.gym_id:
                                    dup_q = dup_q.filter(SectionClimb.gym_id == current_comp.gym_id)
                                dup = dup_q.first()

                                if dup:
                                    error = f"Climb {new_climb_number} is already in this section."
                                    failed_climb_id = sc.id
                                    failed_values = _capture_failed()
                                    print(f"[edit_section] DUPLICATE — failed_climb_id={failed_climb_id} failed_values={failed_values}", file=sys.stderr)
                                else:
                                    _score_query_for_climb_number(sc.climb_number).update(
                                        {Score.climb_number: new_climb_number},
                                        synchronize_session=False
                                    )
                                    sc.climb_number = new_climb_number

                            if not error:
                                sc.colour = colour
                                sc.grade = grade
                                sc.styles = styles
                                sc.base_points = new_base
                                db.session.commit()
                                invalidate_leaderboard_cache()
                                message = f"Climb {sc.climb_number} updated."

        elif action == "delete_climb":
            climb_id_raw = request.form.get("climb_id", "").strip()
            if not climb_id_raw.isdigit():
                error = "Invalid climb selection."
            else:
                climb_id = int(climb_id_raw)
                sc = SectionClimb.query.get(climb_id)

                if not sc or sc.section_id != section.id:
                    error = "Climb not found in this section."
                else:
                    if current_comp.gym_id and getattr(sc, "gym_id", None) and sc.gym_id != current_comp.gym_id:
                        abort(403)

                    climb_number = sc.climb_number
                    _delete_scores_for_climb_number(climb_number)

                    climbs_to_delete = (
                        db.session.query(SectionClimb.id)
                        .join(Section, SectionClimb.section_id == Section.id)
                        .filter(
                            Section.competition_id == current_comp.id,
                            SectionClimb.climb_number == climb_number,
                        )
                        .all()
                    )
                    ids = [c.id for c in climbs_to_delete]
                    if ids:
                        SectionClimb.query.filter(SectionClimb.id.in_(ids)).delete(synchronize_session=False)

                    db.session.commit()
                    invalidate_leaderboard_cache()
                    message = (
                        f"Climb {climb_number} removed from {section.name}, "
                        "and all associated scores were deleted."
                    )

        elif action == "delete_section":
            section_climbs_q = SectionClimb.query.filter_by(section_id=section.id)
            if hasattr(SectionClimb, "gym_id") and current_comp.gym_id:
                section_climbs_q = section_climbs_q.filter(SectionClimb.gym_id == current_comp.gym_id)

            section_climbs = section_climbs_q.all()
            climb_numbers = [sc.climb_number for sc in section_climbs]
            _delete_scores_for_climb_numbers(climb_numbers)

            delete_climbs_q = SectionClimb.query.filter_by(section_id=section.id)
            if hasattr(SectionClimb, "gym_id") and current_comp.gym_id:
                delete_climbs_q = delete_climbs_q.filter(SectionClimb.gym_id == current_comp.gym_id)
            delete_climbs_q.delete(synchronize_session=False)

            db.session.delete(section)
            db.session.commit()
            invalidate_leaderboard_cache()

            return redirect(f"/admin/comp/{current_comp.slug}" if current_comp.slug else "/admin/comps/manage")

        else:
            error = "Unknown action."

    # DEBUG: log what we're passing to the template
    print(f"[edit_section] RENDER failed_climb_id={failed_climb_id} failed_values={failed_values} error={error!r}", file=sys.stderr)

    climbs_q = SectionClimb.query.filter_by(section_id=section.id)
    if hasattr(SectionClimb, "gym_id") and current_comp.gym_id:
        climbs_q = climbs_q.filter(SectionClimb.gym_id == current_comp.gym_id)

    climbs = climbs_q.order_by(SectionClimb.climb_number).all()

    section_boundary_points = parse_boundary_points(section.boundary_points_json)

    climb_points_json = []
    for c in climbs:
        if getattr(c, "x_percent", None) is not None and getattr(c, "y_percent", None) is not None:
            climb_points_json.append({
                "id": c.id,
                "climb_number": c.climb_number,
                "x": float(c.x_percent),
                "y": float(c.y_percent),
            })

    gym_map_url = current_comp.gym.map_image_path if current_comp.gym else None

    return render_template(
        "admin_section_edit.html",
        section=section,
        climbs=climbs,
        error=error,
        message=message,
        current_comp=current_comp,
        current_comp_id=current_comp.id,
        gym_map_url=gym_map_url,
        section_boundary_points=section_boundary_points,
        climb_points_json=climb_points_json,
        failed_climb_id=failed_climb_id,
        failed_values=failed_values,
    )


@admin_bp.route("/admin/map")
def admin_map():
    guard = _require_admin_login()
    if guard:
        return guard

    comp_id = request.args.get("comp_id", type=int)

    if not comp_id:
        comp_id = session.get("admin_comp_id")

    if not comp_id:
        flash(
            "Pick a competition first (Gym Admin → Manage Competition) before opening the map editor.",
            "warning",
        )
        return redirect("/admin/comps/manage")

    current_comp = Competition.query.get(comp_id)

    if not current_comp:
        session.pop("admin_comp_id", None)
        flash(
            "That competition no longer exists (or your session is stale). Please choose a competition to manage.",
            "warning",
        )
        return redirect("/admin/comps/manage")

    if request.args.get("comp_id", type=int) != current_comp.id:
        return redirect(f"/admin/map?comp_id={current_comp.id}")

    if not admin_can_manage_competition(current_comp):
        abort(403)

    session["admin_comp_id"] = current_comp.id

    gym_map_url = current_comp.gym.map_image_path if current_comp.gym else None

    sections = (
        Section.query
        .filter(Section.competition_id == current_comp.id)
        .order_by(Section.name)
        .all()
    )

    section_ids = [s.id for s in sections]
    climbs = []
    if section_ids:
        q = SectionClimb.query.filter(SectionClimb.section_id.in_(section_ids))
        if current_comp.gym_id is not None:
            q = q.filter(SectionClimb.gym_id == current_comp.gym_id)
        climbs = q.all()

    gym_name = current_comp.gym.name if getattr(current_comp, "gym", None) else None
    comp_name = current_comp.name

    return render_template(
        "admin_map.html",
        sections=sections,
        climbs=climbs,
        gym_map_url=gym_map_url,
        gym_name=gym_name,
        comp_name=comp_name,
        current_comp_id=current_comp.id,
        current_comp=current_comp,
    )


@admin_bp.route("/admin/comps")
def admin_competitions_redirect():
    guard = _require_admin_login()
    if guard:
        return guard
    return redirect("/admin/comps/manage")


@admin_bp.route("/admin/comps/create", methods=["GET", "POST"])
def admin_competitions_create():
    guard = _require_admin_login()
    if guard:
        return guard

    gyms, is_super = _get_admin_gyms_and_super()
    message = None
    error = None

    if request.method == "POST":
        message, error = _handle_create_comp_post(is_super)
        if not error:
            return redirect("/admin/comps/manage")

    return render_template(
        "admin_create_comp.html",
        message=message,
        gyms=gyms,
        error=error,
        is_super=is_super,
    )


@admin_bp.route("/admin/comps/manage", methods=["GET", "POST"])
def admin_competitions_manage():
    guard = _require_admin_login()
    if guard:
        return guard

    gyms, is_super = _get_admin_gyms_and_super()
    message = None
    error = None

    page = request.args.get("page", 1, type=int)
    per_page = 10

    if request.method == "POST":
        error = _handle_manage_comp_post()
        return redirect(f"/admin/comps/manage?page={page}")

    comps_query = _build_admin_competitions_query(is_super)
    competitions_page = comps_query.paginate(page=page, per_page=per_page, error_out=False)

    return render_template(
        "admin_competitions.html",
        competitions=competitions_page.items,
        competitions_page=competitions_page,
        message=message,
        gyms=gyms,
        error=error,
        is_super=is_super,
    )


@admin_bp.route("/route-setter/comps")
def route_setter_competitions():
    guard = _require_admin_login()
    if guard:
        return guard

    is_super = admin_is_super()

    comps_query = Competition.query

    if not is_super:
        allowed_gym_ids = get_session_admin_gym_ids() or []
        if allowed_gym_ids:
            comps_query = comps_query.filter(Competition.gym_id.in_(allowed_gym_ids))
        else:
            comps_query = comps_query.filter(False)

    competitions = comps_query.order_by(Competition.start_at.desc()).all()

    return render_template(
        "route_setter_competitions.html",
        competitions=competitions,
    )


@admin_bp.route("/route-setter/leaderboards")
def route_setter_leaderboards():
    guard = _require_admin_login()
    if guard:
        return guard

    is_super = admin_is_super()

    comps_query = Competition.query

    if not is_super:
        allowed_gym_ids = get_session_admin_gym_ids() or []
        if allowed_gym_ids:
            comps_query = comps_query.filter(Competition.gym_id.in_(allowed_gym_ids))
        else:
            comps_query = comps_query.filter(False)

    competitions = comps_query.order_by(Competition.start_at.desc()).all()

    return render_template(
        "route_setter_leaderboards.html",
        competitions=competitions,
    )


@admin_bp.route("/admin/comp/<int:competition_id>/configure")
def admin_configure_competition(competition_id):
    guard = _require_admin_login()
    if guard:
        return guard

    comp = Competition.query.get_or_404(competition_id)

    if not admin_can_manage_competition(comp):
        abort(403)

    session["admin_comp_id"] = comp.id

    print(
        f"[ADMIN CONFIGURE] Now editing competition #{comp.id} – {comp.name}",
        file=sys.stderr,
    )

    return redirect("/admin")


@admin_bp.route("/admin/map/add-climb", methods=["POST"])
def admin_map_add_climb():
    """
    Handle form submission from the map when admin clicks and adds a climb.
    On success: redirect so the page refreshes and the new dot appears.
    On error: re-render the map page with form values preserved — no data loss.
    """
    guard = _require_admin_login()
    if guard:
        return guard

    def _render_map_error(error_msg, current_comp, form_values):
        """Re-render the map page with an error and all form values intact."""
        sections = (
            Section.query
            .filter(Section.competition_id == current_comp.id)
            .order_by(Section.name)
            .all()
        )
        section_ids = [s.id for s in sections]
        climbs = []
        if section_ids:
            q = SectionClimb.query.filter(SectionClimb.section_id.in_(section_ids))
            if current_comp.gym_id is not None:
                q = q.filter(SectionClimb.gym_id == current_comp.gym_id)
            climbs = q.all()
        gym_map_url = current_comp.gym.map_image_path if current_comp.gym else None
        gym_name = current_comp.gym.name if getattr(current_comp, "gym", None) else None
        return render_template(
            "admin_map.html",
            sections=sections,
            climbs=climbs,
            gym_map_url=gym_map_url,
            gym_name=gym_name,
            comp_name=current_comp.name,
            current_comp_id=current_comp.id,
            current_comp=current_comp,
            map_error=error_msg,
            form_values=form_values,
        )

    comp_id_raw = (request.form.get("comp_id") or "").strip()
    comp_id = int(comp_id_raw) if comp_id_raw.isdigit() else session.get("admin_comp_id")

    if not comp_id:
        flash("No competition context. Open the map from Gym Admin → Manage Competition.", "warning")
        return redirect("/admin/comps/manage")

    current_comp = Competition.query.get(comp_id)
    if not current_comp:
        flash("Competition not found.", "warning")
        return redirect("/admin/comps/manage")

    session["admin_comp_id"] = current_comp.id

    if not admin_can_manage_competition(current_comp):
        abort(403)

    # Capture all form values upfront so we can pass them back on any error
    section_id_raw   = (request.form.get("section_id") or "").strip()
    new_section_name = (request.form.get("new_section_name") or "").strip()
    climb_raw        = (request.form.get("climb_number") or "").strip()
    colour           = (request.form.get("colour") or "").strip()
    grade            = (request.form.get("grade") or "").strip()
    styles           = request.form.getlist("styles")
    base_raw         = (request.form.get("base_points") or "").strip()
    x_raw            = (request.form.get("x_percent") or "").strip()
    y_raw            = (request.form.get("y_percent") or "").strip()

    valid_styles = {"balance", "power", "coordination"}
    styles = [s for s in styles if s in valid_styles]

    form_values = {
        "section_id":       section_id_raw,
        "new_section_name": new_section_name,
        "climb_number":     climb_raw,
        "colour":           colour,
        "grade":            grade,
        "styles":           styles,
        "base_points":      base_raw,
        "x_percent":        x_raw,
        "y_percent":        y_raw,
    }

    # Resolve section
    section = None
    if section_id_raw.isdigit():
        section = Section.query.get(int(section_id_raw))
        if not section or section.competition_id != current_comp.id:
            section = None

    if not section and new_section_name:
        slug = slugify(new_section_name)
        existing = Section.query.filter_by(competition_id=current_comp.id, slug=slug).first()
        if existing:
            slug = f"{slug}-{int(datetime.utcnow().timestamp())}"
        section = Section(
            name=new_section_name,
            slug=slug,
            start_climb=0,
            end_climb=0,
            competition_id=current_comp.id,
            gym_id=current_comp.gym_id,
        )
        db.session.add(section)
        db.session.flush()

    if not section:
        db.session.rollback()
        return _render_map_error("Please select an existing section or type a new section name.", current_comp, form_values)

    if not climb_raw.isdigit():
        db.session.rollback()
        return _render_map_error("Climb number must be a whole number.", current_comp, form_values)

    if base_raw == "":
        db.session.rollback()
        return _render_map_error("Base points are required.", current_comp, form_values)

    if not base_raw.lstrip("-").isdigit():
        db.session.rollback()
        return _render_map_error("Base points must be a whole number.", current_comp, form_values)

    if not colour:
        db.session.rollback()
        return _render_map_error("Hold colour is required.", current_comp, form_values)

    if not grade:
        db.session.rollback()
        return _render_map_error("Grade is required.", current_comp, form_values)

    if not styles:
        db.session.rollback()
        return _render_map_error("Please select at least one style.", current_comp, form_values)

    climb_number = int(climb_raw)
    base_points  = int(base_raw)

    if climb_number <= 0:
        db.session.rollback()
        return _render_map_error("Climb number must be positive.", current_comp, form_values)

    if base_points < 0:
        db.session.rollback()
        return _render_map_error("Base points must be ≥ 0.", current_comp, form_values)

    try:
        x_percent = float(x_raw)
        y_percent = float(y_raw)
    except ValueError:
        db.session.rollback()
        return _render_map_error("You need to tap the map first to set a position.", current_comp, form_values)

    conflict = (
        db.session.query(SectionClimb)
        .join(Section, SectionClimb.section_id == Section.id)
        .filter(
            Section.competition_id == current_comp.id,
            SectionClimb.climb_number == climb_number,
        )
        .first()
    )
    if conflict:
        db.session.rollback()
        return _render_map_error(f"Climb #{climb_number} already exists in this competition.", current_comp, form_values)

    sc = SectionClimb(
        section_id=section.id,
        gym_id=current_comp.gym_id,
        climb_number=climb_number,
        colour=colour,
        grade=grade,
        styles=styles,
        base_points=base_points,
        x_percent=x_percent,
        y_percent=y_percent,
    )
    db.session.add(sc)

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return _render_map_error(f"DB error saving climb: {e}", current_comp, form_values)

    # Success — redirect so page refreshes and new dot appears on the map
    return redirect(f"/admin/map?comp_id={current_comp.id}")


@admin_bp.route("/admin/map/save-boundary", methods=["POST"])
def admin_map_save_boundary():
    guard = _require_admin_login()
    if guard:
        return guard

    data = request.get_json(silent=True) or request.form.to_dict(flat=True)

    comp_id_raw = (data.get("comp_id") or "").strip()
    section_id_raw = (data.get("section_id") or "").strip()
    points_raw = data.get("points")

    if not comp_id_raw.isdigit() or not section_id_raw.isdigit():
        return jsonify({"ok": False, "error": "Missing comp_id or section_id"}), 400

    comp_id = int(comp_id_raw)
    section_id = int(section_id_raw)

    current_comp = Competition.query.get(comp_id)
    if not current_comp:
        return jsonify({"ok": False, "error": "Competition not found"}), 404

    if not admin_can_manage_competition(current_comp):
        return jsonify({"ok": False, "error": "Forbidden"}), 403

    section = Section.query.get(section_id)
    if not section or section.competition_id != current_comp.id:
        return jsonify({"ok": False, "error": "Section not found in this comp"}), 404

    points = parse_boundary_points(points_raw)

    if points and len(points) < 3:
        return jsonify({"ok": False, "error": "Polygon needs at least 3 points"}), 400

    section.boundary_points_json = boundary_to_json(points) if points else None
    db.session.commit()

    return jsonify({"ok": True, "section_id": section.id, "points": points})


@admin_bp.app_context_processor
def inject_sidebar_admin_context():
    sidebar_admin_comp = None
    sidebar_gym_admin_allowed = False
    sidebar_route_setter_allowed = False

    account_id = session.get("account_id")
    admin_comp_id = session.get("admin_comp_id")

    if account_id:
        gym_admin_row = GymAdmin.query.filter_by(account_id=account_id).first()

        if gym_admin_row:
            sidebar_gym_admin_allowed = True
            sidebar_route_setter_allowed = True

            if admin_comp_id:
                comp = Competition.query.get(admin_comp_id)
                if comp and admin_can_manage_competition(comp):
                    sidebar_admin_comp = comp

    return {
        "sidebar_admin_comp": sidebar_admin_comp,
        "sidebar_gym_admin_allowed": sidebar_gym_admin_allowed,
        "sidebar_route_setter_allowed": sidebar_route_setter_allowed,
    }