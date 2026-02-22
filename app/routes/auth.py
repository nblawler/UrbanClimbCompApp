from flask import Blueprint, render_template, request, redirect, session, flash
from datetime import datetime, timedelta
from urllib.parse import quote
import secrets

from app.extensions import db
from app.models import Account, Competition, Competitor, LoginCode
from app.helpers.email import send_login_code_via_email, normalize_email, is_admin_email
from app.helpers.scoring import get_or_create_account_for_email, establish_gym_admin_session_for_email
from app.helpers.leaderboard_cache import invalidate_leaderboard_cache
from app.helpers.competition import get_current_comp


auth_bp = Blueprint("auth", __name__)

@auth_bp.route("/signup", methods=["GET", "POST"])
def signup():
    """
    App-level signup (ACCOUNT-based):
    - Collect name + email
    - Create/find Account for email
    - Ensure a shell Competitor row exists for legacy linkage (competition_id=None)
    - Send a 6-digit code for verification
    - Redirect to /login/verify
    """
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
            # 1) Create/find Account (REAL identity)
            acct = Account.query.filter_by(email=email).first()
            if not acct:
                acct = Account(email=email)
                db.session.add(acct)
                db.session.commit()

            # 2) Ensure a shell competitor exists for this account (legacy competitor_id)
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
                    email=acct.email,          # legacy copy
                    competition_id=None,
                    account_id=acct.id,
                )
                db.session.add(shell)
                db.session.commit()
            else:
                # Optional: keep shell name fresh-ish
                if name and shell.name in (None, "", "Account"):
                    shell.name = name
                    db.session.commit()

            # 3) Send a login/verification code (tied to ACCOUNT)
            code = f"{secrets.randbelow(1_000_000):06d}"
            now = datetime.utcnow()

            login_code = LoginCode(
                competitor_id=shell.id,   # legacy column
                account_id=acct.id,       # REAL identity
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

@auth_bp.route("/login", methods=["GET", "POST"])
def login_request():
    error = None
    message = None
    email = ""

    slug = (request.args.get("slug") or "").strip()
    current_comp = Competition.query.filter_by(slug=slug).first() if slug else None
    if slug and not current_comp:
        slug = ""
        current_comp = None

    # Capture "next" on entry (GET) and preserve in session through verify step
    if request.method == "GET":
        next_url = (request.args.get("next") or "").strip()
        if next_url:
            session["login_next"] = next_url

    # If they came from nav (no slug), clear comp context
    if not slug:
        session.pop("active_comp_slug", None)

    if request.method == "POST":
        email = normalize_email(request.form.get("email"))

        posted_slug = (request.form.get("slug") or "").strip()
        if posted_slug:
            slug = posted_slug
            current_comp = Competition.query.filter_by(slug=slug).first()
            if not current_comp:
                slug = ""
                current_comp = None

        # Also allow next to be carried via hidden input (optional, but safe)
        posted_next = (request.form.get("next") or "").strip()
        if posted_next:
            session["login_next"] = posted_next

        if not email:
            error = "Please enter your email."
        else:
            # Must already exist as an account OR be an admin email (optional)
            acct = Account.query.filter_by(email=email).first()
            if not acct:
                if is_admin_email(email):
                    acct = get_or_create_account_for_email(email)
                else:
                    error = "We couldn't find that email. If you're new, please sign up first."

            if not error and acct:
                code = f"{secrets.randbelow(1_000_000):06d}"
                now = datetime.utcnow()

                # We still need a competitor_id for legacy column (NOT used for auth)
                comp_shell = (
                    Competitor.query
                    .filter(
                        Competitor.account_id == acct.id,
                        Competitor.competition_id.is_(None),
                    )
                    .order_by(Competitor.created_at.desc())
                    .first()
                )

                if not comp_shell:
                    comp_shell = Competitor(
                        name="Account",
                        gender="Inclusive",
                        email=acct.email,
                        competition_id=None,
                        account_id=acct.id,
                    )
                    db.session.add(comp_shell)
                    db.session.commit()

                login_code = LoginCode(
                    competitor_id=comp_shell.id,   # legacy
                    account_id=acct.id,            # REAL
                    code=code,
                    created_at=now,
                    expires_at=now + timedelta(minutes=10),
                    used=False,
                )
                db.session.add(login_code)
                db.session.commit()

                send_login_code_via_email(email, code)

                session["login_email"] = email

                # If comp context exists, keep it and pass next through to verify
                next_url = session.get("login_next")
                if current_comp and current_comp.slug:
                    session["active_comp_slug"] = current_comp.slug
                    if next_url:
                        return redirect(f"/login/verify?slug={current_comp.slug}&next={quote(next_url)}")
                    return redirect(f"/login/verify?slug={current_comp.slug}")

                session.pop("active_comp_slug", None)
                if next_url:
                    return redirect(f"/login/verify?next={quote(next_url)}")
                return redirect("/login/verify")

    return render_template(
        "login_request.html",
        email=email,
        error=error,
        message=message,
        slug=slug,
        # Optional: if you add a hidden field in the template, you can use this
        next=session.get("login_next", ""),
    )


# --- Email login: verify code ---

@auth_bp.route("/login/verify", methods=["GET", "POST"])
def login_verify():
    error = None
    message = None

    slug = (request.args.get("slug") or "").strip()
    current_comp = Competition.query.filter_by(slug=slug).first() if slug else None
    if slug and not current_comp:
        slug = ""
        current_comp = None

    # Capture / preserve next (GET entry point)
    if request.method == "GET":
        next_qs = (request.args.get("next") or "").strip()
        if next_qs:
            session["login_next"] = next_qs

    email = normalize_email(session.get("login_email"))

    def pending_join_matches(comp_slug: str) -> bool:
        return bool(
            comp_slug
            and (session.get("pending_join_slug") or "").strip() == comp_slug
            and (session.get("pending_join_name") or "").strip()
        )

    def get_next_url_from_request() -> str:
        """
        Prefer explicit next passed through the form (POST),
        else querystring (GET/POST), else session.
        """
        if request.method == "POST":
            posted_next = (request.form.get("next") or "").strip()
            if posted_next:
                return posted_next
        qs_next = (request.args.get("next") or "").strip()
        if qs_next:
            return qs_next
        return (session.get("login_next") or "").strip()

    def safe_redirect(url: str):
        """
        Only allow internal redirects. If url is blank or looks external, ignore it.
        """
        if not url:
            return None
        u = url.strip()
        if not u.startswith("/"):
            return None
        if u.startswith("//"):
            return None
        return redirect(u)

    if request.method == "POST":
        email = normalize_email(request.form.get("email"))
        code = (request.form.get("code") or "").strip()

        posted_slug = (request.form.get("slug") or "").strip()
        if posted_slug:
            slug = posted_slug
            current_comp = Competition.query.filter_by(slug=slug).first()
            if not current_comp:
                slug = ""
                current_comp = None

        # Keep next alive across POSTs
        next_url = get_next_url_from_request()
        if next_url:
            session["login_next"] = next_url

        if not email or not code:
            error = "Please enter both your email and the code."
        else:
            acct = Account.query.filter_by(email=email).first()
            if not acct:
                error = "We couldn't find that email. Please sign up first."

            if not error and acct:
                now = datetime.utcnow()

                login_code = (
                    LoginCode.query
                    .filter_by(account_id=acct.id, code=code, used=False)
                    .order_by(LoginCode.created_at.desc())
                    .first()
                )

                if not login_code:
                    error = "Invalid code. Please double-check or request a new one."
                elif login_code.expires_at < now:
                    error = "That code has expired. Please request a new one."
                else:
                    login_code.used = True
                    db.session.commit()

                    # Auth/session identity = ACCOUNT
                    session.pop("login_email", None)
                    session["competitor_email"] = acct.email
                    session["account_id"] = acct.id

                    # Update admin flags off account-based GymAdmin
                    establish_gym_admin_session_for_email(acct.email)

                    # If comp-scoped, keep it
                    if current_comp and current_comp.slug:
                        session["active_comp_slug"] = current_comp.slug

                        # 1) JOIN FLOW FINALIZE (delayed registration)
                        if pending_join_matches(current_comp.slug):
                            name = (session.get("pending_join_name") or "").strip()
                            gender = (session.get("pending_join_gender") or "Inclusive").strip()
                            if gender not in ("Male", "Female", "Inclusive"):
                                gender = "Inclusive"

                            registered = (
                                Competitor.query
                                .filter(
                                    Competitor.account_id == acct.id,
                                    Competitor.competition_id == current_comp.id,
                                )
                                .first()
                            )

                            if not registered:
                                registered = Competitor(
                                    name=name,
                                    gender=gender,
                                    email=acct.email,         # legacy copy
                                    competition_id=current_comp.id,
                                    account_id=acct.id,
                                )
                                db.session.add(registered)
                                db.session.commit()
                                invalidate_leaderboard_cache()

                            session["competitor_id"] = registered.id

                            # Clear pending join state
                            session.pop("pending_join_slug", None)
                            session.pop("pending_join_name", None)
                            session.pop("pending_join_gender", None)
                            session.pop("pending_comp_verify", None)

                            # If next exists, honour it (common: /comp/<slug>/join or similar)
                            next_url = get_next_url_from_request()
                            r = safe_redirect(next_url)
                            if r:
                                session.pop("login_next", None)
                                return r

                            session.pop("login_next", None)
                            return redirect(f"/comp/{current_comp.slug}/competitor/{registered.id}/sections")

                        # 2) Normal comp-scoped login:
                        registered = (
                            Competitor.query
                            .filter(
                                Competitor.account_id == acct.id,
                                Competitor.competition_id == current_comp.id,
                            )
                            .first()
                        )

                        # If they have a competitor row, set it
                        if registered:
                            session["competitor_id"] = registered.id
                            session.pop("pending_comp_verify", None)

                            # If next exists, honour it (but keep internal-only safety)
                            next_url = get_next_url_from_request()
                            r = safe_redirect(next_url)
                            if r:
                                session.pop("login_next", None)
                                return r

                            session.pop("login_next", None)
                            return redirect(f"/comp/{current_comp.slug}/competitor/{registered.id}/sections")

                        # Otherwise: they must join
                        # If next points to join, go there; else go to join anyway.
                        next_url = get_next_url_from_request()
                        if next_url == f"/comp/{current_comp.slug}/join":
                            session.pop("login_next", None)
                            return redirect(next_url)

                        session.pop("login_next", None)
                        return redirect(f"/comp/{current_comp.slug}/join")

                    # Non-comp scoped login: keep them logged in as account only.
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
                            name="Account",
                            gender="Inclusive",
                            email=acct.email,
                            competition_id=None,
                            account_id=acct.id,
                        )
                        db.session.add(shell)
                        db.session.commit()

                    session["competitor_id"] = shell.id
                    session.pop("active_comp_slug", None)
                    session.pop("pending_comp_verify", None)

                    # Honour next for non-comp flows too (e.g., returning to a page)
                    next_url = get_next_url_from_request()
                    r = safe_redirect(next_url)
                    if r:
                        session.pop("login_next", None)
                        return r

                    session.pop("login_next", None)
                    return redirect("/my-comps")

    else:
        if email and not message:
            message = "We've emailed you a 6-digit code. Enter it below to continue."

    # Make sure template can carry next through as hidden field (optional but recommended)
    next_url = get_next_url_from_request()

    return render_template(
        "login_verify.html",
        email=email,
        error=error,
        message=message,
        slug=slug,
        next=next_url,
    )

@auth_bp.route("/register", methods=["GET", "POST"])
def register_competitor():
    """
    Staff/manual registration for the CURRENT active competition only.

    If there is no active competition, do not create a competitor row.
    This prevents orphan competitors (competition_id=None) from being created here.

    IMPORTANT:
    - This route is now ADMIN-ONLY to avoid bypassing the email verification flow.
    """
    # Staff/admin only (prevents bypassing delayed verify flow)
    if not session.get("admin_ok"):
        return redirect("/admin")

    current_comp = get_current_comp()
    if not current_comp:
        return render_template(
            "register.html",
            error="There is no active competition right now. Create/activate a competition in Admin first.",
            competitor=None,
        )

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        gender = (request.form.get("gender") or "Inclusive").strip()
        email = (request.form.get("email") or "").strip().lower()

        if not name:
            return render_template("register.html", error="Name is required.", competitor=None)

        if gender not in ("Male", "Female", "Inclusive"):
            gender = "Inclusive"

        # Prevent duplicate registration for this comp by email
        if email:
            existing = (
                Competitor.query
                .filter(
                    Competitor.competition_id == current_comp.id,
                    Competitor.email == email,
                )
                .first()
            )
            if existing:
                return render_template(
                    "register.html",
                    error=f"{email} is already registered for this competition as #{existing.id}.",
                    competitor=None,
                )

        comp = Competitor(
            name=name,
            gender=gender,
            email=email or None,
            competition_id=current_comp.id,
        )
        db.session.add(comp)
        db.session.commit()
        invalidate_leaderboard_cache()

        return render_template("register.html", error=None, competitor=comp)

    return render_template("register.html", error=None, competitor=None)
    
@auth_bp.route("/logout")
def logout():
    for k in [
        "account_id",
        "admin_ok",
        "admin_is_super",
        "admin_gym_ids",
        "admin_comp_id",
        "competitor_id",
        "competitor_email",   
        "active_comp_slug",
        "login_next",
        "login_slug",
        "login_email",
    ]:
        session.pop(k, None)

    for k in [
        "pending_email",
        "pending_account_id",
        "pending_login_code",
        "pending_join_slug",
        "pending_join_name",
        "pending_join_gender",
        "pending_comp_verify",
    ]:
        session.pop(k, None)

    return redirect("/")


@auth_bp.route("/join", methods=["GET", "POST"])
@auth_bp.route("/join/", methods=["GET", "POST"])
def public_register():
    """
    Legacy join endpoint (old QR code target).

    Redirect to the current live competition's proper join flow:
      /comp/<slug>/join
    """
    current_comp = get_current_comp()

    if not current_comp or not current_comp.slug:
        flash("No live competition right now — please pick a competition first.", "warning")
        return redirect("/my-comps")

    return redirect(f"/comp/{current_comp.slug}/join")

