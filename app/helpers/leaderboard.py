import re
import json
import time
from functools import wraps
from datetime import datetime
from flask import session, flash, redirect, abort

from app.extensions import db
from app.models import Competition, Competitor, Gym, Score, DoublesTeam
from app.helpers.competition import get_current_comp
from app.helpers.scoring import points_for, normalize_leaderboard_category

# --- In-memory leaderboard cache ---
LEADERBOARD_CACHE = {}
LEADERBOARD_CACHE_TTL = 60  # seconds


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
            if comp:
                # keep session in sync for nav consistency
                if comp.slug:
                    session["active_comp_slug"] = comp.slug
                return comp

    return None


def slugify(name: str) -> str:
    """Create URL friendly string ("The Slab" -> "the-slab")"""
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "section"

def _parse_boundary_points(raw) -> list[dict]:
    """
    Accepts:
      - list of dicts [{"x":..,"y":..}, ...]
      - JSON string of that list
    Returns cleaned list with floats clamped 0..100.
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

        # clamp to 0..100 (since we're storing % coords)
        x = max(0.0, min(100.0, x))
        y = max(0.0, min(100.0, y))
        cleaned.append({"x": x, "y": y})

    return cleaned


def _boundary_to_json(points: list[dict]) -> str:
    return json.dumps(points, separators=(",", ":"))


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


def admin_can_manage_competition(comp) -> bool:
    if comp is None:
        return False
    return admin_can_manage_gym(comp.gym)

def comp_is_finished(comp) -> bool:
    """True if comp has an end_at and it is in the past (UTC naive)."""
    if not comp:
        return True
    if comp.end_at is None:
        return False
    return datetime.utcnow() >= comp.end_at


def comp_is_live(comp) -> bool:
    """
    True only when the comp is active AND has started AND has not ended.
    If start_at is missing, we treat it as NOT live (prevents 'always live' comps).
    If end_at is missing, we treat it as live from start_at onward (optional).
    """
    if not comp or not comp.is_active:
        return False

    now = datetime.utcnow()

    # IMPORTANT: start time must exist, otherwise the comp is not considered live.
    if comp.start_at is None:
        return False

    if comp.start_at > now:
        return False

    # If end_at missing, allow "open ended" comps once started
    if comp.end_at is not None and comp.end_at < now:
        return False

    return True


def deny_if_comp_finished(comp, redirect_to=None, message=None):
    """
    Return a redirect response if finished, otherwise None.
    """
    if comp_is_finished(comp):
        flash(message or "That competition has finished — scoring and stats are locked.", "warning")
        return redirect(redirect_to or "/my-comps")
    return None

def finished_guard(get_comp_func, redirect_builder=None, message=None):
    """
    Decorator that blocks route access if the resolved comp is finished.
    - get_comp_func(*args, **kwargs) -> Competition
    - redirect_builder(comp, *args, **kwargs) -> url string (optional)
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

def _normalise_category_key(category):
    """Normalise the category argument into a cache key."""
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
    Build leaderboard rows.

    Modes:
    - Singles (default): All / Male / Female / Gender Inclusive
      Returns rows shaped like:
        {
          "competitor_id", "name", "gender",
          "tops", "attempts_on_tops",
          "total_points", "last_update",
          "position"
        }

    - Doubles (category == "doubles"):
      Returns rows shaped like:
        {
          "team_id",
          "a_id", "b_id",
          "a_name", "b_name",
          "name",              # "A + B"
          "total_points",
          "position"
        }

    Scoping:
    - If competition_id is provided -> use that competition
    - Else if slug is provided -> look up that competition by slug
    - Else -> fall back to get_current_comp()

    Cache is per (competition_id, category_key).
    """

    # --- resolve competition scope ---
    current_comp = None

    if competition_id:
        current_comp = Competition.query.get(competition_id)
    elif slug:
        current_comp = Competition.query.filter_by(slug=slug).first()
    else:
        current_comp = get_current_comp()

    if not current_comp:
        return [], "No active competition"

    # --- cache lookup (scoped per competition + category) ---
    cat_key = _normalise_category_key(category)
    cache_key = (current_comp.id, cat_key)

    now = time.time()
    cached = LEADERBOARD_CACHE.get(cache_key)
    if cached:
        rows, category_label, ts = cached
        if now - ts <= LEADERBOARD_CACHE_TTL:
            return rows, category_label

    # --- detect doubles mode early ---
    norm = (category or "").strip().lower()
    is_doubles = norm.startswith("doub")  # matches "doubles"

    if is_doubles:
        # Build singles totals once (All) so doubles can sum partner points
        singles_rows, _ = build_leaderboard(None, competition_id=current_comp.id)

        points_by_id = {r["competitor_id"]: r["total_points"] for r in singles_rows}
        name_by_id = {r["competitor_id"]: r["name"] for r in singles_rows}

        teams = DoublesTeam.query.filter_by(competition_id=current_comp.id).all()
        if not teams:
            rows = []
            category_label = "Doubles"
            LEADERBOARD_CACHE[cache_key] = (rows, category_label, now)
            return rows, category_label

        rows = []
        for t in teams:
            a_id = t.competitor_a_id
            b_id = t.competitor_b_id

            a_name = name_by_id.get(a_id, f"#{a_id}")
            b_name = name_by_id.get(b_id, f"#{b_id}")

            total_points = points_by_id.get(a_id, 0) + points_by_id.get(b_id, 0)

            rows.append(
                {
                    "team_id": t.id,
                    "a_id": a_id,
                    "b_id": b_id,
                    "a_name": a_name,
                    "b_name": b_name,
                    "name": f"{a_name} + {b_name}",
                    "total_points": total_points,
                }
            )

        # Sort: points desc, then stable name tie-break
        rows.sort(key=lambda r: (-r["total_points"], r["name"]))

        # Assign positions with ties sharing the same place
        pos = 0
        prev_key = None
        for row in rows:
            k = (row["total_points"],)
            if k != prev_key:
                pos += 1
            prev_key = k
            row["position"] = pos

        category_label = "Doubles"
        LEADERBOARD_CACHE[cache_key] = (rows, category_label, now)
        return rows, category_label

    # --- singles mode (existing logic) ---
    q = Competitor.query.filter(Competitor.competition_id == current_comp.id)
    category_label = "All"

    cat = normalize_leaderboard_category(category)
    
    if cat == "male":
        q = q.filter(Competitor.gender == "Male")
        category_label = "Male"
    elif cat == "female":
        q = q.filter(Competitor.gender == "Female")
        category_label = "Female"
    elif cat == "inclusive":
        q = q.filter(Competitor.gender == "Inclusive")
        category_label = "Gender Inclusive"
    else:
        category_label = "All"

    competitors = q.all()
    if not competitors:
        rows = []
        LEADERBOARD_CACHE[cache_key] = (rows, category_label, now)
        return rows, category_label

    competitor_ids = [c.id for c in competitors]

    all_scores = (
        Score.query
        .filter(Score.competitor_id.in_(competitor_ids))
        .all()
        if competitor_ids else []
    )

    by_competitor = {}
    for s in all_scores:
        by_competitor.setdefault(s.competitor_id, []).append(s)

    rows = []
    for c in competitors:
        scores = by_competitor.get(c.id, [])

        tops = sum(1 for s in scores if s.topped)
        attempts_on_tops = sum(s.attempts for s in scores if s.topped)

        total_points = sum(
            points_for(s.climb_number, s.attempts, s.topped, current_comp.id)
            for s in scores
        )

        last_update = None
        if scores:
            last_update = max(
                (s.updated_at for s in scores if s.updated_at is not None),
                default=None
            )

        rows.append(
            {
                "competitor_id": c.id,
                "name": c.name,
                "gender": c.gender,
                "tops": tops,
                "attempts_on_tops": attempts_on_tops,
                "total_points": total_points,
                "last_update": last_update,
            }
        )

    rows.sort(key=lambda r: (-r["total_points"], -r["tops"], r["attempts_on_tops"]))

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


def build_doubles_leaderboard(competition_id):
    teams = DoublesTeam.query.filter_by(competition_id=competition_id).all()
    if not teams:
        return [], "Doubles"

    # First get singles leaderboard rows so we know points
    singles_rows, _ = build_leaderboard(None, competition_id=competition_id)

    points_by_id = {r["competitor_id"]: r["total_points"] for r in singles_rows}

    rows = []
    for team in teams:
        a = Competitor.query.get(team.competitor_a_id)
        b = Competitor.query.get(team.competitor_b_id)

        total_points = (
            points_by_id.get(team.competitor_a_id, 0)
            + points_by_id.get(team.competitor_b_id, 0)
        )

        rows.append({
            "team_id": team.id,
            "name": f"{a.name} + {b.name}",
            "total_points": total_points,
        })

    # sort descending by total points
    rows.sort(key=lambda r: -r["total_points"])

    # assign positions
    pos = 0
    prev_pts = None
    for row in rows:
        if row["total_points"] != prev_pts:
            pos += 1
        prev_pts = row["total_points"]
        row["position"] = pos

    return rows, "Doubles"


def build_doubles_rows(singles_rows, competition_id: int):
    """
    Build doubles leaderboard rows from:
    - singles_rows: output from build_leaderboard(...) (already category-filtered)
    - competition_id: current competition scope

    Filtering rule:
    - If the leaderboard is category-filtered (Male/Female/Inclusive), singles_rows will only include those competitors.
      We only include doubles teams where BOTH partners are in singles_rows.
    """

    # competitor_id -> total_points + name lookup (from the already-scoped singles leaderboard)
    totals_by_id = {r["competitor_id"]: r["total_points"] for r in singles_rows}
    name_by_id = {r["competitor_id"]: r["name"] for r in singles_rows}

    teams = DoublesTeam.query.filter_by(competition_id=competition_id).all()

    doubles_rows = []
    for t in teams:
        a_id = t.competitor_a_id
        b_id = t.competitor_b_id

        # Only include teams where BOTH partners are in the current singles_rows scope
        # (so category leaderboards behave sensibly)
        if a_id not in totals_by_id or b_id not in totals_by_id:
            continue

        a_pts = totals_by_id.get(a_id, 0)
        b_pts = totals_by_id.get(b_id, 0)

        doubles_rows.append({
            "team_id": t.id,
            "a_id": a_id,
            "b_id": b_id,
            "a_name": name_by_id.get(a_id, f"#{a_id}"),
            "b_name": name_by_id.get(b_id, f"#{b_id}"),
            "total_points": a_pts + b_pts,
        })

    # sort by total desc
    doubles_rows.sort(key=lambda r: (-r["total_points"], r["a_name"], r["b_name"]))

    # assign positions with ties sharing the same place
    pos = 0
    prev = None
    for r in doubles_rows:
        k = (r["total_points"],)
        if k != prev:
            pos += 1
        prev = k
        r["position"] = pos

    return doubles_rows
