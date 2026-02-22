from app.models import Competitor, Competition, SectionClimb, Section, Score
from app.helpers.competition import get_current_comp

def points_for(climb_number, attempts, topped, competition_id=None):
    """
    Calculate points for a climb using ONLY DB config, scoped to a competition.

    If competition_id is None, we fall back to the current active competition.
    """
    if not topped:
        return 0

    # sanity-clamp attempts recorded
    if attempts < 1:
        attempts = 1
    elif attempts > 50:
        attempts = 50

    # Resolve competition scope
    comp = None
    if competition_id:
        comp = Competition.query.get(competition_id)
    else:
        comp = get_current_comp()

    if not comp:
        # No competition context = no reliable scoring config
        return 0

    # Per-climb config must exist in DB for THIS competition
    q = (
        SectionClimb.query
        .join(Section, Section.id == SectionClimb.section_id)
        .filter(
            SectionClimb.climb_number == climb_number,
            Section.competition_id == comp.id,
        )
    )

    # Optional extra safety: ensure gym matches too (if you’re populating gym_id everywhere)
    if comp.gym_id:
        q = q.filter(SectionClimb.gym_id == comp.gym_id)

    sc = q.first()

    if not sc or sc.base_points is None or sc.penalty_per_attempt is None:
        return 0

    base = sc.base_points
    penalty = sc.penalty_per_attempt
    cap = sc.attempt_cap if sc.attempt_cap and sc.attempt_cap > 0 else 5

    # only attempts from 2 onward incur penalty; cap at `cap`
    penalty_attempts = max(0, min(attempts, cap) - 1)

    return max(int(base - penalty * penalty_attempts), 0)
    
def competitor_total_points(comp_id: int, competition_id=None) -> int:
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
        # Fallback: old behaviour
        scores = Score.query.filter_by(competitor_id=comp_id).all()

    return sum(
        points_for(s.climb_number, s.attempts, s.topped, competition_id)
        for s in scores
    )
