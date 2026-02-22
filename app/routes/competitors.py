from flask import (
    Blueprint, render_template, redirect,
    session, request, abort, flash
)
from datetime import datetime

from app.models import (
    Competitor, Competition, Section,
    SectionClimb, Score
)
from app.helpers.admin import admin_can_manage_competition
from app.helpers.competition import (
    comp_is_live,
    comp_is_finished,
)
from app.helpers.gym import get_gym_map_url_for_competition
from app.helpers.leaderboard import build_leaderboard
from app.helpers.scoring import points_for, competitor_total_points

competitors_bp = Blueprint("competitors", __name__)

@competitors_bp.route("/resume")
def resume_competitor():
    """
    Resume scoring for the last competitor on this device.
    Only resumes if their competition is still live/not-ended.
    Otherwise, send them to /my-comps.
    """
    cid = session.get("competitor_id")
    if not cid:
        return redirect("/")

    comp = Competitor.query.get(cid)
    if not comp:
        session.pop("competitor_id", None)
        return redirect("/")

    if comp.competition_id:
        comp_row = Competition.query.get(comp.competition_id)
        if comp_row and comp_row.slug:
            now = datetime.utcnow()
            if comp_row.is_active and (comp_row.end_at is None or comp_row.end_at >= now):
                return redirect(f"/comp/{comp_row.slug}/competitor/{cid}/sections")

    # If the comp is finished (or missing), don't send them back to old scoring
    return redirect("/my-comps")

@competitors_bp.route("/competitor/<int:competitor_id>")
def competitor_redirect(competitor_id):
    """
    Canonical redirect for a competitor "profile" URL.

    Goal:
    - If this competitor belongs to a competition with a slug, redirect to the
      competition-scoped sections URL:
        /comp/<slug>/competitor/<id>/sections

    - Otherwise, fall back to the legacy sections URL:
        /competitor/<id>/sections

    Notes:
    - This route should never render a template.
    - It's safe for shared links / old emails / old QR codes.
    """
    comp = Competitor.query.get_or_404(competitor_id)

    # If competitor is attached to a competition, prefer slugged canonical route
    if comp.competition_id:
        comp_row = Competition.query.get(comp.competition_id)
        if comp_row and comp_row.slug:
            return redirect(f"/comp/{comp_row.slug}/competitor/{competitor_id}/sections")

    # Fallback: legacy route
    return redirect(f"/competitor/{competitor_id}/sections")

@competitors_bp.route("/competitor/<int:competitor_id>/sections")
def competitor_sections(competitor_id):
    """
    Sections index page (legacy URL).

    Rules:
    - Non-admins are forced to their own competitor id from the session.
    - If the competitor row is not registered for a competition -> kick to /my-comps.
    - If competition exists and has a slug -> redirect to slugged route.
    - Only allow access when that competition is LIVE.
    - Leaderboard + sections + map dots are scoped to THIS competition (not "active comp").
    """
    viewer_id = session.get("competitor_id")
    is_admin = session.get("admin_ok", False)

    # Not logged in as competitor and not admin -> no access
    if not viewer_id and not is_admin:
        return redirect("/")

    # Determine which competitor to show
    if is_admin:
        target_id = competitor_id
    else:
        target_id = viewer_id
        if competitor_id != viewer_id:
            return redirect(f"/competitor/{viewer_id}/sections")

    competitor = Competitor.query.get_or_404(target_id)

    # Must belong to a competition. If not, this is an "Account" row or stale session.
    if not competitor.competition_id:
        session.pop("active_comp_slug", None)
        flash("You’re not registered in a competition yet. Pick a comp to join.", "warning")
        return redirect("/my-comps")

    comp_row = Competition.query.get(competitor.competition_id)
    if not comp_row:
        session.pop("active_comp_slug", None)
        flash("That competition no longer exists. Please join again.", "warning")
        return redirect("/my-comps")

    # If the comp has a slug, push everyone to the canonical slugged route.
    # (This prevents legacy routes from becoming the main flow.)
    if comp_row.slug:
        return redirect(f"/comp/{comp_row.slug}/competitor/{target_id}/sections")

    # LIVE gate (scheduled or finished comps should not allow scoring/nav pages)
    if not comp_is_live(comp_row):
        session.pop("active_comp_slug", None)
        if comp_is_finished(comp_row):
            flash("That competition has finished — scoring is locked.", "warning")
        else:
            flash("That competition isn’t live yet — scoring will open when it starts.", "warning")
        return redirect("/my-comps")

    # Only enforce gym-level permissions when an admin is viewing SOMEONE ELSE
    if is_admin and viewer_id and target_id != viewer_id:
        if not admin_can_manage_competition(comp_row):
            abort(403)

    # --- Gym map + gym name (DB-driven) ---
    gym_name = None
    gym_map_path = None
    if comp_row.gym:
        gym_name = comp_row.gym.name
        gym_map_path = comp_row.gym.map_image_path

    # Legacy var (keep during transition)
    gym_map_url = get_gym_map_url_for_competition(comp_row)

    # Scope sections to THIS competition
    sections = (
        Section.query
        .filter(Section.competition_id == comp_row.id)
        .order_by(Section.name)
        .all()
    )

    total_points = competitor_total_points(target_id, comp_row.id)

    # IMPORTANT: Leaderboard must be scoped to THIS competition
    rows, _ = build_leaderboard(None, competition_id=comp_row.id)
    position = None
    for r in rows:
        if r["competitor_id"] == target_id:
            position = r["position"]
            break

    can_edit = (viewer_id == target_id or is_admin)

    # Map dots: only climbs with coords for THIS competition’s sections
    section_ids = [s.id for s in sections]
    if section_ids:
        q = (
            SectionClimb.query
            .filter(
                SectionClimb.section_id.in_(section_ids),
                SectionClimb.x_percent.isnot(None),
                SectionClimb.y_percent.isnot(None),
            )
            .order_by(SectionClimb.climb_number)
        )

        # Optional safety: if SectionClimb has gym_id populated, keep it consistent
        if comp_row.gym_id:
            q = q.filter(SectionClimb.gym_id == comp_row.gym_id)

        map_climbs = q.all()
    else:
        map_climbs = []

    return render_template(
        "competitor_sections.html",
        competitor=competitor,
        sections=sections,
        total_points=total_points,
        position=position,
        nav_active="sections",
        viewer_id=viewer_id,
        is_admin=is_admin,
        can_edit=can_edit,
        map_climbs=map_climbs,
        comp=comp_row,
        comp_slug=None,  # legacy route has no slug canonical

        # New template vars
        gym_name=gym_name,
        gym_map_path=gym_map_path,

        # Legacy
        gym_map_url=gym_map_url,
    )

@competitors_bp.route("/competitor/<int:competitor_id>/stats")
@competitors_bp.route("/competitor/<int:competitor_id>/stats/<string:mode>")
def competitor_stats(competitor_id, mode="my"):
    """
    Legacy stats route.

    If the competitor belongs to a competition with a slug, redirect to:
      /comp/<slug>/competitor/<id>/stats/<mode>

    Otherwise, fall back to the old behaviour (single-comp mode).
    """
    comp = Competitor.query.get_or_404(competitor_id)

    # If this competitor is attached to a competition with a slug, use the new route
    if comp.competition_id:
        comp_row = Competition.query.get(comp.competition_id)
        if comp_row and comp_row.slug:
            return redirect(f"/comp/{comp_row.slug}/competitor/{competitor_id}/stats/{mode}")

    # --- Fallback: original single-comp logic ---

    # Normalise mode
    mode = (mode or "my").lower()
    if mode not in ("my", "overall", "climber"):
        mode = "my"

    total_points = competitor_total_points(competitor_id)

    # Who is viewing?
    view_mode = request.args.get("view", "").lower()
    viewer_id = session.get("competitor_id")
    viewer_is_self = (viewer_id == competitor_id)

    # Spectator mode from old ?view=public flag (still supported)
    is_public_view = (view_mode == "public" and not viewer_is_self)

    sections = Section.query.order_by(Section.name).all()

    # Personal scores for this competitor
    personal_scores = Score.query.filter_by(competitor_id=competitor_id).all()
    personal_by_climb = {s.climb_number: s for s in personal_scores}

    # Global aggregate for every climb across all competitors (no comp scoping)
    all_scores = Score.query.all()
    global_by_climb = {}
    for s in all_scores:
        info = global_by_climb.setdefault(
            s.climb_number,
            {
                "attempts_total": 0,
                "tops": 0,
                "flashes": 0,
                "competitors": set(),
            },
        )
        info["attempts_total"] += s.attempts
        info["competitors"].add(s.competitor_id)
        if s.topped:
            info["tops"] += 1
            if s.attempts == 1:
                info["flashes"] += 1

    # --- get leaderboard position for this competitor ---
    rows, _ = build_leaderboard(None)
    position = None
    for r in rows:
        if r["competitor_id"] == competitor_id:
            position = r["position"]
            break

    section_stats = []
    personal_heatmap_sections = []
    global_heatmap_sections = []

    for sec in sections:
        climbs = (
            SectionClimb.query
            .filter_by(section_id=sec.id)
            .order_by(SectionClimb.climb_number)
            .all()
        )

        sec_tops = 0
        sec_attempts = 0
        sec_points = 0

        personal_cells = []
        global_cells = []

        for sc in climbs:
            score = personal_by_climb.get(sc.climb_number)

            if score:
                sec_attempts += score.attempts
                if score.topped:
                    sec_tops += 1
                sec_points += points_for(
                    score.climb_number, score.attempts, score.topped
                )

                if score.topped and score.attempts == 1:
                    status = "flashed"
                elif score.topped:
                    status = "topped-late"
                else:
                    status = "not-topped"
            else:
                status = "skipped"

            personal_cells.append(
                {
                    "climb_number": sc.climb_number,
                    "status": status,
                }
            )

            g = global_by_climb.get(sc.climb_number)
            if not g or len(g["competitors"]) == 0:
                g_status = "no-data"
            else:
                total_comp = len(g["competitors"])
                tops = g["tops"]
                top_rate = tops / total_comp if total_comp > 0 else 0.0

                if top_rate >= 0.8:
                    g_status = "easy"
                elif top_rate >= 0.4:
                    g_status = "medium"
                else:
                    g_status = "hard"

            global_cells.append(
                {
                    "climb_number": sc.climb_number,
                    "status": g_status,
                }
            )

        efficiency = (sec_tops / sec_attempts) if sec_attempts > 0 else 0.0

        section_stats.append(
            {
                "section": sec,
                "tops": sec_tops,
                "attempts": sec_attempts,
                "efficiency": efficiency,
                "points": sec_points,
            }
        )

        personal_heatmap_sections.append(
            {
                "section": sec,
                "climbs": personal_cells,
            }
        )

        global_heatmap_sections.append(
            {
                "section": sec,
                "climbs": global_cells,
            }
        )

    if mode == "my":
        nav_active = "my_stats"
    elif mode == "overall":
        nav_active = "overall_stats"
    else:
        nav_active = "climber_stats"

    return render_template(
        "competitor_stats.html",
        competitor=comp,
        total_points=total_points,
        position=position,
        section_stats=section_stats,
        heatmap_sections=personal_heatmap_sections,
        global_heatmap_sections=global_heatmap_sections,
        is_public_view=is_public_view,
        viewer_id=viewer_id,
        viewer_is_self=viewer_is_self,
        mode=mode,
        nav_active=nav_active,
    )

@competitors_bp.route("/competitor/<int:competitor_id>/section/<section_slug>")
def competitor_section_climbs(competitor_id, section_slug):
    """
    DROP-IN replacement for legacy per-section route.

    Key fix:
    - Still supports old URLs, but builds `existing` keyed by section_climb_id
    - If competitor is in a slugged comp, redirect to canonical route
    """

    comp = Competitor.query.get_or_404(competitor_id)

    if comp.competition_id:
        comp_row = Competition.query.get(comp.competition_id)
        if comp_row and comp_row.slug:
            return redirect(
                f"/comp/{comp_row.slug}/competitor/{competitor_id}/section/{section_slug}"
            )

    viewer_id = session.get("competitor_id")
    is_admin = session.get("admin_ok", False)

    if not viewer_id and not is_admin:
        return redirect("/")

    target_id = competitor_id if is_admin else viewer_id
    if not target_id:
        return redirect("/")
    if not is_admin and competitor_id != target_id:
        return redirect(f"/competitor/{target_id}/section/{section_slug}")

    competitor = Competitor.query.get_or_404(target_id)

    # Legacy sections are not competition scoped; keep behaviour, but safer to require section exists
    section = Section.query.filter_by(slug=section_slug).first_or_404()
    all_sections = Section.query.order_by(Section.name).all()

    # climbs in this section (include even without coords so score cards exist)
    section_climbs = (
        SectionClimb.query
        .filter(SectionClimb.section_id == section.id)
        .order_by(SectionClimb.climb_number)
        .all()
    )

    climbs = [sc.climb_number for sc in section_climbs]
    colours = {sc.climb_number: sc.colour for sc in section_climbs if sc.colour}
    max_points = {sc.climb_number: sc.base_points for sc in section_climbs if sc.base_points is not None}

    # Scores (legacy: unscoped if no competition context)
    if competitor.competition_id:
        comp_row = Competition.query.get(competitor.competition_id)
        scores = (
            Score.query
            .join(Competitor, Competitor.id == Score.competitor_id)
            .filter(
                Score.competitor_id == target_id,
                Competitor.competition_id == comp_row.id,
            )
            .all()
        )
        per_climb_points = {
            s.climb_number: points_for(s.climb_number, s.attempts, s.topped, comp_row.id)
            for s in scores
        }
        total_points = competitor_total_points(target_id, comp_row.id)
    else:
        scores = Score.query.filter_by(competitor_id=target_id).all()
        per_climb_points = {
            s.climb_number: points_for(s.climb_number, s.attempts, s.topped)
            for s in scores
        }
        total_points = competitor_total_points(target_id)

    # FIX: index by section_climb_id
    existing = {s.section_climb_id: s for s in scores if s.section_climb_id is not None}
    existing_by_number = {s.climb_number: s for s in scores}

    rows, _ = build_leaderboard(None, competition_id=competitor.competition_id) if competitor.competition_id else build_leaderboard(None)
    position = next((r["position"] for r in rows if r["competitor_id"] == target_id), None)

    return render_template(
        "competitor.html",
        competitor=competitor,
        climbs=climbs,
        existing=existing,                      
        existing_by_number=existing_by_number,  # legacy helper
        total_points=total_points,
        section=section,
        colours=colours,
        position=position,
        max_points=max_points,
        per_climb_points=per_climb_points,
        nav_active="sections",
        can_edit=True,
        viewer_id=viewer_id,
        is_admin=is_admin,
        section_climbs=section_climbs,
        sections=all_sections,
        gym_map_url=None,
    )

