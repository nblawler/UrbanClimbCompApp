from flask import session

from app.extensions import db
from app.models import Gym
from app.helpers.url import slugify

def gym_map_for(gym_name: str) -> str:
    name = (gym_name or "").lower()
    if "adelaide" in name:
        return "Adelaide_Gym_Map.png"
    return "Collingwood_Gym_Map.png"

def get_or_create_gym_by_name(name: str):
    """
    Lightweight helper used by the admin 'create competition' form.

    - Normalises name -> slug
    - Reuses an existing Gym row if the slug already exists
    - Otherwise creates a new Gym with that name
    """
    clean = (name or "").strip()
    if not clean:
        return None

    slug_val = slugify(clean)
    gym = Gym.query.filter_by(slug=slug_val).first()
    if gym:
        return gym

    gym = Gym(
        name=clean,
        slug=slug_val,
        # map_image_path can be filled later via DB or a future UI
    )
    db.session.add(gym)
    # NOTE: caller is responsible for committing; they may also
    # be creating a Competition in the same transaction.
    return gym

def get_gym_map_url_for_competition(comp):
    """
    Return the map image URL for a competition's gym.
    """
    if not comp or not comp.gym:
        return None

    return comp.gym.map_image_path

def get_session_admin_gym_ids():
    """
    Return a set of gym_ids this admin is allowed to manage
    (for non-super admins). Stored in the session as a list.
    """
    raw = session.get("admin_gym_ids")
    if not raw:
        return set()
    try:
        return {int(x) for x in raw if x is not None}
    except Exception:
        return set()
