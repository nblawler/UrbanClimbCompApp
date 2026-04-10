from datetime import datetime, timezone
from app.extensions import db
from app.models import Competitor, Score, Section, SectionClimb, Leaderboard, DoublesLeaderboard, DoublesTeam


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
    
def refresh_doubles_leaderboard_row(competitor_id: int, competition_id: int, top_n: int = 8):
    """
    Find every doubles team this competitor belongs to in this competition
    and recompute that team's DoublesLeaderboard row.
    """
    teams = DoublesTeam.query.filter(
        DoublesTeam.competition_id == competition_id,
        db.or_(
            DoublesTeam.competitor_a_id == competitor_id,
            DoublesTeam.competitor_b_id == competitor_id,
        )
    ).all()

    if not teams:
        return

    for team in teams:
        _recompute_doubles_row(team, competition_id, top_n)


def _recompute_doubles_row(team: DoublesTeam, competition_id: int, top_n: int = 8):
    """Recompute and persist one DoublesLeaderboard row."""

    def member_data(cid):
        comp = Competitor.query.get(cid)
        lb   = Leaderboard.query.filter_by(
            competitor_id=cid,
            competition_id=competition_id,
        ).first()

        points   = int(lb.total_points     or 0) if lb else 0
        attempts = int(lb.attempts_on_tops or 0) if lb else 0

        climbs = (
            db.session.query(
                SectionClimb.climb_number,
                Score.attempts,
                SectionClimb.base_points,
            )
            .join(Score, Score.section_climb_id == SectionClimb.id)
            .join(Section, Section.id == SectionClimb.section_id)
            .filter(
                Score.competitor_id == cid,
                Section.competition_id == competition_id,
                Score.topped == True,
            )
            .order_by(SectionClimb.base_points.desc(), Score.attempts.asc())
            .limit(top_n)
            .all()
        )

        climb_list = [
            {
                "label":    f"Climb {r.climb_number}",
                "attempts": r.attempts,
                "score":    r.base_points,
            }
            for r in climbs
        ]

        return comp.name if comp else f"#{cid}", points, attempts, climb_list

    a_name, a_pts, a_att, a_climbs = member_data(team.competitor_a_id)
    b_name, b_pts, b_att, b_climbs = member_data(team.competitor_b_id)

    row = DoublesLeaderboard.query.filter_by(
        team_id=team.id,
        competition_id=competition_id,
    ).first()

    if not row:
        row = DoublesLeaderboard(
            team_id=team.id,
            competition_id=competition_id,
        )
        db.session.add(row)

    row.total_points     = a_pts + b_pts
    row.attempts_on_tops = a_att + b_att
    row.a_id     = team.competitor_a_id
    row.a_name   = a_name
    row.a_climbs = a_climbs
    row.b_id     = team.competitor_b_id
    row.b_name   = b_name
    row.b_climbs = b_climbs
    row.last_update = datetime.now(timezone.utc)