from flask import Blueprint, render_template, redirect, session, flash, abort, request
from datetime import datetime, timedelta
from urllib.parse import quote
import secrets

from app.extensions import db
from app.models import Account, Competition, Competitor, Section, SectionClimb, Score, LoginCode
from app.helpers.leaderboard import comp_is_live, comp_is_finished, admin_can_manage_competition, build_leaderboard, get_viewer_comp, get_gym_map_url_for_competition
from app.helpers.competition import get_comp_or_404
from app.helpers.scoring import points_for, competitor_total_points
from app.helpers.email import send_login_code_via_email


comp_bp = Blueprint("competitions", __name__)

@comp_bp.route("/competitions")
def competitions_index():
    comps = (
        Competition.query
        .order_by(Competition.start_at.asc().nullsfirst())
        .all()
    )
    return render_template("competitions.html", competitions=comps)

@comp_bp.route("/my-comps")
def my_competitions():
    viewer_id = session.get("competitor_id")
    competitor = Competitor.query.get(viewer_id) if viewer_id else None
    now = datetime.utcnow()

    upcoming_q = Competition.query.filter(
        (Competition.end_at == None) | (Competition.end_at >= now)
    )

    competitions = (
        upcoming_q
        .order_by(Competition.start_at.asc().nullsfirst(), Competition.name.asc())
        .all()
    )

    cards = []
    for c in competitions:
        # status + label
        if comp_is_live(c):
            status = "live"
            status_label = "This comp is live — tap to register."
            opens_at = None
        elif comp_is_finished(c):
            status = "finished"
            status_label = "This comp has finished — registration is closed."
            opens_at = None
        else:
            status = "scheduled"
            opens_at = c.start_at
            status_label = (
                f"Comp currently not live – opens on {opens_at.strftime('%d %b %Y, %I:%M %p')}."
                if opens_at else "Comp currently not live – opening time TBC."
            )

        my_scoring_url = None
        if competitor and competitor.email:
            competitor_for_comp = (
                Competitor.query
                .filter(
                    Competitor.email == competitor.email,
                    Competitor.competition_id == c.id,
                )
                .first()
            )
            if competitor_for_comp and c.slug:
                my_scoring_url = f"/comp/{c.slug}/competitor/{competitor_for_comp.id}/sections"
        
        pill_href, pill_title = None, None
        if my_scoring_url:
            pill_href, pill_title = my_scoring_url, "Keep scoring"
        elif status == "live" and c.slug:
            pill_href, pill_title = f"/comp/{c.slug}/join", "Register"

        cards.append({
            "comp": c,
            "status": status,
            "status_label": status_label,
            "opens_at": opens_at,
            "my_scoring_url": my_scoring_url,
            "pill_href": pill_href,
            "pill_title": pill_title,
        })

    return render_template(
        "competitions_upcoming.html",
        competitions=competitions,
        cards=cards,
        competitor=competitor,
        nav_active="my_comps",
    )

@comp_bp.route("/resume")
def resume_competitor():
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

    return redirect("/my-comps")

@comp_bp.route("/my-scoring")
def my_scoring_redirect():
    viewer_id = session.get("competitor_id")
    if not viewer_id:
        return redirect("/")

    competitor = Competitor.query.get(viewer_id)
    if not competitor:
        session.pop("competitor_id", None)
        return redirect("/")

    if competitor.competition_id:
        comp = Competition.query.get(competitor.competition_id)
        if comp and comp.slug:
            session["active_comp_slug"] = comp.slug
            return redirect(f"/comp/{comp.slug}/competitor/{competitor.id}/sections")

    slug = (session.get("active_comp_slug") or "").strip()
    if slug:
        return redirect(f"/comp/{slug}/join")

    return redirect("/my-comps")

@comp_bp.route("/comp/<slug>/competitor/<int:competitor_id>/section/<section_slug>")
def comp_competitor_section_climbs(slug, competitor_id, section_slug):
    """
    Key fix:
    - Build `existing` keyed by section_climb_id (matches DB uniqueness)
    - Also provide `existing_by_number` for backward compatibility
    """

    current_comp = get_comp_or_404(slug)

    if not comp_is_live(current_comp):
        session.pop("active_comp_slug", None)
        if comp_is_finished(current_comp):
            flash("That competition has finished — scoring is locked.", "warning")
        else:
            flash("That competition isn’t live yet — scoring isn’t available.", "warning")
        return redirect("/my-comps")

    viewer_id = session.get("competitor_id")
    is_admin = session.get("admin_ok", False)
    account_id = session.get("account_id")

    if not viewer_id and not is_admin and not account_id:
        return redirect("/")

    # Resolve target competitor:
    # - If logged-in account is registered for this comp, force to that competitor row
    target_id = None
    if account_id:
        acct = Account.query.get(account_id)
        if acct:
            registered = (
                Competitor.query
                .filter(
                    Competitor.account_id == acct.id,
                    Competitor.competition_id == current_comp.id,
                )
                .first()
            )
            if registered:
                target_id = registered.id
                # heal session
                session["competitor_id"] = registered.id
                session["competitor_email"] = registered.email or acct.email
                session["active_comp_slug"] = current_comp.slug

    if target_id is None:
        # fall back to admin / legacy behaviour
        if is_admin:
            target_id = competitor_id
        else:
            target_id = viewer_id
            if not target_id:
                return redirect("/")
            if competitor_id != target_id:
                return redirect(f"/comp/{slug}/competitor/{target_id}/section/{section_slug}")

    competitor = Competitor.query.get_or_404(target_id)

    if not competitor.competition_id:
        session.pop("active_comp_slug", None)
        flash("You’re not registered in a competition yet. Pick a comp to join.", "warning")
        return redirect("/my-comps")

    if competitor.competition_id != current_comp.id:
        abort(404)

    # Admin permission if viewing someone else
    if is_admin and viewer_id and target_id != viewer_id:
        if not admin_can_manage_competition(current_comp):
            abort(403)

    section = (
        Section.query
        .filter_by(slug=section_slug, competition_id=current_comp.id)
        .first_or_404()
    )

    all_sections = (
        Section.query
        .filter_by(competition_id=current_comp.id)
        .order_by(Section.name)
        .all()
    )

    rows, _ = build_leaderboard(None, competition_id=current_comp.id)
    position = next((r["position"] for r in rows if r["competitor_id"] == target_id), None)

    # IMPORTANT: include climbs even if coords are missing (so score cards exist)
    section_climbs = (
        SectionClimb.query
        .filter(SectionClimb.section_id == section.id)
        .order_by(SectionClimb.climb_number)
        .all()
    )

    # UI card list uses climb numbers (fine), but SCORE LOOKUP should use section_climb_id
    climbs = [sc.climb_number for sc in section_climbs]

    colours = {sc.climb_number: sc.colour for sc in section_climbs if sc.colour}
    max_points = {sc.climb_number: sc.base_points for sc in section_climbs if sc.base_points is not None}

    # Pull all scores for this competitor (scoped to this comp)
    scores = (
        Score.query
        .join(Competitor, Competitor.id == Score.competitor_id)
        .filter(
            Score.competitor_id == target_id,
            Competitor.competition_id == current_comp.id,
        )
        .all()
    )

    # FIX: index by section_climb_id (source of truth)
    existing = {s.section_climb_id: s for s in scores if s.section_climb_id is not None}

    # Backward-compatible index by climb_number (only safe if comp has unique climb_numbers)
    existing_by_number = {s.climb_number: s for s in scores}

    per_climb_points = {
        s.climb_number: points_for(s.climb_number, s.attempts, s.topped, current_comp.id)
        for s in scores
    }

    total_points = competitor_total_points(target_id, current_comp.id)
    gym_map_url = get_gym_map_url_for_competition(current_comp)

    return render_template(
        "competitor.html",
        competitor=competitor,
        climbs=climbs,
        existing=existing,                      # NEW: keyed by section_climb_id
        existing_by_number=existing_by_number,  # Legacy helper for templates still using climb_number
        total_points=total_points,
        section=section,
        colours=colours,
        position=position,
        max_points=max_points,
        per_climb_points=per_climb_points,
        nav_active="sections",
        can_edit=True,
        viewer_id=session.get("competitor_id"),
        is_admin=is_admin,
        section_climbs=section_climbs,
        sections=all_sections,
        gym_map_url=gym_map_url,
        comp=current_comp,
        comp_slug=slug,
    )

@comp_bp.route("/comp/<slug>/join", methods=["GET", "POST"])
def public_register_for_comp(slug):
    comp = get_comp_or_404(slug)

    # Competition must be live
    if not comp_is_live(comp):
        flash("That competition isn't live — registration is closed.", "warning")
        for k in [
            "pending_join_slug",
            "pending_join_name",
            "pending_join_gender",
            "pending_comp_verify",
            "active_comp_slug",
        ]:
            session.pop(k, None)
        return redirect("/my-comps")

    # Must have an account_id in session (NOT competitor_id, NOT competitor_email)
    account_id = session.get("account_id")
    if not account_id:
        # IMPORTANT: when not logged in, do NOT establish comp context in session
        session.pop("competitor_id", None)
        session.pop("active_comp_slug", None)

        flash("Please log in first to join this competition.", "warning")
        next_path = f"/comp/{comp.slug}/join"
        return redirect(f"/login?slug={comp.slug}&next={quote(next_path)}")

    # Fetch account (now safe)
    acct = Account.query.get(account_id)
    if not acct:
        # Session is stale/bad: clear and force login
        for k in ["account_id", "competitor_id", "competitor_email", "active_comp_slug"]:
            session.pop(k, None)

        flash("Please log in again to continue.", "warning")
        next_path = f"/comp/{comp.slug}/join"
        return redirect(f"/login?slug={comp.slug}&next={quote(next_path)}")

    # Check if this ACCOUNT is already registered for THIS comp
    existing_for_comp = (
        Competitor.query
        .filter(
            Competitor.account_id == acct.id,
            Competitor.competition_id == comp.id,
        )
        .first()
    )

    # If already registered -> establish comp context + competitor_id and go score
    if existing_for_comp and request.method == "GET":
        session["competitor_id"] = existing_for_comp.id
        session["competitor_email"] = acct.email
        session["active_comp_slug"] = comp.slug
        session.pop("pending_comp_verify", None)
        return redirect(f"/comp/{comp.slug}/competitor/{existing_for_comp.id}/sections")

    # Not registered yet (GET): make sure there is NO sticky scoring context
    if request.method == "GET" and not existing_for_comp:
        session.pop("competitor_id", None)
        session.pop("active_comp_slug", None)
        # also clear any stale pending join state for a different comp
        session.pop("pending_comp_verify", None)

        return render_template(
            "register_public.html",
            comp=comp,
            error=None,
            name="",
            gender="Inclusive",
        )

    # POST: attempt to register (this is where we *can* establish comp context)
    name = (request.form.get("name") or "").strip()
    gender = (request.form.get("gender") or "Inclusive").strip()
    if gender not in ("Male", "Female", "Inclusive"):
        gender = "Inclusive"

    if not name:
        return render_template(
            "register_public.html",
            comp=comp,
            error="Please enter your name.",
            name=name,
            gender=gender,
        )

    # If already registered (POST) -> go score (and set context)
    if existing_for_comp:
        session["competitor_id"] = existing_for_comp.id
        session["competitor_email"] = acct.email
        session["active_comp_slug"] = comp.slug
        session.pop("pending_comp_verify", None)
        return redirect(f"/comp/{comp.slug}/competitor/{existing_for_comp.id}/sections")

    # Store pending join details (for delayed verify)
    session["pending_join_slug"] = comp.slug
    session["pending_join_name"] = name
    session["pending_join_gender"] = gender
    session["pending_comp_verify"] = comp.slug

    session["login_email"] = acct.email
    session["competitor_email"] = acct.email

    # IMPORTANT: we can set active_comp_slug during the verify flow
    # because user is actively joining this comp now
    session["active_comp_slug"] = comp.slug

    # Send code against ACCOUNT
    code = f"{secrets.randbelow(1_000_000):06d}"
    now = datetime.utcnow()

    # Need a shell competitor_id for legacy LoginCode column
    shell = (
        Competitor.query
        .filter(
            Competitor.account_id == acct.id,
            Competitor.competition_id.is_(None),
        )
        .order_by(Competitor.created_at.desc())
        .first()
    )

    if not shell:
        shell = Competitor(
            name="Account",
            gender="Inclusive",
            email=acct.email,
            competition_id=None,
            account_id=acct.id,
        )
        db.session.add(shell)
        db.session.commit()

    login_code = LoginCode(
        competitor_id=shell.id,    # legacy
        account_id=acct.id,        # real
        code=code,
        created_at=now,
        expires_at=now + timedelta(minutes=10),
        used=False,
    )
    db.session.add(login_code)
    db.session.commit()

    send_login_code_via_email(acct.email, code)

    flash(f"We sent a 6-digit code to {acct.email}.", "success")
    next_path = f"/comp/{comp.slug}/join"
    return redirect(f"/login/verify?slug={comp.slug}&next={quote(next_path)}")

@comp_bp.route("/leaderboard")
def leaderboard_all():
    """
    Leaderboard page for the currently selected competition context.

    Rules:
    - Must have a selected competition context (get_viewer_comp()).
    - That competition must be LIVE to view leaderboard.
      (If you later want finished comps viewable read-only, we can relax this.)
    """
    # Optional highlighting of a competitor row
    cid_raw = (request.args.get("cid") or "").strip()
    competitor = Competitor.query.get(int(cid_raw)) if cid_raw.isdigit() else None

    comp = get_viewer_comp()

    # No comp context at all
    if not comp:
        flash("Pick a competition first to view the leaderboard.", "warning")
        return redirect("/my-comps")

    # Comp exists but isn't live -> clear stale comp context and bounce
    if not comp_is_live(comp):
        # Prevent stale slug from keeping comp-nav alive
        session.pop("active_comp_slug", None)
        flash("That competition isn’t live right now — leaderboard is unavailable.", "warning")
        return redirect("/my-comps")

    rows, category_label = build_leaderboard(None, competition_id=comp.id)
    current_competitor_id = session.get("competitor_id")

    return render_template(
        "leaderboard.html",
        leaderboard=rows,
        category=category_label,
        competitor=competitor,
        current_competitor_id=current_competitor_id,
        nav_active="leaderboard",
        comp=comp,
        comp_slug=comp.slug,
    )


@comp_bp.route("/leaderboard/<category>")
def leaderboard_by_category(category):
    """
    Category leaderboard for the currently selected competition context.

    Same rules as /leaderboard:
    - Must have a selected comp context
    - Must be LIVE
    """
    cid_raw = (request.args.get("cid") or "").strip()
    competitor = Competitor.query.get(int(cid_raw)) if cid_raw.isdigit() else None

    comp = get_viewer_comp()

    if not comp:
        flash("Pick a competition first to view the leaderboard.", "warning")
        return redirect("/my-comps")

    if not comp_is_live(comp):
        session.pop("active_comp_slug", None)
        flash("That competition isn’t live right now — leaderboard is unavailable.", "warning")
        return redirect("/my-comps")

    rows, category_label = build_leaderboard(category, competition_id=comp.id)
    current_competitor_id = session.get("competitor_id")

    return render_template(
        "leaderboard.html",
        leaderboard=rows,
        category=category_label,
        competitor=competitor,
        current_competitor_id=current_competitor_id,
        nav_active="leaderboard",
        comp=comp,
        comp_slug=comp.slug,
    )
