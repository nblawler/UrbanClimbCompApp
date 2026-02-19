from flask import Blueprint, render_template, request, session, jsonify, abort, redirect, flash
from datetime import datetime
import sys
import os

from app.extensions import db
from app.models import Competition, Competitor, Score, Section, SectionClimb, Gym
from app.helpers.competition import get_current_comp
from app.helpers.leaderboard_cache import invalidate_leaderboard_cache
from app.helpers.leaderboard import admin_can_manage_competition, admin_is_super, _parse_boundary_points, _boundary_to_json, get_session_admin_gym_ids, slugify

admin_bp = Blueprint("admin", __name__)

@admin_bp.route("/admin", methods=["GET", "POST"])
def admin_page():
    message = None
    error = None
    search_results = None
    search_query = ""
    is_admin = session.get("admin_ok", False)

    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "letmein123")

    def resolve_admin_current_comp():
        """
        Admin pages should prefer the admin-selected competition context (session['admin_comp_id']).
        Only fall back to get_current_comp() if no admin context exists.
        """
        admin_comp_id = session.get("admin_comp_id")
        if admin_comp_id:
            comp = Competition.query.get(admin_comp_id)
            if comp:
                return comp
            # stale session value
            session.pop("admin_comp_id", None)
        return get_current_comp()

    # If an admin_comp_id is set, ensure this admin can manage it.
    # If not, clear it to prevent confusing 403 loops.
    current_comp = resolve_admin_current_comp()
    if current_comp and session.get("admin_comp_id"):
        if not admin_can_manage_competition(current_comp):
            session.pop("admin_comp_id", None)
            current_comp = None
            error = "You don't have access to manage that competition. Please choose a different competition."

    if request.method == "POST":
        action = request.form.get("action")

        # Handle login separately
        if action == "login":
            password = request.form.get("password", "")
            if password != ADMIN_PASSWORD:
                error = "Incorrect admin password."
            else:
                # Password-based admin = super admin
                session["admin_ok"] = True
                session["admin_is_super"] = True
                session["admin_gym_ids"] = None

                # IMPORTANT: don't keep stale editing context from past sessions
                session.pop("admin_comp_id", None)

                is_admin = True
                message = "Admin access granted."

        else:
            # For all other actions, require that admin has been unlocked
            if not is_admin:
                error = "Please enter the admin password first."
            else:
                # Always resolve comp AFTER login and before handling actions
                current_comp = resolve_admin_current_comp()

                if action == "reset_all":
                    # Super-admin only (server-side enforcement)
                    if not admin_is_super():
                        abort(403)

                    # For this action, require password *again* each time
                    password = request.form.get("password", "")
                    if password != ADMIN_PASSWORD:
                        error = "Incorrect admin password."
                    else:
                        # Delete scores, section climbs, competitors, sections
                        Score.query.delete()
                        SectionClimb.query.delete()
                        Competitor.query.delete()
                        Section.query.delete()
                        db.session.commit()
                        invalidate_leaderboard_cache()
                        message = "All competitors, scores, sections, and section climbs have been deleted."

                elif action == "delete_competitor":
                    # Require a selected admin comp to avoid deleting cross-comp records
                    if not current_comp:
                        error = "No competition selected. Go to Admin → Comps and click Manage first."
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
                        error = "No competition selected. Go to Admin → Comps and click Manage first."
                    else:
                        name = request.form.get("new_name", "").strip()
                        gender = request.form.get("new_gender", "Inclusive").strip()

                        if not name:
                            error = "Competitor name is required."
                        else:
                            if gender not in ("Male", "Female", "Inclusive"):
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
                        error = "No competition selected. Go to Admin → Comps and click Manage first."
                    else:
                        name = request.form.get("section_name", "").strip()
                        if not name:
                            error = "Please provide a section name."
                        else:
                            slug = slugify(name)

                            # scoped duplicate check (competition_id + slug)
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
                        error = "No competition selected. Go to Admin → Comps and click Manage first."
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

    # Always resolve current_comp AFTER any POST actions too
    current_comp = resolve_admin_current_comp()

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
        is_admin=is_admin,
        current_comp=current_comp,
    )


@admin_bp.route("/admin/comp/<slug>")
def admin_comp(slug):
    """
    Per-competition admin dashboard.

    - Requires admin session (session["admin_ok"] = True)
    - Looks up the competition by slug
    - Loads sections for that competition
    - Reuses the existing admin.html template
    """
    # Make sure only admins can see this
    if not session.get("admin_ok"):
        return redirect("/login")

    # Find the competition or 404 if the slug is invalid
    comp = Competition.query.filter_by(slug=slug).first_or_404()

    # Gym-level permission check
    if not admin_can_manage_competition(comp):
        abort(403)
        
    session["admin_comp_id"] = comp.id

    # Load sections for this competition (if Section has competition_id)
    sections = (
        Section.query
        .filter_by(competition_id=comp.id)
        .order_by(Section.name.asc())
        .all()
    )

    # Reuse admin.html but now with a specific competition in context
    return render_template(
        "admin.html",
        is_admin=True,
        competition=comp,
        sections=sections,
    )
    
@admin_bp.route("/admin/api/comp/<int:comp_id>/section-boundaries")
def admin_api_comp_section_boundaries(comp_id):
    """
    Admin-only: return boundaries for all sections in this comp.
    Works even if the comp isn't live.
    """
    if not session.get("admin_ok"):
        return jsonify({"ok": False, "error": "Not admin"}), 403

    comp = Competition.query.get_or_404(comp_id)
    if not admin_can_manage_competition(comp):
        return jsonify({"ok": False, "error": "Forbidden"}), 403

    sections = Section.query.filter(Section.competition_id == comp.id).all()

    out = {}
    for s in sections:
        pts = _parse_boundary_points(s.boundary_points_json)
        out[str(s.id)] = pts  # include empty list too (useful for UI)

    return jsonify({"ok": True, "boundaries": out})



@admin_bp.route("/admin/section/<int:section_id>/edit", methods=["GET", "POST"])
def edit_section(section_id):
    # Require an unlocked admin session
    if not session.get("admin_ok"):
        return redirect("/admin")

    section = Section.query.get_or_404(section_id)

    # Admin-selected comp context:
    # - On GET: comp_id from querystring
    # - On POST: comp_id from hidden form field
    # - Fallback: session["admin_comp_id"]
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
        return redirect("/admin/comps")

    current_comp = Competition.query.get(comp_id)
    if not current_comp:
        return redirect("/admin/comps")

    # Keep session in sync (so other admin routes stay consistent)
    session["admin_comp_id"] = current_comp.id

    # Ensure this section belongs to the comp we're editing
    if section.competition_id != current_comp.id:
        abort(404)

    # Gym-level permission check (super admin OR gym admin for this gym)
    if not admin_can_manage_competition(current_comp):
        abort(403)

    # Helper: delete scores safely (scoped to this comp/gym/section when possible)
    def _delete_scores_for_climb_number(climb_number: int):
        q = Score.query.filter(Score.climb_number == climb_number)

        # Scope to competition if the column exists
        if hasattr(Score, "competition_id"):
            q = q.filter(Score.competition_id == current_comp.id)

        # Scope to gym if the column exists
        if hasattr(Score, "gym_id") and current_comp.gym_id:
            q = q.filter(Score.gym_id == current_comp.gym_id)

        # Scope to section if the column exists
        if hasattr(Score, "section_id"):
            q = q.filter(Score.section_id == section.id)

        q.delete(synchronize_session=False)

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

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()

        if action == "save_section":
            name = request.form.get("name", "").strip()
            if not name:
                error = "Section name is required."
            else:
                section.name = name
                db.session.commit()
                invalidate_leaderboard_cache()
                message = "Section name updated."

        elif action == "add_climb":
            climb_raw = request.form.get("climb_number", "").strip()
            colour = request.form.get("colour", "").strip()

            base_raw = request.form.get("base_points", "").strip()
            penalty_raw = request.form.get("penalty_per_attempt", "").strip()
            cap_raw = request.form.get("attempt_cap", "").strip()

            # basic validation
            if not climb_raw.isdigit():
                error = "Please enter a valid climb number."
            elif base_raw == "" or penalty_raw == "" or cap_raw == "":
                error = "Please enter base points, penalty per attempt, and attempt cap."
            elif not (
                base_raw.lstrip("-").isdigit()
                and penalty_raw.lstrip("-").isdigit()
                and cap_raw.lstrip("-").isdigit()
            ):
                error = "Base points, penalty per attempt, and attempt cap must be whole numbers."
            else:
                climb_number = int(climb_raw)
                base_points = int(base_raw)
                penalty_per_attempt = int(penalty_raw)
                attempt_cap = int(cap_raw)

                if climb_number <= 0:
                    error = "Climb number must be positive."
                elif base_points < 0 or penalty_per_attempt < 0 or attempt_cap <= 0:
                    error = "Base points and penalty must be ≥ 0 and attempt cap must be > 0."
                else:
                    # Uniqueness check should include gym_id (gym separation)
                    existing = (
                        SectionClimb.query
                        .filter_by(
                            section_id=section.id,
                            climb_number=climb_number,
                            gym_id=current_comp.gym_id,
                        )
                        .first()
                    )

                    if existing:
                        error = f"Climb {climb_number} is already in this section."
                    else:
                        sc = SectionClimb(
                            section_id=section.id,
                            gym_id=current_comp.gym_id,
                            climb_number=climb_number,
                            colour=colour or None,
                            base_points=base_points,
                            penalty_per_attempt=penalty_per_attempt,
                            attempt_cap=attempt_cap,
                        )
                        db.session.add(sc)
                        db.session.commit()
                        invalidate_leaderboard_cache()
                        message = f"Climb {climb_number} added to {section.name}."

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
                    # Extra safety: ensure you're not deleting across gyms
                    if current_comp.gym_id and getattr(sc, "gym_id", None) and sc.gym_id != current_comp.gym_id:
                        abort(403)

                    # Delete scores for this climb number, scoped to this comp/gym/section when possible
                    _delete_scores_for_climb_number(sc.climb_number)

                    # Then delete the climb config itself
                    db.session.delete(sc)
                    db.session.commit()
                    invalidate_leaderboard_cache()
                    message = (
                        f"Climb {sc.climb_number} removed from {section.name}, "
                        "and all associated scores were deleted."
                    )

        elif action == "delete_section":
            # Find all climb numbers in this section (and gym, if applicable)
            section_climbs_q = SectionClimb.query.filter_by(section_id=section.id)
            if hasattr(SectionClimb, "gym_id") and current_comp.gym_id:
                section_climbs_q = section_climbs_q.filter(SectionClimb.gym_id == current_comp.gym_id)

            section_climbs = section_climbs_q.all()
            climb_numbers = [sc.climb_number for sc in section_climbs]

            # Delete all scores for those climbs (scoped where possible)
            _delete_scores_for_climb_numbers(climb_numbers)

            # Delete the section's climbs (scoped where possible)
            delete_climbs_q = SectionClimb.query.filter_by(section_id=section.id)
            if hasattr(SectionClimb, "gym_id") and current_comp.gym_id:
                delete_climbs_q = delete_climbs_q.filter(SectionClimb.gym_id == current_comp.gym_id)
            delete_climbs_q.delete(synchronize_session=False)

            # Delete the section itself
            db.session.delete(section)
            db.session.commit()
            invalidate_leaderboard_cache()

            # Keep comp context on redirect
            return redirect(f"/admin/comp/{current_comp.slug}" if current_comp.slug else "/admin/comps")

        else:
            error = "Unknown action."

    climbs_q = SectionClimb.query.filter_by(section_id=section.id)
    if hasattr(SectionClimb, "gym_id") and current_comp.gym_id:
        climbs_q = climbs_q.filter(SectionClimb.gym_id == current_comp.gym_id)

    climbs = climbs_q.order_by(SectionClimb.climb_number).all()

    return render_template(
        "admin_section_edit.html",
        section=section,
        climbs=climbs,
        error=error,
        message=message,
        current_comp=current_comp,
        current_comp_id=current_comp.id,
    )


@admin_bp.route("/admin/map")
def admin_map():
    """
    Map-based climb creation/edit view.
    Admin can click the gym map, then fill climb config and save.
    Loads the *admin-selected* competition (not the public active comp).
    """
    if not session.get("admin_ok"):
        return redirect("/admin")

    # 1) Prefer explicit comp_id in querystring
    comp_id = request.args.get("comp_id", type=int)

    # 2) Fallback to session "admin currently editing" comp
    if not comp_id:
        comp_id = session.get("admin_comp_id")

    # If still no comp context, bounce to comps list (never use public current comp)
    if not comp_id:
        flash(
            "Pick a competition first (Admin → Comps → Manage) before opening the map editor.",
            "warning",
        )
        return redirect("/admin/comps")

    current_comp = Competition.query.get(comp_id)

    # Stale/invalid comp_id in session or URL
    if not current_comp:
        session.pop("admin_comp_id", None)
        flash(
            "That competition no longer exists (or your session is stale). Please choose a competition to manage.",
            "warning",
        )
        return redirect("/admin/comps")

    # Canonicalize URL so refresh/back/share works reliably
    # (If they arrived via /admin/map without comp_id, redirect to include it.)
    if request.args.get("comp_id", type=int) != current_comp.id:
        return redirect(f"/admin/map?comp_id={current_comp.id}")

    # Only allow admins who can manage this competition's gym
    if not admin_can_manage_competition(current_comp):
        abort(403)

    # Keep session in sync so POST can always fall back safely
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

        # Keep gym_id filter only if SectionClimb actually stores gym_id correctly.
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
        current_comp_id=current_comp.id,  # template uses this for hidden input + links
    )



@admin_bp.route("/admin/comps", methods=["GET", "POST"])
def admin_competitions():
    """
    Admin UI to manage competitions:
    - List all competitions (filtered by gym access for gym admins)
    - Create a new competition:
        - super admin: can create for any gym
        - gym admin: can create only for gyms they manage
    - Set a competition as the single active comp
    - Archive (deactivate) a competition
    """
    if not session.get("admin_ok"):
        return redirect("/admin")
    
    if admin_is_super():
        gyms = Gym.query.order_by(Gym.name).all()
    else:
        allowed_gym_ids = get_session_admin_gym_ids()
        if allowed_gym_ids:
            gyms = (
                Gym.query
                .filter(Gym.id.in_(allowed_gym_ids))
                .order_by(Gym.name)
                .all()
            )
        else:
            gyms = []

    message = None
    error = None
    is_super = admin_is_super()
    

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()

        if action == "create_comp":
            name = (request.form.get("name") or "").strip()
            slug_raw = (request.form.get("slug") or "").strip().lower()

            start_date = (request.form.get("start_date") or "").strip()
            start_time = (request.form.get("start_time") or "").strip()
            end_date = (request.form.get("end_date") or "").strip()
            end_time = (request.form.get("end_time") or "").strip()

            is_active_flag = bool(request.form.get("is_active"))

            if not name:
                error = "Competition name is required."
            else:
                # slug: either provided or derived from name
                slug_val = slug_raw or slugify(name)
                existing_slug = Competition.query.filter_by(slug=slug_val).first()
                if existing_slug:
                    # ensure uniqueness by timestamp suffix
                    slug_val = f"{slug_val}-{int(datetime.utcnow().timestamp())}"

                def parse_dt(date_str, time_str):
                    if not date_str:
                        return None
                    try:
                        if not time_str:
                            return datetime.strptime(date_str, "%Y-%m-%d")
                        return datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
                    except ValueError:
                        return None

                start_at = parse_dt(start_date, start_time)
                end_at = parse_dt(end_date, end_time)

                # gym selection must come from gym_id (dropdown)
                gym_id_raw = (request.form.get("gym_id") or "").strip()
                gym = None

                if not gym_id_raw.isdigit():
                    error = "Please select a gym."
                else:
                    gym_id = int(gym_id_raw)

                    if not is_super:
                        # Gym admins can only create comps for their allowed gyms
                        allowed = get_session_admin_gym_ids()
                        if gym_id not in allowed:
                            error = "You are not allowed to create a competition for that gym."

                    if not error:
                        gym = Gym.query.get(gym_id)
                        if not gym:
                            error = "Selected gym not found."

                if not error:
                    comp = Competition(
                        name=name,
                        gym_name=gym.name if gym else None,  # legacy text field (optional)
                        gym=gym,                              # formal relationship
                        slug=slug_val,
                        start_at=start_at,
                        end_at=end_at,
                        is_active=is_active_flag,
                    )
                    db.session.add(comp)
                    db.session.commit()

                    # If this new comp is active, make all others inactive
                    if is_active_flag:
                        all_comps = Competition.query.all()
                        for c in all_comps:
                            c.is_active = (c.id == comp.id)
                        db.session.commit()

                    message = f"Competition '{comp.name}' created."

        elif action == "set_active":
            raw_id = (request.form.get("competition_id") or "").strip()
            if not raw_id.isdigit():
                error = "Invalid competition id."
            else:
                cid = int(raw_id)
                comp = Competition.query.get(cid)
                if not comp:
                    error = "Competition not found."
                elif not admin_can_manage_competition(comp):
                    error = "You are not allowed to manage this competition."
                else:
                    all_comps = Competition.query.all()
                    for c in all_comps:
                        c.is_active = (c.id == comp.id)
                    db.session.commit()
                    message = f"'{comp.name}' is now the active competition."

        elif action == "archive":
            raw_id = (request.form.get("competition_id") or "").strip()
            if not raw_id.isdigit():
                error = "Invalid competition id."
            else:
                cid = int(raw_id)
                comp = Competition.query.get(cid)
                if not comp:
                    error = "Competition not found."
                elif not admin_can_manage_competition(comp):
                    error = "You are not allowed to manage this competition."
                else:
                    comp.is_active = False
                    db.session.commit()
                    message = f"Competition '{comp.name}' has been archived (inactive)."

    # Always show current list, but filter for gym admins
    comps_query = Competition.query
    if not admin_is_super():
        allowed_gym_ids = get_session_admin_gym_ids()
        if allowed_gym_ids:
            comps_query = comps_query.filter(Competition.gym_id.in_(allowed_gym_ids))
        else:
            comps_query = comps_query.filter(False)  # no comps

    comps = (
        comps_query
        .order_by(Competition.start_at.asc(), Competition.created_at.asc())
        .all()
    )

    return render_template(
        "admin_comps.html",
        competitions=comps,
        message=message,
        gyms=gyms,
        error=error,
    )


@admin_bp.route("/admin/comp/<int:competition_id>/configure")
def admin_configure_competition(competition_id):
    """
    Set this competition as the active one for editing
    and then send the admin to the main admin page where
    they can manage sections, climbs, map, etc.
    """
    if not session.get("admin_ok"):
        return redirect("/admin")

    comp = Competition.query.get_or_404(competition_id)

    # Only allow admins who can manage this gym
    if not admin_can_manage_competition(comp):
        abort(403)

    # Persist admin editing context so /admin, /admin/map, etc. know which comp you're editing
    session["admin_comp_id"] = comp.id

    # Make this the active competition (editing context)
    all_comps = Competition.query.all()

    for c in all_comps:
        c.is_active = (c.id == comp.id)
    db.session.commit()

    print(
        f"[ADMIN CONFIGURE] Now editing competition #{comp.id} – {comp.name}",
        file=sys.stderr,
    )

    # Send them to the existing admin hub where they can create sections, climbs, etc.
    return redirect("/admin")

@admin_bp.route("/admin/map/add-climb", methods=["POST"])
def admin_map_add_climb():
    """
    Handle form submission from the map when admin clicks and adds a climb.
    Uses the admin-selected competition context (comp_id), NOT the public "current" comp.
    """
    # Debug: if this fires, the session cookie isn't present or SECRET_KEY mismatch
    if not session.get("admin_ok"):
        print("[ADMIN MAP ADD] admin_ok missing in session. session keys:", list(session.keys()), file=sys.stderr)
        flash("Admin session missing — please log in again.", "warning")
        return redirect("/admin")

    def back(comp_id=None):
        return redirect(f"/admin/map?comp_id={comp_id}") if comp_id else redirect("/admin/map")

    # 1) Get comp_id from POST (hidden field), fallback to session
    comp_id_raw = (request.form.get("comp_id") or "").strip()
    comp_id = int(comp_id_raw) if comp_id_raw.isdigit() else session.get("admin_comp_id")

    if not comp_id:
        flash("No competition context (comp_id missing). Open the map from Admin → Comps.", "warning")
        return redirect("/admin/comps")

    current_comp = Competition.query.get(comp_id)
    if not current_comp:
        flash("Competition not found.", "warning")
        return redirect("/admin/comps")

    # Keep session in sync
    session["admin_comp_id"] = current_comp.id

    # Permission check
    if not admin_can_manage_competition(current_comp):
        abort(403)

    section_id_raw = (request.form.get("section_id") or "").strip()
    new_section_name = (request.form.get("new_section_name") or "").strip()
    climb_raw = (request.form.get("climb_number") or "").strip()
    colour = (request.form.get("colour") or "").strip()

    base_raw = (request.form.get("base_points") or "").strip()
    penalty_raw = (request.form.get("penalty_per_attempt") or "").strip()
    cap_raw = (request.form.get("attempt_cap") or "").strip()

    x_raw = (request.form.get("x_percent") or "").strip()
    y_raw = (request.form.get("y_percent") or "").strip()

    # ---- Choose section ----
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
        db.session.flush()  # get section.id

    if not section:
        flash("Please select an existing section or type a new section name.", "warning")
        db.session.rollback()
        return back(current_comp.id)

    # ---- Validate numbers ----
    if not climb_raw.isdigit():
        flash("Climb number must be a whole number.", "warning")
        db.session.rollback()
        return back(current_comp.id)

    if base_raw == "" or penalty_raw == "" or cap_raw == "":
        flash("Base points, penalty, and attempt cap are required.", "warning")
        db.session.rollback()
        return back(current_comp.id)

    if not (base_raw.lstrip("-").isdigit() and penalty_raw.lstrip("-").isdigit() and cap_raw.lstrip("-").isdigit()):
        flash("Base points, penalty, and attempt cap must be whole numbers.", "warning")
        db.session.rollback()
        return back(current_comp.id)

    climb_number = int(climb_raw)
    base_points = int(base_raw)
    penalty_per_attempt = int(penalty_raw)
    attempt_cap = int(cap_raw)

    if climb_number <= 0:
        flash("Climb number must be positive.", "warning")
        db.session.rollback()
        return back(current_comp.id)

    if base_points < 0 or penalty_per_attempt < 0 or attempt_cap <= 0:
        flash("Base points/penalty must be ≥ 0 and attempt cap must be > 0.", "warning")
        db.session.rollback()
        return back(current_comp.id)

    # ---- Coordinates ----
    try:
        x_percent = float(x_raw)
        y_percent = float(y_raw)
    except ValueError:
        flash("You need to click the map first (missing coordinates).", "warning")
        db.session.rollback()
        return back(current_comp.id)

    # ---- Conflict check (unique climb number within this competition) ----
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
        flash(f"Climb #{climb_number} already exists in this competition.", "warning")
        db.session.rollback()
        return back(current_comp.id)

    sc = SectionClimb(
        section_id=section.id,
        gym_id=current_comp.gym_id,
        climb_number=climb_number,
        colour=colour or None,
        base_points=base_points,
        penalty_per_attempt=penalty_per_attempt,
        attempt_cap=attempt_cap,
        x_percent=x_percent,
        y_percent=y_percent,
    )
    db.session.add(sc)

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        flash(f"DB error saving climb: {e}", "warning")
        return back(current_comp.id)

    flash(f"Saved climb #{climb_number}.", "success")
    return back(current_comp.id)

@admin_bp.route("/admin/map/save-boundary", methods=["POST"])
def admin_map_save_boundary():
    """
    Save polygon boundary for a section.
    Payload can be JSON or form-encoded.

    Expects:
      - comp_id
      - section_id
      - points: JSON string or list of points [{x,y},...]
    """
    if not session.get("admin_ok"):
        return jsonify({"ok": False, "error": "Not admin"}), 403

    # Accept JSON body or form
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

    points = _parse_boundary_points(points_raw)

    # Require at least 3 points for a polygon, or allow empty to "clear"
    if points and len(points) < 3:
        return jsonify({"ok": False, "error": "Polygon needs at least 3 points"}), 400

    section.boundary_points_json = _boundary_to_json(points) if points else None
    db.session.commit()

    return jsonify({"ok": True, "section_id": section.id, "points": points})
