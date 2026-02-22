from flask import session

from app.models import Account, GymAdmin
from app.helpers.email import normalize_email

def admin_is_super():
    """
    True if this admin is a global/super admin (can manage all gyms).
    """
    return bool(session.get("admin_is_super"))

def admin_can_manage_gym(gym) -> bool:
    if not gym:
        return False

    # Super-admin can manage all gyms
    if session.get("admin_is_super"):
        return True

    gym_ids = session.get("admin_gym_ids") or []
    return gym.id in gym_ids

def admin_can_manage_gym_id(gym_id: int) -> bool:
    if session.get("admin_is_super"):
        return True

    allowed_ids = session.get("admin_gym_ids") or []
    try:
        allowed_ids = [int(x) for x in allowed_ids]
    except Exception:
        allowed_ids = []

    return int(gym_id) in allowed_ids

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

    # Always reset admin session first (prevents stale perms)
    session["admin_ok"] = False
    session["admin_is_super"] = False
    session["admin_gym_ids"] = []
    session.pop("admin_comp_id", None)

    if not email:
        return

    # Super admin (password-based) stays separate — don't set here.
    # This function is for "gym admin by membership".
    acct = Account.query.filter_by(email=email).first()
    if not acct:
        return

    gym_ids = [
        ga.gym_id
        for ga in GymAdmin.query.filter_by(account_id=acct.id).all()
        if ga.gym_id is not None
    ]

    if gym_ids:
        session["admin_ok"] = True
        session["admin_is_super"] = False
        session["admin_gym_ids"] = sorted(list(set(gym_ids)))

    # If there was an admin_comp_id previously, only keep it if allowed
    # (we popped it above, so nothing to do here unless you want to restore it safely later)

def admin_can_manage_competition(comp) -> bool:
    if comp is None:
        return False
    return admin_can_manage_gym(comp.gym)
