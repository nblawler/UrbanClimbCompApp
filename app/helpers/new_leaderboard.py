from app.extensions import db
from app.models import Competitor, Score, Section, SectionClimb, Leaderboard


def normalize_leaderboard_category(category):
    value = (category or "all").strip().lower()
    if value in {"all", "male", "female", "inclusive"}:
        return value
    return "all"


def refresh_leaderboard_row(competitor_id: int, competition_id: int, top_n: int):
    """
    Recalculate one competitor's leaderboard summary for one competition.
    Uses section_climb.base_points directly.
    """
    top_n = int(top_n) if top_n is not None else 8
    if top_n < 1:
        top_n = 1

    competitor = Competitor.query.get(competitor_id)
    if not competitor or competitor.competition_id != competition_id:
        return

    rows = (
        db.session.query(
            Score.attempts,
            Score.topped,
            Score.updated_at,
            Score.section_climb_id,
            SectionClimb.base_points,
        )
        .join(SectionClimb, SectionClimb.id == Score.section_climb_id)
        .join(Section, Section.id == SectionClimb.section_id)
        .filter(
            Score.competitor_id == competitor_id,
            Section.competition_id == competition_id,
        )
        .all()
    )

    topped_scored = []
    last_update = None

    for row in rows:
        if row.updated_at is not None:
            if last_update is None or row.updated_at > last_update:
                last_update = row.updated_at

        if not bool(row.topped):
            continue

        topped_scored.append({
            "points": int(row.base_points or 0),
            "attempts": int(row.attempts or 0),
            "section_climb_id": int(row.section_climb_id or 0),
        })

    topped_scored.sort(
    key=lambda climb: (-climb["points"], climb["attempts"], climb["section_climb_id"])
    )

    top_rows = topped_scored[:top_n]

    total_points = sum(climb["points"] for climb in top_rows)
    attempts_on_tops = sum(climb["attempts"] for climb in top_rows)
    tops = len(top_rows)

    leaderboard_row = Leaderboard.query.filter_by(
        competitor_id=competitor_id,
        competition_id=competition_id,
    ).first()

    if not leaderboard_row:
        leaderboard_row = Leaderboard(
            competitor_id=competitor_id,
            competition_id=competition_id,
        )
        db.session.add(leaderboard_row)

    leaderboard_row.total_points = total_points
    leaderboard_row.attempts_on_tops = attempts_on_tops
    leaderboard_row.tops = tops
    leaderboard_row.last_update = last_update