# app/helpers/leaderboard.py

import time
from typing import Optional
from collections import defaultdict

from app.models import Competition, Competitor, Score, DoublesTeam, SectionClimb, Section, Leaderboard, DoublesLeaderboard
from app.extensions import db
from app.helpers.competition import get_current_comp
from app.helpers.new_leaderboard import normalize_leaderboard_category
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

    return None


def _safe_label_from_section_climb(sc: Optional[SectionClimb]) -> Optional[str]:
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
    scores = Score.query.filter_by(competitor_id=competitor_id).all()
    if not scores:
        return []

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
        sc = sc_map.get(s.section_climb_id)
        if s.section_climb_id and sc is None:
            continue
        if not bool(s.topped):
            continue

        pts = points_for(s.climb_number, s.attempts, s.topped, competition_id)
        colour = (sc.colour.strip() if (sc and sc.colour) else None)
        label = f"{colour} #{s.climb_number}" if colour else f"Climb #{s.climb_number}"

        scored.append({
            "section_climb_id": s.section_climb_id,
            "climb_number":     s.climb_number,
            "colour":           colour,
            "label":            label,
            "attempts":         int(s.attempts or 0),
            "topped":           True,
            "score":            int(pts or 0),
            "updated_at":       s.updated_at.isoformat() if getattr(s, "updated_at", None) else None,
        })

    scored.sort(key=lambda x: (-x["score"], x["attempts"], x["climb_number"] or 0))
    return scored[:limit]


def build_leaderboard(category=None, competition_id=None, slug=None):
    """
    For singles/filtered categories:
        Returns (query, category_label).
        The query is unpaginated — callers use .count()/.offset()/.limit().

    For doubles:
        Returns (list_of_row_dicts, "Doubles") as before.
    """
    current_competition = None
    if competition_id:
        current_competition = Competition.query.get(competition_id)
    elif slug:
        current_competition = Competition.query.filter_by(slug=slug).first()
    else:
        current_competition = get_current_comp()

    if not current_competition:
        return [], "No active competition"

    category_key = normalize_leaderboard_category(category)

    # Doubles — return a precomputed list as before
    if category_key == "doubles":
        cache_key = (current_competition.id, "doubles")
        current_time = time.time()
        cached = LEADERBOARD_CACHE.get(cache_key)
        if cached:
            cached_rows, cached_label, cached_time = cached
            if current_time - cached_time <= LEADERBOARD_CACHE_TTL:
                return cached_rows, cached_label

        doubles_rows = build_doubles_rows(None, current_competition.id)
        LEADERBOARD_CACHE[cache_key] = (doubles_rows, "Doubles", current_time)
        return doubles_rows, "Doubles"

    # Singles — build and return the query, no .all(), no row-building
    leaderboard_query = (
        db.session.query(Leaderboard, Competitor)
        .join(Competitor, Competitor.id == Leaderboard.competitor_id)
        .filter(
            Leaderboard.competition_id == current_competition.id,
            Competitor.competition_id == current_competition.id,
        )
    )

    if category_key == "male":
        leaderboard_query = leaderboard_query.filter(Competitor.gender == "Male")
        category_label = "Male"
    elif category_key == "female":
        leaderboard_query = leaderboard_query.filter(Competitor.gender == "Female")
        category_label = "Female"
    elif category_key == "inclusive":
        leaderboard_query = leaderboard_query.filter(Competitor.gender == "Inclusive")
        category_label = "Gender Inclusive"
    else:
        category_label = "All"

    leaderboard_query = leaderboard_query.order_by(
        Leaderboard.total_points.desc(),
        Leaderboard.attempts_on_tops.asc(),
        Competitor.name.asc(),
    )

    return leaderboard_query, category_label


def build_doubles_leaderboard(competition_id):
    """Convenience wrapper — delegates to build_leaderboard."""
    return build_leaderboard("doubles", competition_id=competition_id)


def build_doubles_rows(_singles_rows, competition_id: int):
    """
    Reads from DoublesLeaderboard — single query, no computation.
    _singles_rows is unused, kept for compatibility.
    """
    results = (
        db.session.query(DoublesLeaderboard, DoublesTeam)
        .join(DoublesTeam, DoublesTeam.id == DoublesLeaderboard.team_id)
        .filter(DoublesLeaderboard.competition_id == competition_id)
        .order_by(
            DoublesLeaderboard.total_points.desc(),
            DoublesLeaderboard.attempts_on_tops.asc(),
        )
        .all()
    )

    doubles_rows = []
    pos = 0
    prev = None

    for dl, team in results:
        k = (dl.total_points, dl.attempts_on_tops)
        if k != prev:
            pos += 1
        prev = k

        doubles_rows.append({
            "team_id":          team.id,
            "position":         pos,
            "a_id":             dl.a_id,
            "a_name":           dl.a_name,
            "a_climbs":         dl.a_climbs or [],
            "b_id":             dl.b_id,
            "b_name":           dl.b_name,
            "b_climbs":         dl.b_climbs or [],
            "total_points":     dl.total_points,
            "attempts_on_tops": dl.attempts_on_tops,
            "name":             f"{dl.a_name} and {dl.b_name}",
        })

    return doubles_rows

def get_competitor_position(competitor_id: int, competition_id: int):
    """
    Return the leaderboard position for a single competitor.
    Uses a COUNT query instead of fetching all rows.
    """
    lb_row = Leaderboard.query.filter_by(
        competitor_id=competitor_id,
        competition_id=competition_id,
    ).first()

    if not lb_row:
        return None

    above = Leaderboard.query.filter(
        Leaderboard.competition_id == competition_id,
        db.or_(
            Leaderboard.total_points > lb_row.total_points,
            db.and_(
                Leaderboard.total_points == lb_row.total_points,
                Leaderboard.attempts_on_tops < lb_row.attempts_on_tops,
            )
        )
    ).count()

    return above + 1