# app/helpers/leaderboard.py

import time
from typing import Optional
from collections import defaultdict

from app.models import Competition, Competitor, Score, DoublesTeam, SectionClimb, Section, Leaderboard
from app.extensions import db
from app.helpers.competition import get_current_comp
from app.helpers.new_leaderboard import normalize_leaderboard_category
from app.helpers.competition import get_current_comp
from app.helpers.scoring import points_for
from app.helpers.leaderboard_cache import LEADERBOARD_CACHE, LEADERBOARD_CACHE_TTL


def normalise_category_key(category):
    """Normalise the category argument into a cache key. (legacy helper)"""
    if not category:
        return "all"
    norm = category.strip().lower()
    if norm.startswith("m"):
        return "male"
    if norm.startswith("f"):
        return "female"
    return "inclusive"


def normalize_leaderboard_category(raw: Optional[str]) -> Optional[str]:
    """
    Normalizes incoming category strings into:
      None (meaning "all singles"),
      "male", "female", "inclusive",
      or "doubles".
    """
    if not raw:
        return None
    k = (raw or "").strip().lower()

    if k in ("all", "overall", "singles", "none"):
        return None
    if k in ("m", "male", "men"):
        return "male"
    if k in ("f", "female", "women"):
        return "female"
    if k in ("i", "incl", "inclusive", "genderinclusive", "gender-inclusive", "gender_inclusive"):
        return "inclusive"
    if k in ("d", "double", "doubles", "team", "teams"):
        return "doubles"

    # unknown category -> treat like "all"
    return None


def _safe_label_from_section_climb(sc: Optional[SectionClimb]) -> Optional[str]:
    """
    Best-effort label builder. We don't know your exact display fields,
    so we try a few common ones and fall back later.
    """
    if not sc:
        return None

    if hasattr(sc, "label") and getattr(sc, "label"):
        return getattr(sc, "label")
    if hasattr(sc, "name") and getattr(sc, "name"):
        return getattr(sc, "name")

    if hasattr(sc, "color") and getattr(sc, "color"):
        try:
            return f"{getattr(sc, 'color')} #{sc.climb_number}"
        except Exception:
            return str(getattr(sc, "color"))

    return None


def get_top_climbs_for_competitor(competition_id: int, competitor_id: int, limit: int = 8):
    """
    Return the competitor's top N climbs using the SAME selection rule as build_leaderboard:
      - ONLY include topped climbs
      - points_for(climb_number, attempts, topped, competition_id)
      - sort by points desc, attempts asc
      - take top N

    Uses SectionClimb.colour for labels like "Yellow #12".

    Output:
      [
        {
          "section_climb_id": 123,
          "climb_number": 12,
          "colour": "Yellow",
          "label": "Yellow #12",
          "attempts": 4,
          "topped": True,
          "score": 1012,
          "updated_at": "..." | None
        },
        ...
      ]
    """

    scores = Score.query.filter_by(competitor_id=competitor_id).all()
    if not scores:
        return []

    # Map section_climb_id -> SectionClimb, scoped to this competition (safety)
    sc_ids = [s.section_climb_id for s in scores if s.section_climb_id]
    sc_map = {}
    if sc_ids:
        sc_rows = (
            SectionClimb.query
            .join(Section, Section.id == SectionClimb.section_id)
            .filter(
                SectionClimb.id.in_(sc_ids),
                Section.competition_id == competition_id,
            )
            .all()
        )
        sc_map = {sc.id: sc for sc in sc_rows}

    scored = []
    for s in scores:
        # ignore cross-comp or missing sc rows
        sc = sc_map.get(s.section_climb_id)
        if s.section_climb_id and sc is None:
            continue

        # IMPORTANT: leaderboard details should only show topped climbs
        if not bool(s.topped):
            continue

        pts = points_for(s.climb_number, s.attempts, s.topped, competition_id)

        colour = (sc.colour.strip() if (sc and sc.colour) else None)
        if colour:
            label = f"{colour} #{s.climb_number}"
        else:
            label = f"Climb #{s.climb_number}"

        scored.append({
            "section_climb_id": s.section_climb_id,
            "climb_number": s.climb_number,
            "colour": colour,
            "label": label,
            "attempts": int(s.attempts or 0),
            "topped": True,
            "score": int(pts or 0),
            "updated_at": s.updated_at.isoformat() if getattr(s, "updated_at", None) else None,
        })

    scored.sort(key=lambda x: (-x["score"], x["attempts"], x["climb_number"] or 0))
    return scored[:limit]


def build_leaderboard(category=None, competition_id=None, slug=None):
    """
    Fast singles-only leaderboard using precomputed Leaderboard rows.

    Ranking:
    - total_points desc
    - attempts_on_tops asc
    - name asc

    Returns rows shaped like:
        {
          "competitor_id", "name", "gender",
          "tops", "attempts_on_tops",
          "total_points", "last_update",
          "position"
        }
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

    # --- normalise category once ---
    cat_key = normalize_leaderboard_category(category)
    cache_key = (current_comp.id, cat_key)

    now = time.time()
    cached = LEADERBOARD_CACHE.get(cache_key)
    if cached:
        rows, category_label, ts = cached
        if now - ts <= LEADERBOARD_CACHE_TTL:
            return rows, category_label

    # --- singles only ---
    q = (
        db.session.query(Leaderboard, Competitor)
        .join(Competitor, Competitor.id == Leaderboard.competitor_id)
        .filter(
            Leaderboard.competition_id == current_comp.id,
            Competitor.competition_id == current_comp.id,
        )
    )

    if cat_key == "male":
        q = q.filter(Competitor.gender == "Male")
        category_label = "Male"
    elif cat_key == "female":
        q = q.filter(Competitor.gender == "Female")
        category_label = "Female"
    elif cat_key == "inclusive":
        q = q.filter(Competitor.gender == "Inclusive")
        category_label = "Gender Inclusive"
    else:
        category_label = "All"

    results = (
        q.order_by(
            Leaderboard.total_points.desc(),
            Leaderboard.attempts_on_tops.asc(),
            Competitor.name.asc(),
        )
        .all()
    )

    if not results:
        rows = []
        LEADERBOARD_CACHE[cache_key] = (rows, category_label, now)
        return rows, category_label

    rows = []
    position = 0
    previous_rank_key = None

    for leaderboard_row, competitor in results:
        total_points = int(leaderboard_row.total_points or 0)
        attempts_on_tops = int(leaderboard_row.attempts_on_tops or 0)

        current_rank_key = (total_points, attempts_on_tops)
        if current_rank_key != previous_rank_key:
            position += 1
        previous_rank_key = current_rank_key

        rows.append({
            "competitor_id": competitor.id,
            "name": competitor.name,
            "gender": competitor.gender,
            "tops": int(leaderboard_row.tops or 0),
            "attempts_on_tops": attempts_on_tops,
            "total_points": total_points,
            "last_update": leaderboard_row.last_update,
            "position": position,
        })

    LEADERBOARD_CACHE[cache_key] = (rows, category_label, now)
    return rows, category_label


def build_doubles_leaderboard(competition_id):
    """Convenience wrapper — delegates to build_leaderboard."""
    return build_leaderboard("doubles", competition_id=competition_id)


def build_doubles_rows(singles_rows, competition_id: int):
    """
    Build doubles leaderboard rows from already-scoped singles rows.

    NOTE:
    This helper is kept for compatibility. It uses the *already computed*
    singles totals + attempts_on_tops (which are now top-8 topped-climb based).

    Filtering rule: only include teams where BOTH partners appear in singles_rows
    (so category-filtered leaderboards behave sensibly).
    """
    totals_by_id = {r["competitor_id"]: r.get("total_points", 0) for r in singles_rows}
    attempts_by_id = {r["competitor_id"]: r.get("attempts_on_tops", 0) for r in singles_rows}
    name_by_id = {r["competitor_id"]: r.get("name", "") for r in singles_rows}

    teams = DoublesTeam.query.filter_by(competition_id=competition_id).all()

    doubles_rows = []
    for t in teams:
        a_id = t.competitor_a_id
        b_id = t.competitor_b_id

        if a_id not in totals_by_id or b_id not in totals_by_id:
            continue

        total_points = int(totals_by_id.get(a_id, 0) or 0) + int(totals_by_id.get(b_id, 0) or 0)
        team_attempts = int(attempts_by_id.get(a_id, 0) or 0) + int(attempts_by_id.get(b_id, 0) or 0)

        a_name = name_by_id.get(a_id, f"#{a_id}")
        b_name = name_by_id.get(b_id, f"#{b_id}")

        doubles_rows.append({
            "team_id": t.id,
            "a_id": a_id,
            "b_id": b_id,
            "a_name": a_name,
            "b_name": b_name,
            "total_points": total_points,
            "attempts_on_tops": team_attempts,
            "name": f"{a_name} and {b_name}",
        })

    # Rank: points desc, attempts asc, then names
    doubles_rows.sort(key=lambda r: (-r["total_points"], r.get("attempts_on_tops", 0), (r.get("name") or "").lower()))

    pos = 0
    prev = None
    for r in doubles_rows:
        k = (r["total_points"], r.get("attempts_on_tops", 0))
        if k != prev:
            pos += 1
        prev = k
        r["position"] = pos

    return doubles_rows