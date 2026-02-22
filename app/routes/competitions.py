from flask import Blueprint, render_template, redirect, session, flash, abort, request, url_for, jsonify
from datetime import datetime, timedelta
from urllib.parse import quote
import secrets
import sys
import resend

from app.extensions import db
from app.models import Account, Competition, Competitor, Section, SectionClimb, Score, LoginCode, DoublesTeam, DoublesInvite
from app.helpers.admin import admin_can_manage_competition
from app.helpers.climb import parse_boundary_points
from app.helpers.competition import get_comp_or_404, comp_is_live, comp_is_finished
from app.helpers.date import utcnow
from app.helpers.email import send_login_code_via_email
from app.helpers.gym import get_gym_map_url_for_competition
from app.helpers.leaderboard import build_leaderboard
from app.helpers.scoring import points_for, competitor_total_points
from app.helpers.url import make_token, hash_token
from app.config import RESEND_API_KEY, RESEND_FROM_EMAIL

competitions_bp = Blueprint("competitions", __name__)

@competitions_bp.route("/competitions")
def competitions_index():
    """
    Simple list of all competitions.
    For now it's read-only; later we'll wire this into per-comp flows.
    """
    comps = (
        Competition.query
        .order_by(Competition.start_at.asc().nullsfirst())
        .all()
    )

    return render_template("competitions.html", competitions=comps)


@competitions_bp.route("/my-comps")
def my_competitions():
    """
    Competitor-facing hub showing all upcoming competitions.

    - Shows comps with end_at in the future (or no end_at)
    - If comp is live (is_active=True):
        - If competitor is already registered -> "Keep scoring" (go to sections)
        - Else -> "Register" (go to /comp/<slug>/join)
    - If comp is not live -> Upcoming (no register link yet)

    IMPORTANT:
    - A single email can be registered in multiple comps (multiple Competitor rows).
    - So "Keep scoring" must link to the Competitor row for THAT competition,
      not just the current session competitor_id.
    """
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
            if opens_at:
                status_label = (
                    "Comp currently not live – opens on "
                    f"{opens_at.strftime('%d %b %Y, %I:%M %p')}."
                )
            else:
                status_label = "Comp currently not live – opening time TBC."

        # --- IMPORTANT: resolve the correct competitor row for THIS comp ---
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

            if competitor_for_comp:
                if c.slug:
                    my_scoring_url = f"/comp/{c.slug}/competitor/{competitor_for_comp.id}/sections"
                else:
                    my_scoring_url = f"/competitor/{competitor_for_comp.id}/sections"

        # clickable pill target
        pill_href = None
        pill_title = None

        if my_scoring_url:
            pill_href = my_scoring_url
            pill_title = "Keep scoring"
        elif status == "live" and c.slug:
            pill_href = f"/comp/{c.slug}/join"
            pill_title = "Register"
        else:
            pill_href = None
            pill_title = None

        cards.append(
            {
                "comp": c,
                "status": status,
                "status_label": status_label,
                "opens_at": opens_at,
                "my_scoring_url": my_scoring_url,
                "pill_href": pill_href,
                "pill_title": pill_title,
            }
        )

    return render_template(
        "competitions_upcoming.html",
        competitions=competitions,
        cards=cards,
        competitor=competitor,
        nav_active="my_comps",
    )

@competitions_bp.route("/comp/<slug>/doubles/invite", methods=["POST"])
def doubles_invite(slug):
    viewer_id = session.get("competitor_id")
    if not viewer_id:
        return redirect(url_for("login", next=request.path))

    comp = Competition.query.filter_by(slug=slug).first_or_404()

    me = Competitor.query.filter_by(id=viewer_id, competition_id=comp.id).first()
    if not me:
        abort(403)

    # 1) locked already?
    existing_team = DoublesTeam.query.filter(
        DoublesTeam.competition_id == comp.id,
        ((DoublesTeam.competitor_a_id == viewer_id) | (DoublesTeam.competitor_b_id == viewer_id))
    ).first()
    if existing_team:
        flash("You’re already locked into a doubles team for this comp.", "error")
        return redirect(f"/comp/{slug}/doubles")

    # 2) validate email
    invitee_email = (request.form.get("email") or "").strip().lower()
    if not invitee_email:
        flash("Enter an email address.", "error")
        return redirect(f"/comp/{slug}/doubles")

    my_email = (me.email or "").strip().lower()
    if my_email and invitee_email == my_email:
        flash("You can’t invite yourself. That’s just singles with extra paperwork.", "error")
        return redirect(f"/comp/{slug}/doubles")

    # 3) only one pending invite at a time
    pending = DoublesInvite.query.filter_by(
        competition_id=comp.id,
        inviter_competitor_id=viewer_id,
        status="pending"
    ).first()
    if pending:
        flash(f"You already invited {pending.invitee_email}. You can’t invite someone else until that’s resolved.", "error")
        return redirect(f"/comp/{slug}/doubles")

    # 4) create invite row
    token = make_token()
    inv = DoublesInvite(
        competition_id=comp.id,
        inviter_competitor_id=viewer_id,
        invitee_email=invitee_email,
        token_hash=hash_token(token),
        status="pending",
        expires_at=utcnow() + timedelta(hours=48),
    )
    db.session.add(inv)
    db.session.commit()

    accept_url = url_for("doubles_accept", slug=slug, _external=True) + f"?token={token}"

    # 5) send doubles invite email via Resend (same pattern as login code)

    if not RESEND_API_KEY:
        print(f"[DOUBLES INVITE - DEV ONLY] {invitee_email} -> {accept_url}", file=sys.stderr)
    else:
        html = f"""
          <div style="font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; padding: 16px;">
            <p>Hey climber 👋</p>
            <p><strong>{me.name}</strong> has invited you to form a doubles team for:</p>
            <p style="font-weight: 600; margin: 8px 0;">{comp.name}</p>

            <p>Click below to accept:</p>

            <p style="margin: 16px 0;">
              <a href="{accept_url}"
                 style="display:inline-block; padding:10px 18px; border-radius:999px; background:#111; color:#fff; text-decoration:none;">
                 Accept Doubles Invite
              </a>
            </p>

            <p>This link expires in 48 hours.</p>
          </div>
        """

        try:
            params = {
                "from": RESEND_FROM_EMAIL,
                "to": [invitee_email],
                "subject": f"Doubles invite for {comp.name}",
                "html": html,
            }
            resend.Emails.send(params)
            print(f"[DOUBLES INVITE] Sent doubles invite to {invitee_email}", file=sys.stderr)
        except Exception as e:
            print(f"[DOUBLES INVITE] Failed to send via Resend: {e}", file=sys.stderr)

    flash("Invite sent. Waiting for them to accept.", "success")
    return redirect(f"/comp/{slug}/doubles")


@competitions_bp.route("/comp/<slug>/doubles/accept", methods=["GET"])
def doubles_accept(slug):
    token = (request.args.get("token") or "").strip()
    if not token:
        flash("Missing doubles token.", "error")
        return redirect(f"/comp/{slug}/doubles")

    viewer_id = session.get("competitor_id")
    if not viewer_id:
        # Force login then come back here
        return redirect(url_for("login", next=request.url))

    comp = Competition.query.filter_by(slug=slug).first_or_404()

    invite = DoublesInvite.query.filter_by(
        competition_id=comp.id,
        token_hash=hash_token(token),
        status="pending"
    ).first()

    if not invite:
        flash("That doubles link is invalid or already used.", "error")
        return redirect(f"/comp/{slug}/doubles")

    if invite.expires_at < utcnow():
        invite.status = "expired"
        db.session.commit()
        flash("That doubles link expired. Ask them to resend.", "error")
        return redirect(f"/comp/{slug}/doubles")

    me = Competitor.query.filter_by(id=viewer_id, competition_id=comp.id).first()
    if not me:
        abort(403)

    # Make sure the logged-in user is the intended invitee
    if (me.email or "").strip().lower() != (invite.invitee_email or "").strip().lower():
        flash("This invite was sent to a different email address.", "error")
        return redirect(f"/comp/{slug}/doubles")

    # Ensure inviter isn't already locked in a team
    inviter_team = DoublesTeam.query.filter(
        DoublesTeam.competition_id == comp.id,
        ((DoublesTeam.competitor_a_id == invite.inviter_competitor_id) |
         (DoublesTeam.competitor_b_id == invite.inviter_competitor_id))
    ).first()
    if inviter_team:
        flash("The inviter is already in a doubles team. This invite can’t be used.", "error")
        invite.status = "cancelled"
        db.session.commit()
        return redirect(f"/comp/{slug}/doubles")

    # Ensure invitee (me) isn't already locked in a team
    my_team = DoublesTeam.query.filter(
        DoublesTeam.competition_id == comp.id,
        ((DoublesTeam.competitor_a_id == viewer_id) | (DoublesTeam.competitor_b_id == viewer_id))
    ).first()
    if my_team:
        flash("You’re already in a doubles team. This invite can’t be used.", "error")
        invite.status = "cancelled"
        db.session.commit()
        return redirect(f"/comp/{slug}/doubles")

    # Create the team (order doesn't matter; DB unique index enforces no duplicates)
    team = DoublesTeam(
        competition_id=comp.id,
        competitor_a_id=invite.inviter_competitor_id,
        competitor_b_id=viewer_id,
    )
    db.session.add(team)

    invite.status = "accepted"
    invite.accepted_at = utcnow()

    db.session.commit()

    flash("Doubles team created! You’re locked in and will appear on the doubles leaderboard.", "success")
    return redirect(f"/comp/{slug}/doubles")

@competitions_bp.route("/comp/<slug>/doubles/cancel", methods=["POST"])
def doubles_cancel(slug):
    viewer_id = session.get("competitor_id")
    if not viewer_id:
        return redirect(url_for("login", next=request.path))

    comp = Competition.query.filter_by(slug=slug).first_or_404()

    # Only the inviter can cancel their pending invite
    inv = DoublesInvite.query.filter_by(
        competition_id=comp.id,
        inviter_competitor_id=viewer_id,
        status="pending"
    ).order_by(DoublesInvite.created_at.desc()).first()

    if not inv:
        flash("No pending invite to cancel.", "error")
        return redirect(f"/comp/{slug}/doubles")

    inv.status = "cancelled"
    db.session.commit()

    flash("Invite cancelled.", "success")
    return redirect(f"/comp/{slug}/doubles")

@competitions_bp.route("/comp/<slug>/doubles/resend", methods=["POST"])
def doubles_resend(slug):
    viewer_id = session.get("competitor_id")
    if not viewer_id:
        return redirect(url_for("login", next=request.path))

    comp = Competition.query.filter_by(slug=slug).first_or_404()

    me = Competitor.query.filter_by(id=viewer_id, competition_id=comp.id).first()
    if not me:
        abort(403)

    inv = DoublesInvite.query.filter_by(
        competition_id=comp.id,
        inviter_competitor_id=viewer_id,
        status="pending"
    ).order_by(DoublesInvite.created_at.desc()).first()

    if not inv:
        flash("No pending invite to resend.", "error")
        return redirect(f"/comp/{slug}/doubles")

    # Rotate token
    token = make_token()
    inv.token_hash = hash_token(token)
    inv.expires_at = utcnow() + timedelta(hours=48)
    db.session.commit()

    accept_url = url_for("doubles_accept", slug=slug, _external=True) + f"?token={token}"

    # Send via Resend (same pattern as doubles_invite)
    if not RESEND_API_KEY:
        print(f"[DOUBLES INVITE RESEND - DEV ONLY] {inv.invitee_email} -> {accept_url}", file=sys.stderr)
    else:
        html = f"""
          <div style="font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; padding: 16px;">
            <p>Hey climber 👋</p>
            <p><strong>{me.name}</strong> is reminding you about a doubles invite for:</p>
            <p style="font-weight: 600; margin: 8px 0;">{comp.name}</p>

            <p>Click below to accept:</p>

            <p style="margin: 16px 0;">
              <a href="{accept_url}"
                 style="display:inline-block; padding:10px 18px; border-radius:999px; background:#111; color:#fff; text-decoration:none;">
                 Accept Doubles Invite
              </a>
            </p>

            <p>This link expires in 48 hours.</p>
          </div>
        """
        try:
            params = {
                "from": RESEND_FROM_EMAIL,
                "to": [inv.invitee_email],
                "subject": f"Reminder: Doubles invite for {comp.name}",
                "html": html,
            }
            resend.Emails.send(params)
            print(f"[DOUBLES INVITE] Resent doubles invite to {inv.invitee_email}", file=sys.stderr)
        except Exception as e:
            print(f"[DOUBLES INVITE] Failed to resend via Resend: {e}", file=sys.stderr)

    flash("Invite resent.", "success")
    return redirect(f"/comp/{slug}/doubles")


@competitions_bp.route("/comp/<slug>/doubles", methods=["GET"])
def doubles_home(slug):
    viewer_id = session.get("competitor_id")
    if not viewer_id:
        return redirect(url_for("login", next=request.path))

    comp = Competition.query.filter_by(slug=slug).first_or_404()

    competitor = Competitor.query.filter_by(id=viewer_id, competition_id=comp.id).first()
    if not competitor:
        abort(403)

    # Team (if locked in)
    team = DoublesTeam.query.filter(
        DoublesTeam.competition_id == comp.id,
        ((DoublesTeam.competitor_a_id == viewer_id) | (DoublesTeam.competitor_b_id == viewer_id))
    ).first()

    partner = None
    if team:
        partner_id = team.competitor_b_id if team.competitor_a_id == viewer_id else team.competitor_a_id
        partner = Competitor.query.filter_by(id=partner_id, competition_id=comp.id).first()

    # Pending invite (if not in team)
    pending = None
    if not team:
        pending = DoublesInvite.query.filter_by(
            competition_id=comp.id,
            inviter_competitor_id=viewer_id,
            status="pending"
        ).order_by(DoublesInvite.created_at.desc()).first()

    return render_template(
        "doubles.html",
        comp=comp,
        competitor=competitor,
        comp_slug=slug,
        nav_active="doubles",
        team=team,
        partner=partner,
        pending=pending,
    )


@competitions_bp.route("/comp/<slug>/competitor/<int:competitor_id>/sections")
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

@competitions_bp.route("/comp/<slug>/competitor/<int:competitor_id>/stats")
@competitions_bp.route("/comp/<slug>/competitor/<int:competitor_id>/stats/<string:mode>")
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

@competitions_bp.route("/comp/<slug>/competitor/<int:competitor_id>/section/<section_slug>")
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

@competitions_bp.route("/comp/<slug>/join", methods=["GET", "POST"])
def public_register_for_comp(slug):
    comp = get_comp_or_404(slug)

    # Competition must be live
    if not comp_is_live(comp):
        flash("That competition isn’t live — registration is closed.", "warning")
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

@competitions_bp.route("/api/comp/<slug>/section-boundaries")
def api_comp_section_boundaries(slug):
    """
    Return boundaries for all sections in a competition.
    Used by competitor_sections page to zoom to polygon bounds.
    """
    comp = Competition.query.filter_by(slug=slug).first_or_404()

    # Only allow when comp is live (consistent with your UI rules)
    if not comp_is_live(comp):
        return jsonify({"ok": True, "boundaries": {}})

    sections = (
        Section.query
        .filter(Section.competition_id == comp.id)
        .all()
    )

    out = {}
    for s in sections:
        pts = parse_boundary_points(s.boundary_points_json)
        if pts:
            out[str(s.id)] = pts

    return jsonify({"ok": True, "boundaries": out})

