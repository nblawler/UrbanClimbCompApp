from flask import Blueprint, request, session, flash, redirect, render_template

from app.models import Competitor, Score, Section, SectionClimb
from app.helpers.leaderboard import build_leaderboard
from app.helpers.scoring import points_for, competitor_total_points
from app.helpers.competition import get_current_comp, get_viewer_comp, comp_is_live


climbs_bp = Blueprint("climbs", __name__)

@climbs_bp.route("/climb/<int:climb_number>/stats")
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

