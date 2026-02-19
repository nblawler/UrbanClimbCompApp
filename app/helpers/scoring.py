from typing import Optional
from flask import session
from datetime import datetime
from app.extensions import db
from app.models import Account, Competitor, GymAdmin, Competition, SectionClimb, Section, Score
from app.helpers.competition import get_current_comp
from app.helpers.email import normalize_email

# --- Scoring function ---

def points_for(climb_number: int, attempts: int, topped: bool, competition_id: Optional[int] = None) -> int:
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
    comp = Competition.query.get(competition_id) if competition_id else get_current_comp()
    if not comp:
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

    if comp.gym_id:
        q = q.filter(SectionClimb.gym_id == comp.gym_id)

    sc = q.first()
    if not sc or sc.base_points is None or sc.penalty_per_attempt is None:
        return 0

    base = sc.base_points
    penalty = sc.penalty_per_attempt
    cap = sc.attempt_cap if sc.attempt_cap and sc.attempt_cap > 0 else 5

    penalty_attempts = max(0, min(attempts, cap) - 1)
    return max(int(base - penalty * penalty_attempts), 0)


# --- Account / session helpers ---

def get_or_create_account_for_email(email: str) -> Account:
    email = normalize_email(email)
    if not email:
        raise ValueError("email required")

    acct = Account.query.filter_by(email=email).first()
    if acct:
        return acct

    acct = Account(email=email)
    db.session.add(acct)
    db.session.commit()
    return acct


def get_account_for_session() -> Optional[Account]:
    account_id = session.get("account_id")
    if account_id:
        acct = Account.query.get(account_id)
        if acct:
            return acct

    email = normalize_email(session.get("competitor_email"))
    if not email:
        return None
    return Account.query.filter_by(email=email).first()


def get_admin_gym_ids_for_email(email: str) -> list[int]:
    email = normalize_email(email)
    if not email:
        return []

    acct = Account.query.filter_by(email=email).first()
    if not acct:
        return []

    return [ga.gym_id for ga in GymAdmin.query.filter_by(account_id=acct.id).all()]
    

def establish_gym_admin_session_for_email(email: str) -> None:
    """
    Single source of truth for admin session flags.
    Uses Account + GymAdmin.account_id (stable even if comp competitors are deleted).
    Also clears stale admin_comp_id if the admin can't manage it anymore.
    """
    email = normalize_email(email)

    # Reset admin session
    session["admin_ok"] = False
    session["admin_is_super"] = False
    session["admin_gym_ids"] = []
    session.pop("admin_comp_id", None)

    if not email:
        return
    
def competitor_total_points(comp_id: int, competition_id: Optional[int] = None) -> int:
    """
    Compute total points for a competitor in a specific competition (or all if competition_id is None).
    """
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
        scores = Score.query.filter_by(competitor_id=comp_id).all()

    return sum(
        points_for(s.climb_number, s.attempts, s.topped, competition_id)
        for s in scores
    )
