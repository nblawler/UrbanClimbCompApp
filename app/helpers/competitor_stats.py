"""
app/helpers/competitor_stats.py

Call refresh_competitor_stats(competition_id) after any competition
finalises — e.g. when an admin closes a comp, or on a scheduled job.

It will:
  1. Build the leaderboard for that competition.
  2. For every account that has a competitor in that comp, upsert
     their CompetitorStats row with updated best_place, total_comps,
     medal counts, and milestone flags.

Finalist threshold is intentionally left as a placeholder (FINALIST_THRESHOLD).
Set it to whatever cutoff makes sense once that's decided.
"""

from datetime import datetime

from app.extensions import db
from app.models import Competitor, Competition, CompetitorStats
from app.helpers.leaderboard import build_leaderboard

# ------------------------------------------------------------------ #
# Placeholder — update this once finalist cutoff is decided per comp  #
FINALIST_THRESHOLD = None   # e.g. set to 8 for top-8 = finalist     #
# ------------------------------------------------------------------ #


def refresh_competitor_stats(competition_id: int) -> None:
    """
    Recompute and persist CompetitorStats for every account that has
    a competitor entry in the given competition.
    """

    # 1. Build the leaderboard for this comp (cache-backed, fast)
    rows, _ = build_leaderboard(competition_id=competition_id)
    if not rows:
        return

    # Map competitor_id -> position for quick lookup
    position_by_competitor = {r["competitor_id"]: r["position"] for r in rows}

    # 2. Find all competitors in this comp and group by account_id
    competitors_in_comp = (
        Competitor.query
        .filter(
            Competitor.competition_id == competition_id,
            Competitor.account_id.isnot(None),
        )
        .all()
    )

    # Collect unique account_ids that participated in this comp
    account_ids_in_comp = list({c.account_id for c in competitors_in_comp})

    if not account_ids_in_comp:
        return

    # Build a quick map: account_id -> list of competitor_ids in THIS comp
    comp_competitors_by_account: dict[int, list[int]] = {}
    for c in competitors_in_comp:
        comp_competitors_by_account.setdefault(c.account_id, []).append(c.id)

    # 3. For each affected account, recompute stats across ALL their comps
    for account_id in account_ids_in_comp:
        _recompute_account_stats(account_id, position_by_competitor, comp_competitors_by_account.get(account_id, []))

    db.session.commit()


def _recompute_account_stats(
    account_id: int,
    position_by_competitor: dict,
    comp_competitor_ids: list,
) -> None:
    """
    Recompute stats for a single account and upsert their
    CompetitorStats row.

    position_by_competitor is scoped to the competition being refreshed —
    we use it to get the position for this comp only.
    We still need to load all historical stats to compute totals correctly.
    """

    # Load or create the stats row
    stats = CompetitorStats.query.filter_by(account_id=account_id).first()
    if not stats:
        stats = CompetitorStats(account_id=account_id)
        db.session.add(stats)

    # --- Total comps entered ---
    total_comps = (
        db.session.query(db.func.count(db.distinct(Competitor.competition_id)))
        .filter(
            Competitor.account_id == account_id,
            Competitor.competition_id.isnot(None),
        )
        .scalar()
        or 0
    )
    stats.total_comps = total_comps

    # --- Milestone medals (one-time unlocks) ---
    stats.milestone_10 = total_comps >= 10
    stats.milestone_25 = total_comps >= 25
    stats.milestone_50 = total_comps >= 50

    # --- Position for this comp (best across the account's entries in it) ---
    best_pos_this_comp = None
    for cid in comp_competitor_ids:
        pos = position_by_competitor.get(cid)
        if pos is not None:
            if best_pos_this_comp is None or pos < best_pos_this_comp:
                best_pos_this_comp = pos

    # --- Recompute best_place and medal counts across ALL comps ---
    # We need to re-scan all competitions this account has been in.
    # To avoid a full leaderboard rebuild for every comp on every call,
    # we do an incremental approach: keep existing best_place and medal
    # counts, then factor in the new comp's result.
    #
    # For a full recompute (e.g. data correction), call
    # full_recompute_account_stats(account_id) instead.

    if best_pos_this_comp is not None:
        # Update best place
        if stats.best_place is None or best_pos_this_comp < stats.best_place:
            stats.best_place = best_pos_this_comp

        # Increment the appropriate medal counter
        if best_pos_this_comp == 1:
            stats.medals_gold = (stats.medals_gold or 0) + 1
        elif best_pos_this_comp == 2:
            stats.medals_silver = (stats.medals_silver or 0) + 1
        elif best_pos_this_comp == 3:
            stats.medals_bronze = (stats.medals_bronze or 0) + 1
        elif FINALIST_THRESHOLD and best_pos_this_comp <= FINALIST_THRESHOLD:
            stats.medals_finalist = (stats.medals_finalist or 0) + 1

    stats.updated_at = datetime.utcnow()


def full_recompute_account_stats(account_id: int) -> None:
    """
    Full recompute of CompetitorStats for one account from scratch.
    Slower — use for data corrections or backfills, not on every comp close.

    Call full_recompute_all_accounts() to backfill everyone at once.
    """
    stats = CompetitorStats.query.filter_by(account_id=account_id).first()
    if not stats:
        stats = CompetitorStats(account_id=account_id)
        db.session.add(stats)

    # Reset all counters
    stats.best_place = None
    stats.medals_gold = 0
    stats.medals_silver = 0
    stats.medals_bronze = 0
    stats.medals_finalist = 0

    # Total comps
    all_competitors = (
        Competitor.query
        .filter(
            Competitor.account_id == account_id,
            Competitor.competition_id.isnot(None),
        )
        .all()
    )

    comp_ids = list({c.competition_id for c in all_competitors})
    stats.total_comps = len(comp_ids)
    stats.milestone_10 = stats.total_comps >= 10
    stats.milestone_25 = stats.total_comps >= 25
    stats.milestone_50 = stats.total_comps >= 50

    # Group competitor_ids by comp
    by_comp: dict[int, list[int]] = {}
    for c in all_competitors:
        by_comp.setdefault(c.competition_id, []).append(c.id)

    for comp_id, c_ids in by_comp.items():
        rows, _ = build_leaderboard(competition_id=comp_id)
        pos_map = {r["competitor_id"]: r["position"] for r in rows}

        best_pos = None
        for cid in c_ids:
            pos = pos_map.get(cid)
            if pos is not None:
                if best_pos is None or pos < best_pos:
                    best_pos = pos

        if best_pos is None:
            continue

        if stats.best_place is None or best_pos < stats.best_place:
            stats.best_place = best_pos

        if best_pos == 1:
            stats.medals_gold += 1
        elif best_pos == 2:
            stats.medals_silver += 1
        elif best_pos == 3:
            stats.medals_bronze += 1
        elif FINALIST_THRESHOLD and best_pos <= FINALIST_THRESHOLD:
            stats.medals_finalist += 1

    stats.updated_at = datetime.utcnow()
    db.session.commit()


def full_recompute_all_accounts() -> None:
    """
    Backfill CompetitorStats for every account that has any competitor row.
    Run this once after deploying the new model.

    e.g. from a Flask shell:
        from app.helpers.competitor_stats import full_recompute_all_accounts
        full_recompute_all_accounts()
    """
    from app.models import Account

    account_ids = [
        row[0]
        for row in db.session.query(db.distinct(Competitor.account_id))
        .filter(Competitor.account_id.isnot(None))
        .all()
    ]

    for account_id in account_ids:
        print(f"Recomputing stats for account_id={account_id}...")
        full_recompute_account_stats(account_id)

    print(f"Done. Recomputed stats for {len(account_ids)} accounts.")