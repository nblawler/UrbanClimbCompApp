from flask import (
    Blueprint, render_template, redirect,
    session, request, abort, flash
)

from app.extensions import db
from app.models import (
    Competitor, Competition, Section,
    SectionClimb, Score, Account
)
from app.helpers.scoring import competitor_total_points
from app.helpers.leaderboard import build_leaderboard
from app.helpers.leaderboard import (
    comp_is_live,
    comp_is_finished,
    get_viewer_comp,
)
from app.helpers.leaderboard import admin_can_manage_competition
from app.helpers.leaderboard import get_gym_map_url_for_competition
from app.helpers.scoring import points_for
from app.helpers.competition import get_current_comp, get_comp_or_404


competitors_bp = Blueprint("competitors", __name__)

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


@competitors_bp.route("/comp/<slug>/competitor/<int:competitor_id>/sections")
def comp_competitor_sections(slug, competitor_id):
    """
    Competitor scoring page (comp-scoped).

    Key rule:
    - If the logged-in account is registered for this competition, allow them to score
      regardless of admin_ok.
    - Admin powers are additive (view others), not restrictive.
    """

    current_comp = Competition.query.filter_by(slug=slug).first_or_404()

    account_id = session.get("account_id")
    is_admin = session.get("admin_ok", False)

    if not account_id and not is_admin:
        return redirect("/")

    acct = Account.query.get(account_id) if account_id else None
    if account_id and not acct:
        # stale session
        for k in ["account_id", "competitor_id", "active_comp_slug", "competitor_email"]:
            session.pop(k, None)
        return redirect("/login")

    # 1) If logged-in account exists, try to resolve THEIR registered competitor row for this comp
    registered = None
    if acct:
        registered = (
            Competitor.query
            .filter(
                Competitor.account_id == acct.id,
                Competitor.competition_id == current_comp.id,
            )
            .first()
        )

    # 2) NORMAL COMPETITOR ACCESS (preferred, even if admin_ok=True)
    if registered:
        # Heal stale URLs: always force to the correct competitor id for this account+comp
        if competitor_id != registered.id:
            return redirect(f"/comp/{slug}/competitor/{registered.id}/sections")

        # Establish correct scoring context
        session["competitor_id"] = registered.id
        session["competitor_email"] = registered.email or (acct.email if acct else None)
        session["active_comp_slug"] = slug

        target_id = registered.id
        can_edit = True

    # 3) NOT REGISTERED: allow ADMIN VIEW (with gym permission gate)
    else:
        if not is_admin:
            # Not registered and not admin -> must join
            session.pop("competitor_id", None)
            session.pop("active_comp_slug", None)
            return redirect(f"/comp/{slug}/join")

        # Admin viewing a competitor (must be in this comp)
        comp = Competitor.query.get_or_404(competitor_id)
        if not comp.competition_id or comp.competition_id != current_comp.id:
            abort(404)

        if not admin_can_manage_competition(current_comp):
            abort(403)

        target_id = comp.id
        can_edit = True

    # --- Gym map + gym name (DB-driven) ---
    gym_name = None
    gym_map_path = None
    if current_comp.gym:
        gym_name = current_comp.gym.name
        gym_map_path = current_comp.gym.map_image_path

    gym_map_url = get_gym_map_url_for_competition(current_comp)

    # Sections scoped to THIS competition
    sections = (
        Section.query
        .filter(Section.competition_id == current_comp.id)
        .order_by(Section.name)
        .all()
    )

    total_points = competitor_total_points(target_id, current_comp.id)

    rows, _ = build_leaderboard(None, competition_id=current_comp.id)
    position = None
    for r in rows:
        if r["competitor_id"] == target_id:
            position = r["position"]
            break

    # Map dots: climbs with coords for THIS competition’s sections (+ gym guard)
    if sections:
        section_ids = [s.id for s in sections]
        q = (
            SectionClimb.query
            .filter(
                SectionClimb.section_id.in_(section_ids),
                SectionClimb.x_percent.isnot(None),
                SectionClimb.y_percent.isnot(None),
            )
        )
        if current_comp.gym_id:
            q = q.filter(SectionClimb.gym_id == current_comp.gym_id)

        map_climbs = q.order_by(SectionClimb.climb_number).all()
    else:
        map_climbs = []

    comp_row = Competitor.query.get_or_404(target_id)

    return render_template(
        "competitor_sections.html",
        competitor=comp_row,
        sections=sections,
        total_points=total_points,
        position=position,
        nav_active="sections",
        viewer_id=session.get("competitor_id"),
        is_admin=is_admin,
        can_edit=can_edit,
        map_climbs=map_climbs,
        comp=current_comp,
        comp_slug=slug,
        gym_name=gym_name,
        gym_map_path=gym_map_path,
        gym_map_url=gym_map_url,
    )



# --- Competitor stats page: My Stats + Overall Stats ---

@competitors_bp.route("/comp/<slug>/competitor/<int:competitor_id>/stats")
@competitors_bp.route("/comp/<slug>/competitor/<int:competitor_id>/stats/<string:mode>")
def comp_competitor_stats(slug, competitor_id, mode="my"):
    """
    Stats for a competitor, scoped to a specific competition slug.

    HARD RULE:
    - If comp is NOT LIVE, stats are unavailable.
    - If comp is FINISHED, stats are locked. (handled explicitly too)

    mode:
    - "my"       personal stats
    - "overall"  overall stats
    - "climber"  spectator-ish view of a competitor (still blocked if not live)
    """
    current_comp = get_comp_or_404(slug)

    # Block anything not live (scheduled or finished)
    if not comp_is_live(current_comp):
        # If it’s finished, be explicit
        if comp_is_finished(current_comp):
            flash("That competition has finished — stats are locked.", "warning")
        else:
            flash("That competition isn’t live yet — stats aren’t available.", "warning")

        # prevent stale nav context hanging around
        session.pop("active_comp_slug", None)
        return redirect("/my-comps")

    # Normalise mode
    mode = (mode or "my").lower()
    if mode not in ("my", "overall", "climber"):
        mode = "my"

    comp = Competitor.query.get_or_404(competitor_id)

    # Competitor must belong to this competition
    if comp.competition_id != current_comp.id:
        abort(404)

    total_points = competitor_total_points(competitor_id, current_comp.id)

    # Who is viewing?
    viewer_id = session.get("competitor_id")
    viewer_is_self = (viewer_id == competitor_id)
    is_admin = session.get("admin_ok", False)

    # Optional public view flag (still requires comp live)
    view_mode = request.args.get("view", "").lower()
    is_public_view = (view_mode == "public" and not viewer_is_self)

    # If not self and not admin, allow only public view
    if not viewer_is_self and not is_admin and not is_public_view:
        return redirect(f"/comp/{slug}/competitor/{viewer_id}/stats/{mode}") if viewer_id else redirect("/")

    # Sections only for this competition
    sections = (
        Section.query
        .filter_by(competition_id=current_comp.id)
        .order_by(Section.name)
        .all()
    )

    # Personal scores
    personal_scores = Score.query.filter_by(competitor_id=competitor_id).all()
    personal_by_climb = {s.climb_number: s for s in personal_scores}

    # Global aggregate: ONLY scores from this competition
    all_scores = (
        db.session.query(Score)
        .join(Competitor, Competitor.id == Score.competitor_id)
        .filter(Competitor.competition_id == current_comp.id)
        .all()
    )

    global_by_climb = {}
    for s in all_scores:
        info = global_by_climb.setdefault(
            s.climb_number,
            {"attempts_total": 0, "tops": 0, "flashes": 0, "competitors": set()},
        )
        info["attempts_total"] += s.attempts
        info["competitors"].add(s.competitor_id)
        if s.topped:
            info["tops"] += 1
            if s.attempts == 1:
                info["flashes"] += 1

    # Leaderboard position
    rows, _ = build_leaderboard(None, competition_id=current_comp.id)
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
            # Personal
            score = personal_by_climb.get(sc.climb_number)
            if score:
                sec_attempts += score.attempts
                if score.topped:
                    sec_tops += 1
                sec_points += points_for(score.climb_number, score.attempts, score.topped, current_comp.id)

                if score.topped and score.attempts == 1:
                    status = "flashed"
                elif score.topped:
                    status = "topped-late"
                else:
                    status = "not-topped"
            else:
                status = "skipped"

            personal_cells.append({"climb_number": sc.climb_number, "status": status})

            # Global
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

            global_cells.append({"climb_number": sc.climb_number, "status": g_status})

        efficiency = (sec_tops / sec_attempts) if sec_attempts > 0 else 0.0

        section_stats.append(
            {"section": sec, "tops": sec_tops, "attempts": sec_attempts, "efficiency": efficiency, "points": sec_points}
        )

        personal_heatmap_sections.append({"section": sec, "climbs": personal_cells})
        global_heatmap_sections.append({"section": sec, "climbs": global_cells})

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
        comp=current_comp,
        comp_slug=slug,
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

@competitors_bp.route("/climb/<int:climb_number>/stats")
def climb_stats(climb_number):
    """
    Stats for a single climb across all competitors.

    HARD RULE:
    - Only available when there is a LIVE competition in context.
    """
    comp = get_viewer_comp() or get_current_comp()

    if not comp or not comp_is_live(comp):
        session.pop("active_comp_slug", None)
        flash("There’s no live competition right now — climb stats are unavailable.", "warning")
        return redirect("/my-comps")

    # Mode selection
    mode = (request.args.get("mode", "global") or "global").strip().lower()
    if mode not in ("personal", "global"):
        mode = "global"

    from_climber = (request.args.get("from_climber", "0") == "1")

    cid_raw = request.args.get("cid", "").strip()
    competitor = None
    total_points = None
    position = None

    if cid_raw.isdigit():
        competitor = Competitor.query.get(int(cid_raw))
        if competitor:
            total_points = competitor_total_points(competitor.id, comp.id)
            rows, _ = build_leaderboard(None, competition_id=comp.id)
            for r in rows:
                if r["competitor_id"] == competitor.id:
                    position = r["position"]
                    break

    comp_sections = Section.query.filter(Section.competition_id == comp.id).all()
    section_ids_for_comp = {s.id for s in comp_sections}

    section_climbs = (
        SectionClimb.query
        .filter(
            SectionClimb.climb_number == climb_number,
            SectionClimb.section_id.in_(section_ids_for_comp) if section_ids_for_comp else True,
        )
        .all()
    )

    if not section_climbs:
        nav_active = "climber_stats" if from_climber else ("my_stats" if mode == "personal" else "overall_stats")
        return render_template(
            "climb_stats.html",
            climb_number=climb_number,
            has_config=False,
            competitor=competitor,
            total_points=total_points,
            position=position,
            mode=mode,
            nav_active=nav_active,
            from_climber=from_climber,
        )

    section_ids = {sc.section_id for sc in section_climbs}
    sections = Section.query.filter(Section.id.in_(section_ids)).all()
    sections_by_id = {s.id: s for s in sections}

    scores = (
        Score.query
        .join(Competitor, Score.competitor_id == Competitor.id)
        .filter(
            Score.climb_number == climb_number,
            Competitor.competition_id == comp.id,
        )
        .all()
    )

    total_attempts = sum(s.attempts for s in scores)
    tops = sum(1 for s in scores if s.topped)
    flashes = sum(1 for s in scores if s.topped and s.attempts == 1)
    competitor_ids = {s.competitor_id for s in scores}
    num_competitors = len(competitor_ids)

    top_rate = (tops / num_competitors) if num_competitors > 0 else 0.0
    flash_rate = (flashes / num_competitors) if num_competitors > 0 else 0.0
    avg_attempts_per_comp = (total_attempts / num_competitors) if num_competitors > 0 else 0.0
    avg_attempts_on_tops = (sum(s.attempts for s in scores if s.topped) / tops) if tops > 0 else 0.0

    if num_competitors == 0:
        global_difficulty_key = "no-data"
        global_difficulty_label = "Not tried yet (no data)"
    else:
        if top_rate >= 0.8:
            global_difficulty_key = "easy"
            global_difficulty_label = "Easier"
        elif top_rate >= 0.4:
            global_difficulty_key = "medium"
            global_difficulty_label = "Medium"
        else:
            global_difficulty_key = "hard"
            global_difficulty_label = "Harder"

    comps = {}
    if competitor_ids:
        comps = {c.id: c for c in Competitor.query.filter(Competitor.id.in_(competitor_ids)).all()}

    per_competitor = []
    for s in scores:
        c = comps.get(s.competitor_id)
        per_competitor.append(
            {
                "competitor_id": s.competitor_id,
                "name": c.name if c else f"#{s.competitor_id}",
                "attempts": s.attempts,
                "topped": s.topped,
                "points": points_for(s.climb_number, s.attempts, s.topped, comp.id),
                "updated_at": s.updated_at,
            }
        )

    per_competitor.sort(key=lambda r: (not r["topped"], r["attempts"]))

    personal_row = None
    if competitor:
        for row in per_competitor:
            if row["competitor_id"] == competitor.id:
                personal_row = row
                break

    nav_active = "climber_stats" if from_climber else ("my_stats" if mode == "personal" else "overall_stats")

    return render_template(
        "climb_stats.html",
        climb_number=climb_number,
        has_config=True,
        sections=[sections_by_id[sc.section_id] for sc in section_climbs if sc.section_id in sections_by_id],
        total_attempts=total_attempts,
        tops=tops,
        flashes=flashes,
        num_competitors=num_competitors,
        top_rate=top_rate,
        flash_rate=flash_rate,
        avg_attempts_per_comp=avg_attempts_per_comp,
        avg_attempts_on_tops=avg_attempts_on_tops,
        per_competitor=per_competitor,
        personal_row=personal_row,
        competitor=competitor,
        total_points=total_points,
        position=position,
        mode=mode,
        nav_active=nav_active,
        global_difficulty_key=global_difficulty_key,
        global_difficulty_label=global_difficulty_label,
        from_climber=from_climber,
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
