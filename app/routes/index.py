from flask import Blueprint, render_template, redirect, session

from app.models import Competitor

index_bp = Blueprint("index", __name__)

@index_bp.route("/")
def index():
    """
    First page of the app:
    - If not logged in as a competitor/account, show signup/login landing.
    - If logged in, go straight to Home (/my-comps).
    """
    viewer_id = session.get("competitor_id")
    if viewer_id:
        return redirect("/my-comps")

    return render_template("auth_landing.html")

@index_bp.app_context_processor
def inject_nav_context():
    from app.helpers.leaderboard import get_viewer_comp, comp_is_live
    from flask import request, session

    path = (request.path or "")
    if path.startswith("/login") or path.startswith("/signup") or path.startswith("/admin"):
        return dict(nav_comp=None, show_comp_nav=False)

    comp = get_viewer_comp()
    if not comp or not comp_is_live(comp):
        return dict(nav_comp=None, show_comp_nav=False)

    pending_join_slug = (session.get("pending_join_slug") or "").strip()
    pending_comp_verify = (session.get("pending_comp_verify") or "").strip()

    if pending_join_slug or (pending_comp_verify and pending_comp_verify == comp.slug):
        return dict(nav_comp=None, show_comp_nav=False)

    account_id = session.get("account_id")
    viewer_id = session.get("competitor_id")
    viewer_registered_for_comp = False

    if account_id:
        registered = (
            Competitor.query
            .filter(
                Competitor.account_id == account_id,
                Competitor.competition_id == comp.id,
            )
            .first()
        )
        if registered:
            viewer_registered_for_comp = True
            if viewer_id != registered.id:
                session["competitor_id"] = registered.id
                session["competitor_email"] = registered.email

    if not viewer_registered_for_comp and viewer_id:
        viewer = Competitor.query.get(viewer_id)
        if viewer and viewer.competition_id == comp.id:
            viewer_registered_for_comp = True

    nav_comp = comp if viewer_registered_for_comp else None

    return dict(
        nav_comp=nav_comp,
        show_comp_nav=viewer_registered_for_comp,
    )
