from flask import Blueprint, render_template, request, redirect, session
from datetime import datetime, timedelta
import secrets

from app.extensions import db
from app.models import Account, Competitor, LoginCode
from app.helpers.email import send_login_code_via_email


auth_bp = Blueprint("auth", __name__)

# Context processor for navigation
@auth_bp.app_context_processor
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

@auth_bp.route("/")
def index():
    viewer_id = session.get("competitor_id")
    if viewer_id:
        return redirect("/my-comps")
    return render_template("auth_landing.html")


@auth_bp.route("/signup", methods=["GET", "POST"])
def signup():
    error = None
    message = None
    name = ""
    email = ""

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        email = (request.form.get("email") or "").strip().lower()

        if not name:
            error = "Please enter your name."
        elif not email:
            error = "Please enter your email."
        else:
            acct = Account.query.filter_by(email=email).first()
            if not acct:
                acct = Account(email=email)
                db.session.add(acct)
                db.session.commit()

            shell = (
                Competitor.query
                .filter(
                    Competitor.account_id == acct.id,
                    Competitor.competition_id.is_(None),
                )
                .first()
            )
            if not shell:
                shell = Competitor(
                    name=name or "Account",
                    gender="Inclusive",
                    email=acct.email,
                    competition_id=None,
                    account_id=acct.id,
                )
                db.session.add(shell)
                db.session.commit()
            elif name and shell.name in (None, "", "Account"):
                shell.name = name
                db.session.commit()

            code = f"{secrets.randbelow(1_000_000):06d}"
            now = datetime.utcnow()
            login_code = LoginCode(
                competitor_id=shell.id,
                account_id=acct.id,
                code=code,
                created_at=now,
                expires_at=now + timedelta(minutes=10),
                used=False,
            )
            db.session.add(login_code)
            db.session.commit()

            send_login_code_via_email(email, code)

            session["login_email"] = email
            message = "We emailed you a login code."
            return redirect("/login/verify")

    return render_template(
        "signup.html",
        error=error,
        message=message,
        name=name,
        email=email,
    )
