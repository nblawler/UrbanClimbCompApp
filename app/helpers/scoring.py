from app.models import Competitor, Competition, SectionClimb, Section, Score
from app.helpers.competition import get_current_comp


def points_for(climb_number, attempts, topped, competition_id=None):
    """
    NEW SCORING RULES (fixed points per climb):
    - If not topped: 0 points
    - If topped: fixed base_points from DB for that climb (scoped to competition)
    - attempts are NOT used to reduce points anymore (kept for compatibility)

    NOTE:
    This function still resolves SectionClimb by (climb_number + competition scope).
    If you have duplicate climb_number across sections in the same competition,
    the FIRST match will be used. For perfect accuracy, compute points using
    section_climb_id elsewhere (e.g., in score saving / stats).
    """
    if not topped:
        return 0

    # attempts clamp (not used for points, but keep data sane)
    try:
        attempts = int(attempts)
    except Exception:
        attempts = 1
    attempts = max(1, min(attempts, 50))

    # Resolve competition scope
    comp = Competition.query.get(competition_id) if competition_id else get_current_comp()
    if not comp:
        return 0

    # Resolve SectionClimb config for THIS competition
    q = (
        SectionClimb.query
        .join(Section, Section.id == SectionClimb.section_id)
        .filter(
            SectionClimb.climb_number == climb_number,
            Section.competition_id == comp.id,
        )
    )

    # Optional extra safety: ensure gym matches too
    if getattr(comp, "gym_id", None):
        q = q.filter(SectionClimb.gym_id == comp.gym_id)

    sc = q.first()
    if not sc or sc.base_points is None:
        return 0

    return int(sc.base_points)


def competitor_total_points(comp_id: int, competition_id=None, top_n: int = 8) -> int:
    """
    NEW TOTALS RULES:
    - Only the TOP `top_n` scoring climbs count toward total (default 8)
    - Per-climb points are fixed base_points if topped else 0
    - Attempts do NOT change points (only used for tie-breaks in leaderboard)
    """

    # If we know the competition, only count that competition's scores
    if competition_id:
        scores = (
            Score.query
            .join(Competitor, Competitor.id == Score.competitor_id)
            .filter(
                Score.competitor_id == comp_id,
                Competitor.competition_id == competition_id,
            )
            .all()
        )
    else:
        # Fallback: old behaviour (all scores for competitor)
        scores = Score.query.filter_by(competitor_id=comp_id).all()

    earned_points = [
        points_for(s.climb_number, s.attempts, s.topped, competition_id)
        for s in scores
    ]

    earned_points.sort(reverse=True)
    top_n = int(top_n) if top_n is not None else 8
    if top_n < 1:
        top_n = 1

    return sum(earned_points[:top_n])


def competitor_top_scores_and_attempts(comp_id: int, competition_id: int, top_n: int = 8):
    """
    Helper for leaderboards/stats:
    Returns (total_points, attempts_on_tops, tops_count) using ONLY the TOP `top_n` climbs.

    - total_points: sum of top_n earned points
    - attempts_on_tops: sum of attempts for topped climbs within the selected top_n
    - tops_count: number of topped climbs within the selected top_n

    This uses points_for() for earned points, so it remains competition-scoped.
    """
    scores = (
        Score.query
        .join(Competitor, Competitor.id == Score.competitor_id)
        .filter(
            Score.competitor_id == comp_id,
            Competitor.competition_id == competition_id,
        )
        .all()
    )

    scored = []
    for s in scores:
        p = points_for(s.climb_number, s.attempts, s.topped, competition_id)
        scored.append({
            "points": int(p or 0),
            "attempts": int(s.attempts or 0),
            "topped": bool(s.topped),
        })

    # Sort by points desc, then attempts asc for stability
    scored.sort(key=lambda x: (-x["points"], x["attempts"]))

    top_n = int(top_n) if top_n is not None else 8
    if top_n < 1:
        top_n = 1

    top = scored[:top_n]

    total_points = sum(x["points"] for x in top)
    tops = sum(1 for x in top if x["points"] > 0 and x["topped"])
    attempts_on_tops = sum(x["attempts"] for x in top if x["points"] > 0 and x["topped"])

    return total_points, attempts_on_tops, tops