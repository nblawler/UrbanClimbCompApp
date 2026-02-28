# app/helpers/leaderboard.py

import time
from typing import Optional
from collections import defaultdict

from app.models import Competition, Competitor, Score, DoublesTeam, SectionClimb, Section
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

    # common patterns people use:
    if hasattr(sc, "label") and getattr(sc, "label"):
        return getattr(sc, "label")
    if hasattr(sc, "name") and getattr(sc, "name"):
        return getattr(sc, "name")

    # If SectionClimb has a color field, great:
    if hasattr(sc, "color") and getattr(sc, "color"):
        try:
            return f"{getattr(sc, 'color')} #{sc.climb_number}"
        except Exception:
            return str(getattr(sc, "color"))

    return None

def get_top_climbs_for_competitor(competition_id: int, competitor_id: int, limit: int = 8):
    """
    Return the competitor's top N climbs using the SAME selection rule as build_leaderboard:
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
            "topped": bool(s.topped),
            "score": int(pts or 0),
            "updated_at": s.updated_at.isoformat() if getattr(s, "updated_at", None) else None,
        })

    scored.sort(key=lambda x: (-x["score"], x["attempts"], x["climb_number"] or 0))
    return scored[:limit]


def build_leaderboard(category=None, competition_id=None, slug=None):
    """
    Build leaderboard rows.

    NEW SCORING / RANKING RULES:
    - Each climb has fixed base_points (no per-attempt penalty)
    - A competitor's leaderboard score is the SUM of their TOP 8 climbs (by points)
    - Tie-break: if total_points equal, LOWEST attempts_on_tops ranks higher
      (attempts summed only over topped climbs within those top 8)
    - Stable final tie-break: name asc

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
          "name",              # "A and B"
          "total_points",
          "attempts_on_tops",  # used for tie-break only (not required by UI)
          "position"
        }

    Scoping:
    - If competition_id is provided -> use that competition
    - Else if slug is provided -> look up that competition by slug
    - Else -> fall back to get_current_comp()

    Cache is per (competition_id, cat_key).
    """

    TOP_N = 8

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

    # --- normalise category ONCE ---
    cat_key = normalize_leaderboard_category(category)
    cache_key = (current_comp.id, cat_key)

    now = time.time()
    cached = LEADERBOARD_CACHE.get(cache_key)
    if cached:
        rows, category_label, ts = cached
        if now - ts <= LEADERBOARD_CACHE_TTL:
            return rows, category_label

    # --- doubles mode ---
    if cat_key == "doubles":
        # Always build singles "all" to get per-competitor totals/attempts
        singles_rows, _ = build_leaderboard("all", competition_id=current_comp.id)

        points_by_id = {r["competitor_id"]: r.get("total_points", 0) for r in singles_rows}
        attempts_by_id = {r["competitor_id"]: r.get("attempts_on_tops", 0) for r in singles_rows}
        name_by_id = {r["competitor_id"]: r.get("name", "") for r in singles_rows}

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

            total_points = int(points_by_id.get(a_id, 0) or 0) + int(points_by_id.get(b_id, 0) or 0)
            team_attempts = int(attempts_by_id.get(a_id, 0) or 0) + int(attempts_by_id.get(b_id, 0) or 0)

            rows.append({
                "team_id": t.id,
                "a_id": a_id,
                "b_id": b_id,
                "a_name": a_name,
                "b_name": b_name,
                "name": f"{a_name} and {b_name}",
                "total_points": total_points,
                "attempts_on_tops": team_attempts,  # tie-break only
            })

        # Rank: points desc, attempts asc, name asc
        rows.sort(key=lambda r: (-r["total_points"], r.get("attempts_on_tops", 0), (r.get("name") or "").lower()))

        pos = 0
        prev_key = None
        for row in rows:
            k = (row["total_points"], row.get("attempts_on_tops", 0))
            if k != prev_key:
                pos += 1
            prev_key = k
            row["position"] = pos

        category_label = "Doubles"
        LEADERBOARD_CACHE[cache_key] = (rows, category_label, now)
        return rows, category_label

    # --- singles mode ---
    q = Competitor.query.filter(Competitor.competition_id == current_comp.id)

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

    competitors = q.all()
    if not competitors:
        rows = []
        LEADERBOARD_CACHE[cache_key] = (rows, category_label, now)
        return rows, category_label

    competitor_ids = [c.id for c in competitors]

    # Pull all scores for these competitors in one go
    all_scores = (
        Score.query
        .filter(Score.competitor_id.in_(competitor_ids))
        .all()
        if competitor_ids else []
    )

    by_competitor = defaultdict(list)
    for s in all_scores:
        by_competitor[s.competitor_id].append(s)

    rows = []
    for c in competitors:
        scores = by_competitor.get(c.id, [])

        # Build list of scored climbs with points (fixed) and attempts
        scored = []
        last_update = None

        for s in scores:
            p = points_for(s.climb_number, s.attempts, s.topped, current_comp.id)
            scored.append({
                "climb_number": s.climb_number,
                "points": int(p or 0),
                "attempts": int(s.attempts or 0),
                "topped": bool(s.topped),
                "updated_at": s.updated_at,
            })

            if s.updated_at is not None:
                if last_update is None or s.updated_at > last_update:
                    last_update = s.updated_at

        # Sort by points desc, then attempts asc for deterministic selection
        scored.sort(key=lambda x: (-x["points"], x["attempts"], x.get("climb_number") or 0))

        # Take top N climbs
        topN = scored[:TOP_N]

        total_points = sum(x["points"] for x in topN)

        # Only count topped climbs in topN for tops/attempts_on_tops
        tops = sum(1 for x in topN if x["points"] > 0 and x["topped"])
        attempts_on_tops = sum(x["attempts"] for x in topN if x["points"] > 0 and x["topped"])

        rows.append({
            "competitor_id": c.id,
            "name": c.name,
            "gender": c.gender,
            "tops": tops,
            "attempts_on_tops": attempts_on_tops,
            "total_points": total_points,
            "last_update": last_update,
        })

    # Rank: points desc, attempts asc, name asc
    rows.sort(key=lambda r: (-r["total_points"], r["attempts_on_tops"], (r["name"] or "").lower()))

    pos = 0
    prev_key = None
    for row in rows:
        k = (row["total_points"], row["attempts_on_tops"])
        if k != prev_key:
            pos += 1
        prev_key = k
        row["position"] = pos

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
    singles totals + attempts_on_tops (which are top-8 based now).

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