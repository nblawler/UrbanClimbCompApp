from flask import session, flash, redirect, abort
from functools import wraps

from datetime import datetime
from app.models import Competition, Competitor

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
