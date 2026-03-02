from flask import session, flash, redirect, abort
from functools import wraps

from datetime import datetime
from app.models import Competition, Competitor
from app.helpers.time import melb_now, utc_naive_to_melb

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

def comp_is_finished(comp) -> bool:
    """
    True if comp has an end_at and it is in the past,
    using Melbourne local time semantics.
    DB stores UTC-naive.
    """
    if not comp:
        return True
    if comp.end_at is None:
        return False

    end_melb = utc_naive_to_melb(comp.end_at)  # aware Melbourne
    return melb_now() >= end_melb

def comp_is_live(comp) -> bool:
    """
    Determine whether a competition is currently live for competitors.

    A competition is considered LIVE if:
      1) It exists.
      2) It has been published by admin (is_active == True).
      3) If start_at is set, the current Melbourne time is >= start_at.
      4) If end_at is set, the current Melbourne time is < end_at.

    Notes:
    - All comparisons are performed using Melbourne local time.
    - Datetimes are stored in the DB as UTC-naive.
    - UTC-naive values are converted to Melbourne time before comparison.
    - After end_at is reached, the competition automatically stops being live
      even if is_active remains True.
    """

    if not comp:
        return False

    # Admin publish gate:
    # If the competition is not marked active, it is never live.
    if hasattr(comp, "is_active") and not comp.is_active:
        return False

    now = melb_now()  # Current Melbourne time (aware)

    # Convert stored UTC-naive datetimes to Melbourne-aware for comparison
    start_melb = utc_naive_to_melb(comp.start_at) if comp.start_at else None
    end_melb = utc_naive_to_melb(comp.end_at) if comp.end_at else None

    # Not yet started
    if start_melb and now < start_melb:
        return False

    # Finished (end time is exclusive)
    if end_melb and now >= end_melb:
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
