import re
import json
import time
from functools import wraps
from datetime import datetime
from flask import session, flash, redirect, abort
from app.models import Competition, Competitor, Gym, Score
from app.helpers.competition import get_current_comp
from app.helpers.scoring import points_for

# --- In-memory leaderboard cache ---
LEADERBOARD_CACHE = {}
LEADERBOARD_CACHE_TTL = 60  # seconds


# --- Viewer / competition helpers ---

def get_viewer_comp():
    """
    Resolve a competition context for the current logged-in viewer.

    Priority:
    1) session["active_comp_slug"] if it exists and is valid
    2) viewer's competitor.competition_id
    """
    slug = (session.get("active_comp_slug") or "").strip()
    if slug:
        comp = Competition.query.filter_by(slug=slug).first()
        if comp:
            return comp

    viewer_id = session.get("competitor_id")
    if viewer_id:
        competitor = Competitor.query.get(viewer_id)
        if competitor and competitor.competition_id:
            comp = Competition.query.get(competitor.competition_id)
            if comp and comp.slug:
                session["active_comp_slug"] = comp.slug
                return comp

    return None


# --- Gym / slug helpers ---

def slugify(name: str) -> str:
    """Create URL friendly string ("The Slab" -> "the-slab")"""
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "section"


def _parse_boundary_points(raw) -> list[dict]:
    """
    Accepts list of dicts or JSON string, returns cleaned list with floats clamped 0..100.
    """
    if raw is None:
        return []

    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return []
        try:
            raw = json.loads(raw)
        except Exception:
            return []

    if not isinstance(raw, list):
        return []

    cleaned = []
    for p in raw:
        if not isinstance(p, dict):
            continue
        try:
            x = float(p.get("x"))
            y = float(p.get("y"))
        except Exception:
            continue
        x = max(0.0, min(100.0, x))
        y = max(0.0, min(100.0, y))
        cleaned.append({"x": x, "y": y})
    return cleaned


def _boundary_to_json(points: list[dict]) -> str:
    return json.dumps(points, separators=(",", ":"))


def get_or_create_gym_by_name(name: str):
    """Return existing Gym by slug or create a new one (does not commit)."""
    clean = (name or "").strip()
    if not clean:
        return None

    slug_val = slugify(clean)
    gym = Gym.query.filter_by(slug=slug_val).first()
    if gym:
        return gym

    gym = Gym(name=clean, slug=slug_val)
    from app.extensions import db
    db.session.add(gym)
    return gym


def get_gym_map_url_for_competition(comp):
    if not comp or not comp.gym:
        return None
    return comp.gym.map_image_path


# --- Admin / permissions helpers ---

def get_session_admin_gym_ids():
    raw = session.get("admin_gym_ids")
    if not raw:
        return set()
    try:
        return {int(x) for x in raw if x is not None}
    except Exception:
        return set()


def admin_is_super():
    return bool(session.get("admin_is_super"))


def admin_can_manage_gym(gym) -> bool:
    if not gym:
        return False
    if session.get("admin_is_super"):
        return True
    gym_ids = session.get("admin_gym_ids") or []
    return gym.id in gym_ids


def admin_can_manage_competition(comp) -> bool:
    if comp is None:
        return False
    return admin_can_manage_gym(comp.gym)


# --- Competition status helpers ---

def comp_is_finished(comp) -> bool:
    if not comp:
        return True
    if comp.end_at is None:
        return False
    return datetime.utcnow() >= comp.end_at


def comp_is_live(comp) -> bool:
    if not comp or not comp.is_active:
        return False

    now = datetime.utcnow()
    if comp.start_at is None or comp.start_at > now:
        return False
    if comp.end_at is not None and comp.end_at < now:
        return False
    return True


def deny_if_comp_finished(comp, redirect_to=None, message=None):
    if comp_is_finished(comp):
        flash(message or "That competition has finished — scoring and stats are locked.", "warning")
        return redirect(redirect_to or "/my-comps")
    return None


def finished_guard(get_comp_func, redirect_builder=None, message=None):
    """
    Decorator that blocks route access if the resolved comp is finished.
    """
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            comp = get_comp_func(*args, **kwargs)
            if not comp:
                abort(404)

            if comp_is_finished(comp):
                to = redirect_builder(comp, *args, **kwargs) if redirect_builder else "/my-comps"
                flash(message or "That competition has finished — scoring and stats are locked.", "warning")
                return redirect(to)

            return view(*args, **kwargs)
        return wrapped
    return decorator


# --- Leaderboard helpers ---

def _normalise_category_key(category):
    if not category:
        return "all"
    norm = category.strip().lower()
    if norm.startswith("m"):
        return "male"
    if norm.startswith("f"):
        return "female"
    return "inclusive"


def build_leaderboard(category=None, competition_id=None, slug=None):
    """
    Build leaderboard rows, optionally filtered by gender category.
    Returns (rows, category_label)
    """
    # Resolve competition
    current_comp = None
    if competition_id:
        current_comp = Competition.query.get(competition_id)
    elif slug:
        current_comp = Competition.query.filter_by(slug=slug).first()
    else:
        current_comp = get_current_comp()

    if not current_comp:
        return [], "No active competition"

    # Cache lookup
    cat_key = _normalise_category_key(category)
    cache_key = (current_comp.id, cat_key)
    now = time.time()
    cached = LEADERBOARD_CACHE.get(cache_key)
    if cached:
        rows, category_label, ts = cached
        if now - ts <= LEADERBOARD_CACHE_TTL:
            return rows, category_label

    # Base query
    q = Competitor.query.filter(Competitor.competition_id == current_comp.id)
    category_label = "All"
    if category:
        norm = category.strip().lower()
        if norm.startswith("m"):
            q = q.filter(Competitor.gender == "Male")
            category_label = "Male"
        elif norm.startswith("f"):
            q = q.filter(Competitor.gender == "Female")
            category_label = "Female"
        else:
            q = q.filter(Competitor.gender == "Inclusive")
            category_label = "Gender Inclusive"

    competitors = q.all()
    if not competitors:
        LEADERBOARD_CACHE[cache_key] = ([], category_label, now)
        return [], category_label

    competitor_ids = [c.id for c in competitors]
    all_scores = Score.query.filter(Score.competitor_id.in_(competitor_ids)).all() if competitor_ids else []

    by_competitor = {}
    for s in all_scores:
        by_competitor.setdefault(s.competitor_id, []).append(s)

    rows = []
    for c in competitors:
        scores = by_competitor.get(c.id, [])
        tops = sum(1 for s in scores if s.topped)
        attempts_on_tops = sum(s.attempts for s in scores if s.topped)
        total_points = sum(points_for(s.climb_number, s.attempts, s.topped, current_comp.id) for s in scores)
        last_update = max((s.updated_at for s in scores if s.updated_at is not None), default=None) if scores else None
        rows.append({
            "competitor_id": c.id,
            "name": c.name,
            "gender": c.gender,
            "tops": tops,
            "attempts_on_tops": attempts_on_tops,
            "total_points": total_points,
            "last_update": last_update
        })

    rows.sort(key=lambda r: (-r["total_points"], -r["tops"], r["attempts_on_tops"]))

    # assign positions
    pos = 0
    prev_key = None
    for row in rows:
        k = (row["total_points"], row["tops"], row["attempts_on_tops"])
        if k != prev_key:
            pos += 1
        prev_key = k
        row["position"] = pos

    LEADERBOARD_CACHE[cache_key] = (rows, category_label, now)
    return rows, category_label
