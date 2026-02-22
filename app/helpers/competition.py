from flask import session
from datetime import datetime, timezone
from app.models import Competition
import secrets
import hashlib

def set_pending_join(comp_slug: str, email: str, name: str, gender: str):
    session["pending_join"] = True
    session["pending_join_slug"] = (comp_slug or "").strip()
    session["pending_join_email"] = (email or "").strip().lower()
    session["pending_join_name"] = (name or "").strip()
    session["pending_join_gender"] = (gender or "").strip()

def clear_pending_join():
    session.pop("pending_join", None)
    session.pop("pending_join_slug", None)
    session.pop("pending_join_email", None)
    session.pop("pending_join_name", None)
    session.pop("pending_join_gender", None)

def has_pending_join() -> bool:
    return bool(session.get("pending_join"))

def admin_can_manage_gym_id(gym_id: int) -> bool:
    if session.get("admin_is_super"):
        return True

    allowed_ids = session.get("admin_gym_ids") or []
    try:
        allowed_ids = [int(x) for x in allowed_ids]
    except Exception:
        allowed_ids = []

    return int(gym_id) in allowed_ids

def gym_map_for(gym_name: str) -> str:
    name = (gym_name or "").lower()
    if "adelaide" in name:
        return "Adelaide_Gym_Map.png"
    return "Collingwood_Gym_Map.png"

def get_current_comp():
    """
    Return the single active competition, but NEVER return comps that have ended.
    """
    now = datetime.utcnow()

    return (
        Competition.query
        .filter(
            Competition.is_active == True,
            (Competition.end_at == None) | (Competition.end_at >= now),
        )
        .order_by(Competition.start_at.asc().nullsfirst())
        .first()
    )

def get_comp_or_404(slug: str) -> Competition:
    """
    Look up a competition by slug.
    For now we allow any slug; later you can restrict to is_active=True.
    """
    comp = Competition.query.filter_by(slug=slug).first_or_404()
    return comp

def utcnow():
    return datetime.now(timezone.utc)

def make_token() -> str:
    return secrets.token_urlsafe(32)

def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()