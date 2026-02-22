from flask import Blueprint, request, session, jsonify, redirect, current_app, render_template, flash

from app.extensions import db
from app.models import Competition, Competitor, Score, Section, SectionClimb
from app.helpers.leaderboard import comp_is_finished, get_viewer_comp, comp_is_live, build_leaderboard
from app.helpers.leaderboard_cache import invalidate_leaderboard_cache
from app.helpers.scoring import points_for, normalize_leaderboard_category


scores_bp = Blueprint("scores", __name__)

@scores_bp.route("/my-scoring")
def my_scoring_redirect():
    """
    Safe entry point for competitor scoring.

    Priority:
    1) If the logged-in competitor is attached to a competition -> go there.
    2) Else, if the session has an active_comp_slug -> send them to that comp's join page.
    3) Else -> back to /my-comps to pick a competition.
    """
    viewer_id = session.get("competitor_id")
    if not viewer_id:
        return redirect("/")

    competitor = Competitor.query.get(viewer_id)
    if not competitor:
        session.pop("competitor_id", None)
        return redirect("/")

    # 1) If competitor already belongs to a comp, go straight to scoring
    if competitor.competition_id:
        comp = Competition.query.get(competitor.competition_id)
        if comp and comp.slug:
            session["active_comp_slug"] = comp.slug
            return redirect(f"/comp/{comp.slug}/competitor/{competitor.id}/sections")

    # 2) No competition attached yet -> use selected comp from session if present
    slug = (session.get("active_comp_slug") or "").strip()
    if slug:
        # If they have no comp, they must register for this comp first
        return redirect(f"/comp/{slug}/join")

    # 3) No context at all -> choose a comp
    return redirect("/my-comps")

@scores_bp.route("/api/score", methods=["POST"])
def api_save_score():
    """
    Save/upsert a score.

    Preferred payload (new):
      {
        "competitor_id": 123,
        "section_climb_id": 456,
        "attempts": 2,
        "topped": true
      }

    Legacy payload (still supported):
      {
        "competitor_id": 123,
        "climb_number": 17,
        "attempts": 2,
        "topped": true
      }

    Why:
    - DB uniqueness is (competitor_id, section_climb_id)
    - climb_number alone can be ambiguous if the same number exists in multiple sections
    """

    data = request.get_json(force=True, silent=True) or {}

    # ---- parse basics ----
    try:
        competitor_id = int(data.get("competitor_id", 0))
    except (TypeError, ValueError):
        return "Invalid competitor_id", 400

    # attempts + topped
    try:
        attempts = int(data.get("attempts", 1))
    except (TypeError, ValueError):
        attempts = 1
    topped = bool(data.get("topped", False))

    # payload may contain either section_climb_id or climb_number
    section_climb_id_raw = data.get("section_climb_id", None)
    climb_number_raw = data.get("climb_number", None)

    section_climb_id = None
    climb_number = None

    if section_climb_id_raw is not None:
        try:
            section_climb_id = int(section_climb_id_raw)
        except (TypeError, ValueError):
            return "Invalid section_climb_id", 400

    if climb_number_raw is not None:
        try:
            climb_number = int(climb_number_raw)
        except (TypeError, ValueError):
            return "Invalid climb_number", 400

    if competitor_id <= 0:
        return "Invalid competitor_id", 400

    if section_climb_id is None and (climb_number is None or climb_number <= 0):
        return "Missing section_climb_id or climb_number", 400

    # ---- Auth: competitor themself, admin, or local sim in debug ----
    viewer_id = session.get("competitor_id")
    is_admin = session.get("admin_ok", False)

    if (
        not viewer_id
        and not is_admin
        and current_app.debug
        and request.remote_addr in ("127.0.0.1", "::1")
    ):
        is_admin = True

    if viewer_id != competitor_id and not is_admin:
        return "Not allowed", 403

    # ---- competitor + comp context ----
    comp_row = Competitor.query.get(competitor_id)
    if not comp_row:
        return "Competitor not found", 404

    if not comp_row.competition_id:
        return "Competitor not registered for a competition", 400

    current_comp = Competition.query.get(comp_row.competition_id)
    if not current_comp:
        return "Competition not found", 404

    # Block edits once the comp is finished
    if comp_is_finished(current_comp):
        return "Competition finished — scoring locked", 403

    # ---- resolve SectionClimb (source of truth) ----
    sc = None

    if section_climb_id is not None:
        sc = SectionClimb.query.get(section_climb_id)
        if not sc:
            return "Unknown section_climb_id", 400

        # Ensure this section climb belongs to THIS competition
        sec = Section.query.get(sc.section_id) if sc.section_id else None
        if not sec or sec.competition_id != current_comp.id:
            return "section_climb_id not in this competition", 400

    else:
        # Legacy lookup by climb_number scoped to THIS competition
        # IMPORTANT: if duplicates exist across sections, this is ambiguous.
        matches = (
            SectionClimb.query
            .join(Section, Section.id == SectionClimb.section_id)
            .filter(
                SectionClimb.climb_number == climb_number,
                Section.competition_id == current_comp.id,
            )
            .all()
        )

        if not matches:
            return "Unknown climb number for this competition", 400

        if len(matches) > 1:
            # This is exactly the score-card bug scenario.
            # Force clients/templates to use section_climb_id.
            return (
                "Ambiguous climb_number in this competition. "
                "Send section_climb_id instead.",
                400,
            )

        sc = matches[0]

    # ---- clamp attempts ----
    if attempts < 1:
        attempts = 1
    elif attempts > 50:
        attempts = 50

    # flashed = topped on attempt 1
    flashed = bool(topped and attempts == 1)

    # ---- upsert by (competitor_id, section_climb_id) ----
    score = (
        Score.query
        .filter_by(competitor_id=competitor_id, section_climb_id=sc.id)
        .first()
    )

    if not score:
        score = Score(
            competitor_id=competitor_id,
            section_climb_id=sc.id,
            climb_number=sc.climb_number,  # keep for stats/ordering
            attempts=attempts,
            topped=topped,
            flashed=flashed,
        )
        db.session.add(score)
    else:
        score.climb_number = sc.climb_number
        score.attempts = attempts
        score.topped = topped
        score.flashed = flashed

    db.session.commit()
    invalidate_leaderboard_cache()

    points = points_for(sc.climb_number, attempts, topped, current_comp.id)

    return jsonify(
        {
            "ok": True,
            "competitor_id": competitor_id,
            "climb_number": sc.climb_number,
            "section_climb_id": sc.id,
            "attempts": attempts,
            "topped": topped,
            "flashed": flashed,
            "points": points,
        }
    )


@scores_bp.route("/api/score/<int:competitor_id>")
def api_get_scores(competitor_id):
    """
    Return all scores for this competitor.

    IMPORTANT:
    - Returns BOTH section_climb_id and climb_number so the UI can map correctly.
    - Points are scoped to the competitor's competition.
    """

    competitor = Competitor.query.get_or_404(competitor_id)

    scores = (
        Score.query
        .filter_by(competitor_id=competitor_id)
        .order_by(Score.climb_number.asc(), Score.section_climb_id.asc())
        .all()
    )

    comp_id = competitor.competition_id

    out = []
    for s in scores:
        out.append(
            {
                "climb_number": s.climb_number,
                "section_climb_id": s.section_climb_id,
                "attempts": s.attempts,
                "topped": s.topped,
                "flashed": getattr(s, "flashed", False),
                "points": points_for(s.climb_number, s.attempts, s.topped, comp_id),
            }
        )

    return jsonify(out)

@scores_bp.route("/leaderboard")
def leaderboard_all():
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

    # IMPORTANT
    cat = normalize_leaderboard_category(None)

    rows, category_label = build_leaderboard(cat, competition_id=comp.id)

    return render_template(
        "leaderboard.html",
        leaderboard=rows,
        category=category_label,
        current_competitor_id=session.get("competitor_id"),
        nav_active="leaderboard",
        comp=comp,
        comp_slug=comp.slug,
    )


@scores_bp.route("/leaderboard/<category>")
def leaderboard_by_category(category):
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

    # CRITICAL FIX
    cat = normalize_leaderboard_category(category)

    rows, category_label = build_leaderboard(cat, competition_id=comp.id)

    return render_template(
        "leaderboard.html",
        leaderboard=rows,
        category=category_label,
        current_competitor_id=session.get("competitor_id"),
        nav_active="leaderboard",
        comp=comp,
        comp_slug=comp.slug,
    )

@scores_bp.route("/api/leaderboard")
def api_leaderboard():
    raw_category = request.args.get("category")

    comp = get_viewer_comp()
    if not comp:
        return jsonify({"category": "No competition selected", "rows": []})

    if not comp_is_live(comp):
        session.pop("active_comp_slug", None)
        return jsonify({"category": "Competition not live", "rows": []})

    # CRITICAL FIX
    cat = normalize_leaderboard_category(raw_category)

    rows, category_label = build_leaderboard(cat, competition_id=comp.id)

    return jsonify({"category": category_label, "rows": rows})

