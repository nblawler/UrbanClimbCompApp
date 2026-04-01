from app.extensions import db
from app.models import Competitor, Score, Section, SectionClimb, Leaderboard


def normalize_leaderboard_category(category):
    """
    Ensure category is one of the allowed values.
    Falls back to 'all' if anything unexpected comes in.
    """
    value = (category or "all").strip().lower()
    if value in {"all", "male", "female", "inclusive"}:
        return value
    return "all"


def refresh_leaderboard_row(competitor_id: int, competition_id: int, top_n: int):
    """
    Recalculate ONE competitor's leaderboard row for ONE competition.

    Important:
    - Pulls ALL scores for this competitor in this competition
    - Filters to topped climbs only
    - Sorts by points descending, then attempts ascending
    - Takes top N climbs
    - Writes the summary into the Leaderboard table
    """
    # Default to top 8 climbs if top_n is missing
    top_n = int(top_n) if top_n is not None else 8
    if top_n < 1:
        top_n = 1

    # Make sure the competitor exists and belongs to this competition
    competitor = Competitor.query.get(competitor_id)
    if not competitor or competitor.competition_id != competition_id:
        return

    # Get all score records for this competitor in this competition,
    # including the base_points for each climb
    score_records = (
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

    # Loop through all score records
    for score_record in score_records:
        # Track the most recent score update time
        if score_record.updated_at is not None:
            if last_update is None or score_record.updated_at > last_update:
                last_update = score_record.updated_at

        # Only topped climbs count toward the leaderboard
        if not bool(score_record.topped):
            continue

        # Store the data needed for sorting and totals
        topped_scored.append({
            "points": int(score_record.base_points or 0),
            "attempts": int(score_record.attempts or 0),
            "section_climb_id": int(score_record.section_climb_id or 0),
        })

    # Sort by:
    # 1. highest points first
    # 2. lowest attempts next
    # 3. section_climb_id for stable ordering
    topped_scored.sort(
        key=lambda climb_score: (
            -climb_score["points"],
            climb_score["attempts"],
            climb_score["section_climb_id"],
        )
    )

    # Keep only the top N climbs
    top_rows = topped_scored[:top_n]

    # Calculate final leaderboard values
    total_points = sum(climb_score["points"] for climb_score in top_rows)
    attempts_on_tops = sum(climb_score["attempts"] for climb_score in top_rows)
    tops = len(top_rows)

    # Find the existing leaderboard row for this competitor + competition
    leaderboard_row = Leaderboard.query.filter_by(
        competitor_id=competitor_id,
        competition_id=competition_id,
    ).first()

    # If it does not exist yet, create it
    if not leaderboard_row:
        leaderboard_row = Leaderboard(
            competitor_id=competitor_id,
            competition_id=competition_id,
        )
        db.session.add(leaderboard_row)

    # Update the leaderboard row with the recalculated totals
    leaderboard_row.total_points = total_points
    leaderboard_row.attempts_on_tops = attempts_on_tops
    leaderboard_row.tops = tops
    leaderboard_row.last_update = last_update