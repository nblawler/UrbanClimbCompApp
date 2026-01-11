from flask import Flask, render_template, request, redirect, jsonify, session, abort, flash
from functools import wraps
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint
from datetime import datetime, timedelta
import os
import sys
import re
import time
import secrets  # for 6-digit codes
import resend

app = Flask(__name__)

# --- Core config / secrets ---
raw_db_url = os.getenv("DATABASE_URL")

# If DATABASE_URL is set (e.g. on Render), normalise it for SQLAlchemy
if raw_db_url:
    if raw_db_url.startswith("postgres://"):
        raw_db_url = raw_db_url.replace("postgres://", "postgresql://", 1)
    DB_URL = raw_db_url
else:
    # Local dev fallback: SQLite file
    DB_URL = "sqlite:///scoring.db"

app.config["SQLALCHEMY_DATABASE_URI"] = DB_URL
print("USING DB_URL:", DB_URL)

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
# Needed for session-based admin + remembering competitor
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "change-me-dev-secret")

db = SQLAlchemy(app)

# --- Resend config ---

RESEND_API_KEY = os.getenv("RESEND_API_KEY")
RESEND_FROM_EMAIL = os.getenv(
    "RESEND_FROM_EMAIL",
    "Climbing Competition <onboarding@resend.dev>",  # fallback; override in Render
)

if RESEND_API_KEY:
    resend.api_key = RESEND_API_KEY
    print("[RESEND] API key loaded", file=sys.stderr)
else:
    print("[RESEND] RESEND_API_KEY not set – emails will be logged only", file=sys.stderr)

# --- Admin via email ---

ADMIN_EMAILS_RAW = os.getenv("ADMIN_EMAILS", "")
# Comma-separated list of admin emails, e.g. "host@urbanclimb.com,other@uc.com"
ADMIN_EMAILS = {
    e.strip().lower()
    for e in ADMIN_EMAILS_RAW.split(",")
    if e.strip()
}

def is_admin_email(email: str) -> bool:
    """Return True if this email is configured as an admin."""
    if not email:
        return False
    return email.strip().lower() in ADMIN_EMAILS

# --- Leaderboard cache ---

LEADERBOARD_CACHE_TTL = 10.0  # seconds
# key: normalised category ("all", "male", "female", "inclusive")
# value: (rows, category_label, timestamp)
LEADERBOARD_CACHE = {}


def invalidate_leaderboard_cache():
    """Clear all cached leaderboard entries."""
    LEADERBOARD_CACHE.clear()


# --- Models ---

class Gym(db.Model):
    __tablename__ = "gym"

    id = db.Column(db.Integer, primary_key=True)

    # Public name of the gym, e.g. "UC Collingwood"
    name = db.Column(db.String(160), nullable=False)

    # For pretty URLs and also for mapping to a static map image if you want
    slug = db.Column(db.String(160), nullable=False, unique=True)

    # Path or URL to the map image for this gym
    # Example: "/static/maps/collingwood-map.png"
    map_image_path = db.Column(db.String(255), nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    competitions = db.relationship(
        "Competition",
        back_populates="gym",
        lazy=True,
    )


class GymAdmin(db.Model):
    __tablename__ = "gym_admin"

    id = db.Column(db.Integer, primary_key=True)

    # which competitor (user) is the admin
    competitor_id = db.Column(
        db.Integer,
        db.ForeignKey("competitor.id"),
        nullable=False,
        index=True,
    )

    # which gym they are allowed to manage
    gym_id = db.Column(
        db.Integer,
        db.ForeignKey("gym.id"),
        nullable=False,
        index=True,
    )

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class Competition(db.Model):
    __tablename__ = "competition"

    id = db.Column(db.Integer, primary_key=True)

    # Public-facing name, e.g. "UC Collingwood Boulder Blitz"
    name = db.Column(db.String(160), nullable=False)

    # Optional extra context (legacy free-text)
    gym_name = db.Column(db.String(160), nullable=True)

    # NEW: formal gym relationship
    gym_id = db.Column(
        db.Integer,
        db.ForeignKey("gym.id"),
        nullable=False,
        index=True,
    )
    gym = db.relationship(
        "Gym",
        back_populates="competitions",
    )

    # For pretty URLs later, e.g. "uc-collingwood-boulder-blitz"
    slug = db.Column(db.String(160), nullable=False, unique=True)

    # When the comp runs (optional for now)
    start_at = db.Column(db.DateTime, nullable=True)
    end_at = db.Column(db.DateTime, nullable=True)

    # Let you “archive” comps without deleting them
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    # Relationships
    sections = db.relationship(
        "Section",
        back_populates="competition",
        lazy=True,
    )

    competitors = db.relationship(
        "Competitor",
        back_populates="competition",
        lazy=True,
    )


class Competitor(db.Model):
    # competitor number (auto-incremented)
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    gender = db.Column(db.String(20), nullable=False, default="Inclusive")
    email = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # which competition this competitor belongs to
    competition_id = db.Column(
        db.Integer,
        db.ForeignKey("competition.id"),
        nullable=True,
        index=True,
    )

    competition = db.relationship(
        "Competition",
        back_populates="competitors",
    )
    __table_args__ = (
    UniqueConstraint("competition_id", "email", name="uq_competition_email"),
    )


class Score(db.Model):
    __tablename__ = "scores"

    id = db.Column(db.Integer, primary_key=True)
    competitor_id = db.Column(
        db.Integer,
        db.ForeignKey("competitor.id"),
        nullable=False,
        index=True,  # INDEX for "all scores for this competitor"
    )
    climb_number = db.Column(
        db.Integer,
        nullable=False,
        index=True,  # INDEX for "all scores for this climb"
    )
    attempts = db.Column(db.Integer, nullable=False, default=0)
    topped = db.Column(db.Boolean, nullable=False, default=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        UniqueConstraint("competitor_id", "climb_number", name="uq_competitor_climb"),
    )

    competitor = db.relationship(
        "Competitor", backref=db.backref("scores", lazy=True)
    )


class Section(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    slug = db.Column(db.String(120), nullable=False, unique=True)
    # start_climb / end_climb are now effectively metadata; sections are defined by SectionClimb rows
    start_climb = db.Column(db.Integer, nullable=False, default=0)
    end_climb = db.Column(db.Integer, nullable=False, default=0)
    gym_id = db.Column(db.Integer, db.ForeignKey("gym.id"), nullable=True, index=True)
    gym = db.relationship("Gym")


    # which competition this section belongs to
    competition_id = db.Column(
        db.Integer,
        db.ForeignKey("competition.id"),
        nullable=True,
        index=True,
    )

    competition = db.relationship(
        "Competition",
        back_populates="sections",
    )


class SectionClimb(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    section_id = db.Column(
        db.Integer,
        db.ForeignKey("section.id"),
        nullable=False,
        index=True,
    )

    # Which gym owns this climb config + map coordinate
    gym_id = db.Column(
        db.Integer,
        db.ForeignKey("gym.id"),
        nullable=True,
        index=True,
    )
    gym = db.relationship("Gym")

    climb_number = db.Column(
        db.Integer,
        nullable=False,
        index=True,  # INDEX for "all section mappings for this climb"
    )
    colour = db.Column(db.String(80), nullable=True)

    # per-climb scoring config (admin editable)
    base_points = db.Column(db.Integer, nullable=True)           # e.g. 1000
    penalty_per_attempt = db.Column(db.Integer, nullable=True)   # e.g. 10
    attempt_cap = db.Column(db.Integer, nullable=True)           # e.g. 5

    # where this climb sits on the map (% of width/height)
    x_percent = db.Column(db.Float, nullable=True)
    y_percent = db.Column(db.Float, nullable=True)

    section = db.relationship("Section", backref=db.backref("climbs", lazy=True))

    __table_args__ = (
        #  keeps your current uniqueness rule (we may change this later)
        UniqueConstraint("section_id", "climb_number", name="uq_section_climb"),
    )



class LoginCode(db.Model):
    """One-time 6-digit login codes for email-based login."""
    id = db.Column(db.Integer, primary_key=True)
    competitor_id = db.Column(db.Integer, db.ForeignKey("competitor.id"), nullable=False)
    code = db.Column(db.String(6), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used = db.Column(db.Boolean, default=False, nullable=False)

    competitor = db.relationship("Competitor")



# --- Competition helper ---

def gym_map_for(gym_name: str) -> str:
    name = (gym_name or "").lower()
    if "adelaide" in name:
        return "Adelaide_Gym_Map.png"
    return "Collingwood_Gym_Map.png"

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


# --- Scoring function ---


def points_for(climb_number, attempts, topped, competition_id=None):
    """
    Calculate points for a climb using ONLY DB config, scoped to a competition.

    If competition_id is None, we fall back to the current active competition.
    """
    if not topped:
        return 0

    # sanity-clamp attempts recorded
    if attempts < 1:
        attempts = 1
    elif attempts > 50:
        attempts = 50

    # Resolve competition scope
    comp = None
    if competition_id:
        comp = Competition.query.get(competition_id)
    else:
        comp = get_current_comp()

    if not comp:
        # No competition context = no reliable scoring config
        return 0

    # Per-climb config must exist in DB for THIS competition
    q = (
        SectionClimb.query
        .join(Section, Section.id == SectionClimb.section_id)
        .filter(
            SectionClimb.climb_number == climb_number,
            Section.competition_id == comp.id,
        )
    )

    # Optional extra safety: ensure gym matches too (if you’re populating gym_id everywhere)
    if comp.gym_id:
        q = q.filter(SectionClimb.gym_id == comp.gym_id)

    sc = q.first()

    if not sc or sc.base_points is None or sc.penalty_per_attempt is None:
        return 0

    base = sc.base_points
    penalty = sc.penalty_per_attempt
    cap = sc.attempt_cap if sc.attempt_cap and sc.attempt_cap > 0 else 5

    # only attempts from 2 onward incur penalty; cap at `cap`
    penalty_attempts = max(0, min(attempts, cap) - 1)

    return max(int(base - penalty * penalty_attempts), 0)


# --- Helpers ---

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


def admin_can_manage_gym(gym):
    """
    Check whether the logged-in admin can manage a given gym.
    - Super admins: always True
    - Gym admins: gym_id must be in admin_gym_ids
    """
    if not session.get("admin_ok"):
        return False

    # No specific gym? Super admin only
    if gym is None:
        return admin_is_super()

    if admin_is_super():
        return True

    allowed = get_session_admin_gym_ids()
    if not allowed:
        return False

    return gym.id in allowed


def admin_can_manage_competition(comp):
    """
    Check whether the logged-in admin can manage a given competition.
    """
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
    Build leaderboard rows, optionally filtered by gender category.

    Scoping:
    - If competition_id is provided -> use that competition
    - Else if slug is provided -> look up that competition by slug
    - Else -> fall back to get_current_comp()

    Cache is per (competition_id, category) so comps don't contaminate each other.
    """

    # --- resolve competition scope ---
    current_comp = None

    if competition_id:
        current_comp = Competition.query.get(competition_id)
    elif slug:
        current_comp = Competition.query.filter_by(slug=slug).first()
    else:
        current_comp = get_current_comp()

    # If no competition found, return empty leaderboard gracefully
    if not current_comp:
        rows = []
        category_label = "No active competition"
        return rows, category_label

    # --- cache lookup (scoped per competition + category) ---
    cat_key = _normalise_category_key(category)
    cache_key = (current_comp.id, cat_key)

    now = time.time()
    cached = LEADERBOARD_CACHE.get(cache_key)
    if cached:
        rows, category_label, ts = cached
        if now - ts <= LEADERBOARD_CACHE_TTL:
            return rows, category_label

    # --- base query, scoped to THIS competition ---
    q = Competitor.query.filter(Competitor.competition_id == current_comp.id)
    category_label = "All"

    if category:
        norm = category.strip().lower()
        if norm.startswith("m"):
            q = q.filter(Competitor.gender == "Male")
            category_label = "Male"
        elif norm.startswith("f"):
            q = q.filter(Competitor.gender == "Female")
            category_label = "Female"
        else:
            q = q.filter(Competitor.gender == "Inclusive")
            category_label = "Gender Inclusive"

    competitors = q.all()
    if not competitors:
        rows = []
        LEADERBOARD_CACHE[cache_key] = (rows, category_label, now)
        return rows, category_label

    competitor_ids = [c.id for c in competitors]

    # Pull only scores for competitors in THIS comp
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

        # points_for scoped to THIS competition
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

    # sort by total points desc, tops desc, attempts asc
    rows.sort(key=lambda r: (-r["total_points"], -r["tops"], r["attempts_on_tops"]))

    # assign positions with ties sharing the same place
    pos = 0
    prev_key = None
    for row in rows:
        k = (row["total_points"], row["tops"], row["attempts_on_tops"])
        if k != prev_key:
            pos += 1
        prev_key = k
        row["position"] = pos

    # cache the result
    LEADERBOARD_CACHE[cache_key] = (rows, category_label, now)

    return rows, category_label


def init_db():
    """
    Ensure DB tables exist.

    We no longer auto-create a default competition; admins create them
    explicitly through the competitions admin UI.
    """
    db.create_all()


# Run bootstrap once at startup so tables exist
with app.app_context():
    init_db()


def competitor_total_points(comp_id: int, competition_id=None) -> int:
    # If we know the competition, only count that competition's scores
    if competition_id:
        scores = (
            Score.query
            .join(Competitor, Competitor.id == Score.competitor_id)
            .filter(
                Score.competitor_id == comp_id,
                Competitor.competition_id == competition_id,
            )
            .all()
        )
    else:
        # Fallback: old behaviour
        scores = Score.query.filter_by(competitor_id=comp_id).all()

    return sum(
        points_for(s.climb_number, s.attempts, s.topped, competition_id)
        for s in scores
    )


def send_login_code_via_email(email: str, code: str):
    """
    Send the 6-digit login code via Resend in production.

    - If RESEND_API_KEY is not set, just log to stderr (local dev).
    """
    # Dev / fallback path
    if not RESEND_API_KEY:
        print(f"[LOGIN CODE - DEV ONLY] {email} -> {code}", file=sys.stderr)
        return

    html = f"""
      <div style="font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; padding: 16px;">
        <p>Hey climber 👋</p>
        <p>Your Urban Climb Comp login code is:</p>
        <p style="font-size: 24px; font-weight: 700; letter-spacing: 4px; margin: 12px 0;">{code}</p>
        <p>This code will expire in 10 minutes. If you didn’t request this, you can ignore this email.</p>
      </div>
    """

    try:
        params = {
            "from": RESEND_FROM_EMAIL,
            "to": [email],
            "subject": "Your Urban Climb Comp login code",
            "html": html,
        }
        resend.Emails.send(params)
        print(f"[LOGIN CODE] Sent login code to {email}", file=sys.stderr)
    except Exception as e:
        # Don't crash the app if email fails; just log it.
        print(f"[LOGIN CODE] Failed to send via Resend: {e}", file=sys.stderr)


# --- Routes ---


@app.route("/")
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


@app.route("/signup", methods=["GET", "POST"])
def signup():
    """
    App-level signup:
    - Collect name + email
    - Create a Competitor "account" with no competition yet
    - Send a 6-digit code for verification (same as login)
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
            # Does this email already have an account?
            existing = Competitor.query.filter_by(email=email).first()

            if existing:
                # Already signed up → just send them a login code instead
                code = f"{secrets.randbelow(1_000_000):06d}"
                now = datetime.utcnow()
                login_code = LoginCode(
                    competitor_id=existing.id,
                    code=code,
                    created_at=now,
                    expires_at=now + timedelta(minutes=10),
                    used=False,
                )
                db.session.add(login_code)
                db.session.commit()

                send_login_code_via_email(email, code)

                session["login_email"] = email
                message = "You already have an account. We've emailed you a login code."
                return redirect("/login/verify")
            else:
                # Create a new "account" competitor (no competition yet)
                comp = Competitor(
                    name=name,
                    gender="Inclusive",
                    email=email,
                    competition_id=None,
                )
                db.session.add(comp)
                db.session.commit()

                # Send a login/verification code
                code = f"{secrets.randbelow(1_000_000):06d}"
                now = datetime.utcnow()
                login_code = LoginCode(
                    competitor_id=comp.id,
                    code=code,
                    created_at=now,
                    expires_at=now + timedelta(minutes=10),
                    used=False,
                )
                db.session.add(login_code)
                db.session.commit()

                send_login_code_via_email(email, code)

                session["login_email"] = email
                return redirect("/login/verify")

    return render_template(
        "signup.html",
        error=error,
        message=message,
        name=name,
        email=email,
    )

@app.route("/competitions")
def competitions_index():
    """
    Simple list of all competitions.
    For now it's read-only; later we'll wire this into per-comp flows.
    """
    comps = (
        Competition.query
        .order_by(Competition.start_at.asc().nullsfirst())
        .all()
    )

    return render_template("competitions.html", competitions=comps)


@app.route("/my-comps")
def my_competitions():
    """
    Competitor-facing hub showing all upcoming competitions.

    - Shows comps with end_at in the future (or no end_at)
    - If comp is live (is_active=True):
        - If competitor is already registered -> "Keep scoring" (go to sections)
        - Else -> "Register" (go to /comp/<slug>/join)
    - If comp is not live -> Upcoming (no register link yet)
    """
    viewer_id = session.get("competitor_id")
    competitor = Competitor.query.get(viewer_id) if viewer_id else None

    now = datetime.utcnow()

    upcoming_q = Competition.query.filter(
        (Competition.end_at == None) | (Competition.end_at >= now)
    )

    competitions = (
        upcoming_q
        .order_by(Competition.start_at.asc().nullsfirst(), Competition.name.asc())
        .all()
    )

    cards = []
    for c in competitions:
        # status + label
        if c.is_active:
            status = "live"
            status_label = "Comp currently live – scan the on-site QR code to register."
            opens_at = None
        else:
            status = "scheduled"
            opens_at = c.start_at
            if opens_at:
                status_label = (
                    "Comp currently not live – opens on "
                    f"{opens_at.strftime('%d %b %Y, %I:%M %p')}."
                )
            else:
                status_label = "Comp currently not live – opening time TBC."

        # scoring URL if you're already in that competition
        my_scoring_url = None
        if competitor and competitor.competition_id == c.id:
            if c.slug:
                my_scoring_url = f"/comp/{c.slug}/competitor/{competitor.id}/sections"
            else:
                my_scoring_url = f"/competitor/{competitor.id}/sections"

        # clickable pill target
        pill_href = None
        pill_title = None

        if my_scoring_url:
            pill_href = my_scoring_url
            pill_title = "Keep scoring"
        elif status == "live" and c.slug:
            pill_href = f"/comp/{c.slug}/join"
            pill_title = "Register"
        else:
            pill_href = None
            pill_title = None

        cards.append(
            {
                "comp": c,
                "status": status,
                "status_label": status_label,
                "opens_at": opens_at,
                "my_scoring_url": my_scoring_url,
                "pill_href": pill_href,
                "pill_title": pill_title,
            }
        )

    return render_template(
        "competitions_upcoming.html",
        competitions=competitions,
        cards=cards,
        competitor=competitor,
        nav_active="my_comps",
    )


@app.route("/resume")
def resume_competitor():
    """
    Resume scoring for the last competitor on this device.
    Only resumes if their competition is still live/not-ended.
    Otherwise, send them to /my-comps.
    """
    cid = session.get("competitor_id")
    if not cid:
        return redirect("/")

    comp = Competitor.query.get(cid)
    if not comp:
        session.pop("competitor_id", None)
        return redirect("/")

    if comp.competition_id:
        comp_row = Competition.query.get(comp.competition_id)
        if comp_row and comp_row.slug:
            now = datetime.utcnow()
            if comp_row.is_active and (comp_row.end_at is None or comp_row.end_at >= now):
                return redirect(f"/comp/{comp_row.slug}/competitor/{cid}/sections")

    # If the comp is finished (or missing), don't send them back to old scoring
    return redirect("/my-comps")

@app.route("/my-scoring")
def my_scoring_redirect():
    """
    Safe entry point for competitor scoring.

    Priority:
    1) If the logged-in competitor is attached to a competition -> go there.
    2) Else, if the session has an active_comp_slug -> send them to that comp's join page.
    3) Else -> back to /my-comps to pick a competition.
    """
    viewer_id = session.get("competitor_id")
    if not viewer_id:
        return redirect("/")

    competitor = Competitor.query.get(viewer_id)
    if not competitor:
        session.pop("competitor_id", None)
        return redirect("/")

    # 1) If competitor already belongs to a comp, go straight to scoring
    if competitor.competition_id:
        comp = Competition.query.get(competitor.competition_id)
        if comp and comp.slug:
            session["active_comp_slug"] = comp.slug
            return redirect(f"/comp/{comp.slug}/competitor/{competitor.id}/sections")

    # 2) No competition attached yet -> use selected comp from session if present
    slug = (session.get("active_comp_slug") or "").strip()
    if slug:
        # If they have no comp, they must register for this comp first
        return redirect(f"/comp/{slug}/join")

    # 3) No context at all -> choose a comp
    return redirect("/my-comps")


# --- Email login: request code ---


@app.route("/login", methods=["GET", "POST"])
def login_request():
    """
    Step 1: user enters their email, we generate a 6-digit code and send it.

    Comp-scoped login ONLY when a slug is explicitly provided:
      - /login?slug=uc-adelaide
      - or hidden form field "slug"

    If user visits /login with no slug (nav), we clear any old comp context
    so they get a neutral login and land on /my-comps after verifying.
    """
    error = None
    message = None
    email = ""

    # ---- comp context: ONLY from explicit request args / form ----
    slug = (request.args.get("slug") or "").strip()
    current_comp = None

    # If they came to /login directly (nav), nuke old comp context
    if not slug:
        session.pop("active_comp_slug", None)
    else:
        current_comp = Competition.query.filter_by(slug=slug).first()
        if not current_comp:
            # invalid slug -> treat as neutral
            slug = ""
            current_comp = None

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()

        # slug might also be posted from a hidden field
        posted_slug = (request.form.get("slug") or "").strip()
        if posted_slug:
            slug = posted_slug
            current_comp = Competition.query.filter_by(slug=slug).first()
            if not current_comp:
                slug = ""
                current_comp = None

        if not email:
            error = "Please enter your email."
        else:
            comp = None

            # 1) If we have a comp context, find the competitor *for that comp*
            if current_comp:
                comp = (
                    Competitor.query
                    .filter(
                        Competitor.email == email,
                        Competitor.competition_id == current_comp.id,
                    )
                    .first()
                )

            # 2) If not found (or no comp context), fall back to global lookup
            if not comp:
                matches = Competitor.query.filter_by(email=email).all()

                # Admin bootstrap if no competitor exists yet
                if not matches and is_admin_email(email):
                    # Prefer comp context; otherwise attach to earliest active comp if any
                    if not current_comp:
                        current_comp = (
                            Competition.query
                            .filter_by(is_active=True)
                            .order_by(Competition.start_at.asc())
                            .first()
                        )

                    comp = Competitor(
                        name="Admin",
                        gender="Inclusive",
                        email=email,
                        competition_id=current_comp.id if current_comp else None,
                    )
                    db.session.add(comp)
                    db.session.commit()
                    print(
                        f"[ADMIN BOOTSTRAP] Created admin competitor for {email} -> id={comp.id}",
                        file=sys.stderr,
                    )
                else:
                    if len(matches) == 0:
                        error = "We couldn't find that email. If you're new, please register first."
                    elif len(matches) == 1:
                        comp = matches[0]
                    else:
                        error = (
                            "That email is registered for multiple competitions. "
                            "Please open the competition you want and use 'Log back into your scoring' from there."
                        )

            if not error and comp:
                # Generate 6-digit code
                code = f"{secrets.randbelow(1_000_000):06d}"
                now = datetime.utcnow()

                login_code = LoginCode(
                    competitor_id=comp.id,
                    code=code,
                    created_at=now,
                    expires_at=now + timedelta(minutes=10),
                    used=False,
                )
                db.session.add(login_code)
                db.session.commit()

                send_login_code_via_email(email, code)

                # Store email for verify step
                session["login_email"] = email

                # IMPORTANT:
                # Only carry active_comp_slug through verify if this login was explicitly comp-scoped.
                if current_comp and current_comp.slug:
                    session["active_comp_slug"] = current_comp.slug
                    return redirect(f"/login/verify?slug={current_comp.slug}")

                # Neutral login flow
                session.pop("active_comp_slug", None)
                return redirect("/login/verify")

    return render_template(
        "login_request.html",
        email=email,
        error=error,
        message=message,
        slug=slug,  # template can include hidden <input name="slug" ...> when present
    )

# --- Email login: verify code ---

@app.route("/login/verify", methods=["GET", "POST"])
def login_verify():
    """
    Step 2: user enters the 6-digit code they received.
    - If their email is in ADMIN_EMAILS, they are a *super admin* (global).
    - If they are listed in GymAdmin rows, they are a *gym admin*.
    - Everyone else is a normal competitor.

    Supports comp-scoped login when a comp slug is available:
      - /login/verify?slug=uc-adelaide
      - or hidden form field "slug"
      - or session["active_comp_slug"]

    NEW redirect rule:
    - Only auto-jump to scoring if the user came in with an explicit slug
      (querystring or hidden form field). Otherwise, land on /my-comps.
    """
    error = None
    message = None

    # Optional competition context (for lookup convenience)
    slug = (request.args.get("slug") or "").strip()

    # If they hit verify without slug, don't let session force it
    if not slug:
        session.pop("active_comp_slug", None)

    current_comp = Competition.query.filter_by(slug=slug).first() if slug else None

    # Pre-fill email from session if available
    email = (session.get("login_email") or "").strip().lower()

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        code = (request.form.get("code") or "").strip()

        posted_slug = (request.form.get("slug") or "").strip()
        if posted_slug:
            slug = posted_slug
            current_comp = Competition.query.filter_by(slug=slug).first() if slug else current_comp

        if not email or not code:
            error = "Please enter both your email and the code."
        else:
            # --- Competition-aware competitor lookup ---
            comp = None

            # If we have a valid comp context, look up competitor for THIS competition
            if current_comp:
                comp = Competitor.query.filter(
                    Competitor.email == email,
                    Competitor.competition_id == current_comp.id,
                ).first()

            # Fallback: global lookup (legacy behaviour)
            if not comp:
                matches = Competitor.query.filter_by(email=email).all()
                if len(matches) == 0:
                    comp = None
                elif len(matches) == 1:
                    comp = matches[0]
                else:
                    # Ambiguous: multiple competitor rows for this email across competitions
                    # Force them to log in from a specific comp page
                    error = (
                        "That email is registered for multiple competitions. "
                        "Please open the competition you want and use 'Log back into your scoring' from there."
                    )

            if not error and not comp:
                error = "We couldn't find that email. Please check or register first."

            if not error and comp:
                now = datetime.utcnow()

                # Get the most recent unused code for this competitor
                login_code = (
                    LoginCode.query
                    .filter_by(competitor_id=comp.id, code=code, used=False)
                    .order_by(LoginCode.created_at.desc())
                    .first()
                )

                if not login_code:
                    error = "Invalid code. Please double-check or request a new one."
                elif login_code.expires_at < now:
                    error = "That code has expired. Please request a new one."
                else:
                    # Mark code as used
                    login_code.used = True
                    db.session.commit()

                    # Clear transient login email
                    session.pop("login_email", None)

                    # Everyone gets competitor_id in session
                    session["competitor_id"] = comp.id

                    # Remember comp context when available (helps keep nav + redirects consistent)
                    if comp.competition_id:
                        linked_comp = Competition.query.get(comp.competition_id)
                        if linked_comp and linked_comp.slug:
                            session["active_comp_slug"] = linked_comp.slug
                            slug = linked_comp.slug
                            current_comp = linked_comp

                    # ----- ADMIN FLAGS -----
                    is_super = is_admin_email(email)

                    gym_admin_rows = GymAdmin.query.filter_by(competitor_id=comp.id).all()
                    gym_ids = [ga.gym_id for ga in gym_admin_rows]

                    if is_super or gym_ids:
                        session["admin_ok"] = True
                        session["admin_is_super"] = bool(is_super)
                        session["admin_gym_ids"] = gym_ids
                    else:
                        session.pop("admin_ok", None)
                        session.pop("admin_is_super", None)
                        session.pop("admin_gym_ids", None)

                    # --- Redirect (UPDATED) ---
                    # Only auto-jump to scoring if the user explicitly came in with a slug
                    # (querystring or hidden field). If they just used the nav /login,
                    # they land on /my-comps.
                    requested_slug = (request.args.get("slug") or posted_slug or "").strip()

                    if requested_slug:
                        requested_comp = Competition.query.filter_by(slug=requested_slug).first()
                        if requested_comp and comp.competition_id == requested_comp.id:
                            return redirect(
                                f"/comp/{requested_comp.slug}/competitor/{comp.id}/sections"
                            )

                    return redirect("/my-comps")

    else:
        # GET: if we already have an email (i.e. just sent a code), show a helpful message
        if email and not message:
            message = "We've emailed you a 6-digit code. Enter it below to log back into your scoring."

    return render_template(
        "login_verify.html",
        email=email,
        error=error,
        message=message,
        slug=slug,  # needed for hidden field in the template
    )


@app.route("/competitor/<int:competitor_id>")
def competitor_redirect(competitor_id):
    """
    Backwards compatibility: if we know the competitor's competition,
    redirect to the slugged sections URL. Otherwise, fall back to
    the old /competitor/<id>/sections route.
    """
    comp = Competitor.query.get_or_404(competitor_id)

    if comp.competition_id:
        comp_row = Competition.query.get(comp.competition_id)
        if comp_row and comp_row.slug:
            return redirect(f"/comp/{comp_row.slug}/competitor/{competitor_id}/sections")

    # Fallback: older non-slug URL
    return redirect(f"/competitor/{competitor_id}/sections")


@app.route("/competitor/<int:competitor_id>/sections")
def competitor_sections(competitor_id):
    """
    Sections index page (legacy URL).

    Non-admins are *forced* to their own competitor id from the session.
    Changing the id in the URL does not let you see or edit someone else's sections.
    """
    viewer_id = session.get("competitor_id")
    is_admin = session.get("admin_ok", False)

    # Not logged in as competitor and not admin -> no access
    if not viewer_id and not is_admin:
        return redirect("/")

    # Determine which competitor to show
    if is_admin:
        target_id = competitor_id
    else:
        target_id = viewer_id
        if competitor_id != viewer_id:
            return redirect(f"/competitor/{viewer_id}/sections")

    comp = Competitor.query.get_or_404(target_id)

    # Infer this competitor's competition + slug (if any)
    comp_row = None
    comp_slug = None
    if comp.competition_id:
        comp_row = Competition.query.get(comp.competition_id)
        if comp_row and comp_row.slug:
            comp_slug = comp_row.slug

    # Only enforce gym-level permissions when an admin is viewing SOMEONE ELSE
    if is_admin and comp_row and viewer_id and target_id != viewer_id:
        if not admin_can_manage_competition(comp_row):
            abort(403)


    # --- Gym map + gym name (DB-driven) ---
    gym_name = None
    gym_map_path = None

    if comp_row and comp_row.gym:
        gym_name = comp_row.gym.name
        gym_map_path = comp_row.gym.map_image_path

    # (Keep legacy var so nothing else breaks while you transition templates)
    gym_map_url = get_gym_map_url_for_competition(comp_row) if comp_row else None

    # Scope sections to the competitor's competition (if we can)
    if comp_row:
        sections = (
            Section.query
            .filter(Section.competition_id == comp_row.id)
            .order_by(Section.name)
            .all()
        )
    else:
        # No competition context -> safest is empty
        sections = []

    total_points = competitor_total_points(
        target_id,
        comp_row.id if comp_row else None
    )

    # Leaderboard position (build_leaderboard is already scoped to active comp)
    rows, _ = build_leaderboard(None)
    position = None
    for r in rows:
        if r["competitor_id"] == target_id:
            position = r["position"]
            break

    # Whether this viewer can edit attempts
    can_edit = (viewer_id == target_id or is_admin)

    # Map dots: only climbs with coords for THIS competition's sections
    if sections:
        section_ids = [s.id for s in sections]

        q = (
            SectionClimb.query
            .filter(
                SectionClimb.section_id.in_(section_ids),
                SectionClimb.x_percent.isnot(None),
                SectionClimb.y_percent.isnot(None),
            )
        )

        # Optional extra safety: also scope by gym_id if the competition has one
        if comp_row and comp_row.gym_id:
            q = q.filter(SectionClimb.gym_id == comp_row.gym_id)

        map_climbs = (
            q.order_by(SectionClimb.climb_number)
             .all()
        )
    else:
        map_climbs = []

    return render_template(
        "competitor_sections.html",
        competitor=comp,
        sections=sections,
        total_points=total_points,
        position=position,
        nav_active="sections",
        viewer_id=viewer_id,
        is_admin=is_admin,
        can_edit=can_edit,
        map_climbs=map_climbs,
        comp=comp_row,       # may be None
        comp_slug=comp_slug, # may be None

        # New (template expects these)
        gym_name=gym_name,
        gym_map_path=gym_map_path,

        # Legacy (safe to keep during transition)
        gym_map_url=gym_map_url,
    )


@app.route("/comp/<slug>/competitor/<int:competitor_id>/sections")
def comp_competitor_sections(slug, competitor_id):
    """
    Sections index page, scoped to a specific competition slug.

    Non-admins are *forced* to their own competitor id from the session.
    Changing the id in the URL does not let you see or edit someone else's sections.
    """

    viewer_id = session.get("competitor_id")
    is_admin = session.get("admin_ok", False)

    # Not logged in as competitor and not admin -> no access
    if not viewer_id and not is_admin:
        return redirect("/")

    # Determine which competitor to show
    if is_admin:
        target_id = competitor_id
    else:
        target_id = viewer_id
        if competitor_id != viewer_id:
            # If they mess with the URL, push them back to their own competitor in this comp
            return redirect(f"/comp/{slug}/competitor/{viewer_id}/sections")

    # Load competitor
    comp = Competitor.query.get_or_404(target_id)

    # Competitor MUST belong to a competition for this slugged route
    if not comp.competition_id:
        abort(404)

    # Resolve the competition from the competitor (NOT from "current active")
    current_comp = Competition.query.get_or_404(comp.competition_id)

    # Guard: slug must match the competitor’s competition
    if current_comp.slug != slug:
        abort(404)

    # Only enforce gym-level permissions when an admin is viewing SOMEONE ELSE
    if is_admin and viewer_id and target_id != viewer_id:
        if not admin_can_manage_competition(current_comp):
            abort(403)

    # --- Gym map + gym name (DB-driven) ---
    gym_name = None
    gym_map_path = None
    if current_comp.gym:
        gym_name = current_comp.gym.name
        gym_map_path = current_comp.gym.map_image_path

    # (Keep legacy var so nothing else breaks while you transition templates)
    gym_map_url = get_gym_map_url_for_competition(current_comp)

    # Sections scoped to THIS competition
    sections = (
        Section.query
        .filter(Section.competition_id == current_comp.id)
        .order_by(Section.name)
        .all()
    )

    total_points = competitor_total_points(target_id, current_comp.id)

    # Leaderboard position (note: build_leaderboard() still uses get_current_comp()
    # If you want correct leaderboard per slug, we’ll update build_leaderboard next.)
    rows, _ = build_leaderboard(None, competition_id=current_comp.id)
    
    position = None
    for r in rows:
        if r["competitor_id"] == target_id:
            position = r["position"]
            break

    # Whether this viewer can edit attempts
    can_edit = (viewer_id == target_id or is_admin)

    # Map dots: climbs with coords for THIS competition’s sections (+ gym guard)
    if sections:
        section_ids = [s.id for s in sections]

        q = (
            SectionClimb.query
            .filter(
                SectionClimb.section_id.in_(section_ids),
                SectionClimb.x_percent.isnot(None),
                SectionClimb.y_percent.isnot(None),
            )
        )

        # Extra safety if you're using gym_id properly
        if current_comp.gym_id:
            q = q.filter(SectionClimb.gym_id == current_comp.gym_id)

        map_climbs = (
            q.order_by(SectionClimb.climb_number)
             .all()
        )
    else:
        map_climbs = []

    return render_template(
        "competitor_sections.html",
        competitor=comp,
        sections=sections,
        total_points=total_points,
        position=position,
        nav_active="sections",
        viewer_id=viewer_id,
        is_admin=is_admin,
        can_edit=can_edit,
        map_climbs=map_climbs,
        comp=current_comp,
        comp_slug=slug,

        # New (template expects these)
        gym_name=gym_name,
        gym_map_path=gym_map_path,

        # Legacy (safe to keep during transition)
        gym_map_url=gym_map_url,
    )

# --- Competitor stats page: My Stats + Overall Stats ---

@app.route("/comp/<slug>/competitor/<int:competitor_id>/stats")
@app.route("/comp/<slug>/competitor/<int:competitor_id>/stats/<string:mode>")
@finished_guard(
    get_comp_func=lambda slug, competitor_id, mode="my": get_comp_or_404(slug),
    redirect_builder=lambda comp, slug, competitor_id, mode="my": f"/comp/{slug}/competitor/{competitor_id}/sections",
    message="This competition has finished — stats are locked."
)
def comp_competitor_stats(slug, competitor_id, mode="my"):
    """
    Stats for a competitor, scoped to a specific competition slug.

    - mode="my"       -> My Stats page (personal heatmap only)
    - mode="overall"  -> Overall Stats page (section performance + global heatmap)
    - mode="climber"  -> Climber Stats page (spectator view, same data as "my")
    """
    current_comp = get_comp_or_404(slug)

    # Normalise mode
    mode = (mode or "my").lower()
    if mode not in ("my", "overall", "climber"):
        mode = "my"

    comp = Competitor.query.get_or_404(competitor_id)

    # Make sure competitor belongs to this competition
    if comp.competition_id != current_comp.id:
        abort(404)

    total_points = competitor_total_points(competitor_id, current_comp.id)

    # Who is viewing?
    view_mode = request.args.get("view", "").lower()
    viewer_id = session.get("competitor_id")
    viewer_is_self = (viewer_id == competitor_id)

    # Spectator mode from old ?view=public flag (still supported)
    is_public_view = (view_mode == "public" and not viewer_is_self)

    # Sections only for this competition
    sections = (
        Section.query
        .filter_by(competition_id=current_comp.id)
        .order_by(Section.name)
        .all()
    )

    # Personal scores for this competitor
    personal_scores = Score.query.filter_by(competitor_id=competitor_id).all()
    personal_by_climb = {s.climb_number: s for s in personal_scores}

    # Global aggregate: ONLY scores from this competition
    all_scores = (
        db.session.query(Score)
        .join(Competitor, Competitor.id == Score.competitor_id)
        .filter(Competitor.competition_id == current_comp.id)
        .all()
    )
    global_by_climb = {}
    for s in all_scores:
        info = global_by_climb.setdefault(
            s.climb_number,
            {
                "attempts_total": 0,
                "tops": 0,
                "flashes": 0,
                "competitors": set(),
            },
        )
        info["attempts_total"] += s.attempts
        info["competitors"].add(s.competitor_id)
        if s.topped:
            info["tops"] += 1
            if s.attempts == 1:
                info["flashes"] += 1

    # Leaderboard position
    rows, _ = build_leaderboard(None, competition_id=current_comp.id)
    position = None
    for r in rows:
        if r["competitor_id"] == competitor_id:
            position = r["position"]
            break

    section_stats = []
    personal_heatmap_sections = []
    global_heatmap_sections = []

    for sec in sections:
        climbs = (
            SectionClimb.query
            .filter_by(section_id=sec.id)
            .order_by(SectionClimb.climb_number)
            .all()
        )

        sec_tops = 0
        sec_attempts = 0
        sec_points = 0

        personal_cells = []
        global_cells = []

        for sc in climbs:
            # Personal
            score = personal_by_climb.get(sc.climb_number)
            if score:
                sec_attempts += score.attempts
                if score.topped:
                    sec_tops += 1
                sec_points += points_for(
                    score.climb_number, score.attempts, score.topped, current_comp.id
                )

                if score.topped and score.attempts == 1:
                    status = "flashed"
                elif score.topped:
                    status = "topped-late"
                else:
                    status = "not-topped"
            else:
                status = "skipped"

            personal_cells.append(
                {
                    "climb_number": sc.climb_number,
                    "status": status,
                }
            )

            # Global
            g = global_by_climb.get(sc.climb_number)
            if not g or len(g["competitors"]) == 0:
                g_status = "no-data"
            else:
                total_comp = len(g["competitors"])
                tops = g["tops"]
                top_rate = tops / total_comp if total_comp > 0 else 0.0

                if top_rate >= 0.8:
                    g_status = "easy"
                elif top_rate >= 0.4:
                    g_status = "medium"
                else:
                    g_status = "hard"

            global_cells.append(
                {
                    "climb_number": sc.climb_number,
                    "status": g_status,
                }
            )

        efficiency = (sec_tops / sec_attempts) if sec_attempts > 0 else 0.0

        section_stats.append(
            {
                "section": sec,
                "tops": sec_tops,
                "attempts": sec_attempts,
                "efficiency": efficiency,
                "points": sec_points,
            }
        )

        personal_heatmap_sections.append(
            {
                "section": sec,
                "climbs": personal_cells,
            }
        )

        global_heatmap_sections.append(
            {
                "section": sec,
                "climbs": global_cells,
            }
        )

    # nav_active changes depending on which page we’re on
    if mode == "my":
        nav_active = "my_stats"
    elif mode == "overall":
        nav_active = "overall_stats"
    else:
        nav_active = "climber_stats"

    return render_template(
        "competitor_stats.html",
        competitor=comp,
        total_points=total_points,
        position=position,
        section_stats=section_stats,
        heatmap_sections=personal_heatmap_sections,
        global_heatmap_sections=global_heatmap_sections,
        is_public_view=is_public_view,
        viewer_id=viewer_id,
        viewer_is_self=viewer_is_self,
        mode=mode,
        nav_active=nav_active,
        comp=current_comp,
        comp_slug=slug,
    )


@app.route("/competitor/<int:competitor_id>/stats")
@app.route("/competitor/<int:competitor_id>/stats/<string:mode>")
def competitor_stats(competitor_id, mode="my"):
    """
    Legacy stats route.

    If the competitor belongs to a competition with a slug, redirect to:
      /comp/<slug>/competitor/<id>/stats/<mode>

    Otherwise, fall back to the old behaviour (single-comp mode).
    """
    comp = Competitor.query.get_or_404(competitor_id)

    # If this competitor is attached to a competition with a slug, use the new route
    if comp.competition_id:
        comp_row = Competition.query.get(comp.competition_id)
        if comp_row and comp_row.slug:
            return redirect(f"/comp/{comp_row.slug}/competitor/{competitor_id}/stats/{mode}")

    # --- Fallback: original single-comp logic ---

    # Normalise mode
    mode = (mode or "my").lower()
    if mode not in ("my", "overall", "climber"):
        mode = "my"

    total_points = competitor_total_points(competitor_id)

    # Who is viewing?
    view_mode = request.args.get("view", "").lower()
    viewer_id = session.get("competitor_id")
    viewer_is_self = (viewer_id == competitor_id)

    # Spectator mode from old ?view=public flag (still supported)
    is_public_view = (view_mode == "public" and not viewer_is_self)

    sections = Section.query.order_by(Section.name).all()

    # Personal scores for this competitor
    personal_scores = Score.query.filter_by(competitor_id=competitor_id).all()
    personal_by_climb = {s.climb_number: s for s in personal_scores}

    # Global aggregate for every climb across all competitors (no comp scoping)
    all_scores = Score.query.all()
    global_by_climb = {}
    for s in all_scores:
        info = global_by_climb.setdefault(
            s.climb_number,
            {
                "attempts_total": 0,
                "tops": 0,
                "flashes": 0,
                "competitors": set(),
            },
        )
        info["attempts_total"] += s.attempts
        info["competitors"].add(s.competitor_id)
        if s.topped:
            info["tops"] += 1
            if s.attempts == 1:
                info["flashes"] += 1

    # --- get leaderboard position for this competitor ---
    rows, _ = build_leaderboard(None)
    position = None
    for r in rows:
        if r["competitor_id"] == competitor_id:
            position = r["position"]
            break

    section_stats = []
    personal_heatmap_sections = []
    global_heatmap_sections = []

    for sec in sections:
        climbs = (
            SectionClimb.query
            .filter_by(section_id=sec.id)
            .order_by(SectionClimb.climb_number)
            .all()
        )

        sec_tops = 0
        sec_attempts = 0
        sec_points = 0

        personal_cells = []
        global_cells = []

        for sc in climbs:
            score = personal_by_climb.get(sc.climb_number)

            if score:
                sec_attempts += score.attempts
                if score.topped:
                    sec_tops += 1
                sec_points += points_for(
                    score.climb_number, score.attempts, score.topped
                )

                if score.topped and score.attempts == 1:
                    status = "flashed"
                elif score.topped:
                    status = "topped-late"
                else:
                    status = "not-topped"
            else:
                status = "skipped"

            personal_cells.append(
                {
                    "climb_number": sc.climb_number,
                    "status": status,
                }
            )

            g = global_by_climb.get(sc.climb_number)
            if not g or len(g["competitors"]) == 0:
                g_status = "no-data"
            else:
                total_comp = len(g["competitors"])
                tops = g["tops"]
                top_rate = tops / total_comp if total_comp > 0 else 0.0

                if top_rate >= 0.8:
                    g_status = "easy"
                elif top_rate >= 0.4:
                    g_status = "medium"
                else:
                    g_status = "hard"

            global_cells.append(
                {
                    "climb_number": sc.climb_number,
                    "status": g_status,
                }
            )

        efficiency = (sec_tops / sec_attempts) if sec_attempts > 0 else 0.0

        section_stats.append(
            {
                "section": sec,
                "tops": sec_tops,
                "attempts": sec_attempts,
                "efficiency": efficiency,
                "points": sec_points,
            }
        )

        personal_heatmap_sections.append(
            {
                "section": sec,
                "climbs": personal_cells,
            }
        )

        global_heatmap_sections.append(
            {
                "section": sec,
                "climbs": global_cells,
            }
        )

    if mode == "my":
        nav_active = "my_stats"
    elif mode == "overall":
        nav_active = "overall_stats"
    else:
        nav_active = "climber_stats"

    return render_template(
        "competitor_stats.html",
        competitor=comp,
        total_points=total_points,
        position=position,
        section_stats=section_stats,
        heatmap_sections=personal_heatmap_sections,
        global_heatmap_sections=global_heatmap_sections,
        is_public_view=is_public_view,
        viewer_id=viewer_id,
        viewer_is_self=viewer_is_self,
        mode=mode,
        nav_active=nav_active,
    )


# --- Per-climb stats page (personal/global view) ---


@app.route("/climb/<int:climb_number>/stats")
def climb_stats(climb_number):
    """
    Stats for a single climb across all competitors.

    Locked when there is no current (not-ended) competition.
    """
    current_comp = get_current_comp()

    if not current_comp:
        flash("There’s no active competition right now — climb stats are unavailable.", "warning")
        return redirect("/my-comps")

    # (extra safety; get_current_comp already prevents ended comps)
    if comp_is_finished(current_comp):
        flash("That competition has finished — climb stats are locked.", "warning")
        return redirect("/my-comps")

    # --- Mode selection: personal vs global context ---
    mode = (request.args.get("mode", "global") or "global").strip().lower()
    if mode not in ("personal", "global"):
        mode = "global"

    from_climber = (request.args.get("from_climber", "0") == "1")

    cid_raw = request.args.get("cid", "").strip()
    competitor = None
    total_points = None
    position = None

    if cid_raw.isdigit():
        competitor = Competitor.query.get(int(cid_raw))
        if competitor:
            total_points = competitor_total_points(competitor.id, current_comp.id)

            rows, _ = build_leaderboard(None, competition_id=current_comp.id)
            for r in rows:
                if r["competitor_id"] == competitor.id:
                    position = r["position"]
                    break

    comp_sections = (
        Section.query
        .filter(Section.competition_id == current_comp.id)
        .all()
    )
    section_ids_for_comp = {s.id for s in comp_sections}

    section_climbs = (
        SectionClimb.query
        .filter(
            SectionClimb.climb_number == climb_number,
            SectionClimb.section_id.in_(section_ids_for_comp) if section_ids_for_comp else True,
        )
        .all()
    )

    if not section_climbs:
        nav_active = "climber_stats" if from_climber else (
            "my_stats" if mode == "personal" else "overall_stats"
        )
        return render_template(
            "climb_stats.html",
            climb_number=climb_number,
            has_config=False,
            competitor=competitor,
            total_points=total_points,
            position=position,
            mode=mode,
            nav_active=nav_active,
            from_climber=from_climber,
        )

    section_ids = {sc.section_id for sc in section_climbs}
    sections = Section.query.filter(Section.id.in_(section_ids)).all()
    sections_by_id = {s.id: s for s in sections}

    scores = (
        Score.query
        .join(Competitor, Score.competitor_id == Competitor.id)
        .filter(
            Score.climb_number == climb_number,
            Competitor.competition_id == current_comp.id,
        )
        .all()
    )

    total_attempts = sum(s.attempts for s in scores)
    tops = sum(1 for s in scores if s.topped)
    flashes = sum(1 for s in scores if s.topped and s.attempts == 1)
    competitor_ids = {s.competitor_id for s in scores}
    num_competitors = len(competitor_ids)

    top_rate = (tops / num_competitors) if num_competitors > 0 else 0.0
    flash_rate = (flashes / num_competitors) if num_competitors > 0 else 0.0
    avg_attempts_per_comp = (
        (total_attempts / num_competitors) if num_competitors > 0 else 0.0
    )
    avg_attempts_on_tops = (
        sum(s.attempts for s in scores if s.topped) / tops
        if tops > 0 else 0.0
    )

    if num_competitors == 0:
        global_difficulty_key = "no-data"
        global_difficulty_label = "Not tried yet (no data)"
    else:
        if top_rate >= 0.8:
            global_difficulty_key = "easy"
            global_difficulty_label = "Easier"
        elif top_rate >= 0.4:
            global_difficulty_key = "medium"
            global_difficulty_label = "Medium"
        else:
            global_difficulty_key = "hard"
            global_difficulty_label = "Harder"

    comps = {}
    if competitor_ids:
        comps = {
            c.id: c
            for c in Competitor.query.filter(Competitor.id.in_(competitor_ids)).all()
        }

    per_competitor = []
    for s in scores:
        c = comps.get(s.competitor_id)
        per_competitor.append(
            {
                "competitor_id": s.competitor_id,
                "name": c.name if c else f"#{s.competitor_id}",
                "attempts": s.attempts,
                "topped": s.topped,
                "points": points_for(s.climb_number, s.attempts, s.topped, current_comp.id),
                "updated_at": s.updated_at,
            }
        )

    per_competitor.sort(key=lambda r: (not r["topped"], r["attempts"]))

    personal_row = None
    if competitor:
        for row in per_competitor:
            if row["competitor_id"] == competitor.id:
                personal_row = row
                break

    nav_active = "climber_stats" if from_climber else (
        "my_stats" if mode == "personal" else "overall_stats"
    )

    return render_template(
        "climb_stats.html",
        climb_number=climb_number,
        has_config=True,
        sections=[
            sections_by_id[sc.section_id]
            for sc in section_climbs
            if sc.section_id in sections_by_id
        ],
        total_attempts=total_attempts,
        tops=tops,
        flashes=flashes,
        num_competitors=num_competitors,
        top_rate=top_rate,
        flash_rate=flash_rate,
        avg_attempts_per_comp=avg_attempts_per_comp,
        avg_attempts_on_tops=avg_attempts_on_tops,
        per_competitor=per_competitor,
        personal_row=personal_row,
        competitor=competitor,
        total_points=total_points,
        position=position,
        mode=mode,
        nav_active=nav_active,
        global_difficulty_key=global_difficulty_key,
        global_difficulty_label=global_difficulty_label,
        from_climber=from_climber,
    )


@app.route("/comp/<slug>/competitor/<int:competitor_id>/section/<section_slug>")
@finished_guard(
    get_comp_func=lambda slug, competitor_id, section_slug: get_comp_or_404(slug),
    redirect_builder=lambda comp, slug, competitor_id, section_slug: f"/comp/{slug}/competitor/{competitor_id}/sections",
    message="This competition has finished — you can’t enter scores anymore."
)
def comp_competitor_section_climbs(slug, competitor_id, section_slug):
    """
    Per-section climbs page, scoped to a specific competition slug.

    Non-admins are *forced* to use their own competitor id from the session.
    This prevents URL tampering from giving edit access to other competitors.
    """
    current_comp = get_comp_or_404(slug)

    viewer_id = session.get("competitor_id")
    is_admin = session.get("admin_ok", False)

    # Not logged in as competitor and not admin -> no access to climbs page
    if not viewer_id and not is_admin:
        return redirect("/")

    # Determine which competitor to show
    if is_admin:
        target_id = competitor_id
    else:
        target_id = viewer_id
        # If they mess with the URL, push them back to their own competitor in this comp
        if competitor_id != viewer_id:
            return redirect(f"/comp/{slug}/competitor/{viewer_id}/section/{section_slug}")

    comp = Competitor.query.get_or_404(target_id)

    # Make sure this competitor actually belongs to this competition
    if comp.competition_id != current_comp.id:
        abort(404)
        
    # If admin is viewing, enforce gym-level permissions
    if is_admin and not admin_can_manage_competition(current_comp):
        abort(403)

    # Section must also belong to this competition
    section = (
        Section.query
        .filter_by(slug=section_slug, competition_id=current_comp.id)
        .first_or_404()
    )

    # All sections for the tabs (only this comp)
    all_sections = (
        Section.query
        .filter_by(competition_id=current_comp.id)
        .order_by(Section.name)
        .all()
    )

    # Leaderboard rows to figure out competitor position
    rows, _ = build_leaderboard(None, competition_id=current_comp.id)
    position = None
    for r in rows:
        if r["competitor_id"] == target_id:
            position = r["position"]
            break

    # ------------- SECTION CLIMBS (what feeds the map) ----------------
    # Climbs for THIS section with coordinates
    section_climbs = (
        SectionClimb.query
        .filter(
            SectionClimb.section_id == section.id,
            SectionClimb.x_percent.isnot(None),
            SectionClimb.y_percent.isnot(None),
        )
        .order_by(SectionClimb.climb_number)
        .all()
    )

    print("=== SECTION CLIMBS FOR", section.id, section.name, "===")
    print([
        {
            "id": sc.id,
            "climb_number": sc.climb_number,
            "section_id": sc.section_id,
            "x": sc.x_percent,
            "y": sc.y_percent,
        }
        for sc in section_climbs
    ])

    climbs = [sc.climb_number for sc in section_climbs]

    colours = {
        sc.climb_number: sc.colour
        for sc in section_climbs
        if sc.colour
    }

    max_points = {
        sc.climb_number: sc.base_points
        for sc in section_climbs
        if sc.base_points is not None
    }

    scores = (
        Score.query
        .join(Competitor, Competitor.id == Score.competitor_id)
        .filter(
            Score.competitor_id == target_id,
            Competitor.competition_id == current_comp.id,
        )
        .all()
    )
    
    existing = {s.climb_number: s for s in scores}

    per_climb_points = {
        s.climb_number: points_for(s.climb_number, s.attempts, s.topped, current_comp.id)
        for s in scores
    }

    total_points = competitor_total_points(target_id, current_comp.id)

    can_edit = True  # if you got here, you're either that competitor or admin

    gym_map_url = get_gym_map_url_for_competition(current_comp)

    return render_template(
        "competitor.html",
        competitor=comp,
        climbs=climbs,
        existing=existing,
        total_points=total_points,
        section=section,
        colours=colours,
        position=position,
        max_points=max_points,
        per_climb_points=per_climb_points,
        nav_active="sections",
        can_edit=can_edit,
        viewer_id=viewer_id,
        is_admin=is_admin,
        section_climbs=section_climbs,   # map dots for THIS section
        sections=all_sections,           # all sections for the tabs
        gym_map_url=gym_map_url,
    )

@app.route("/competitor/<int:competitor_id>/section/<section_slug>")
def competitor_section_climbs(competitor_id, section_slug):
    """
    Legacy route for per-section climbs.

    If the competitor belongs to a competition with a slug, redirect to the
    slugged route:
      /comp/<slug>/competitor/<id>/section/<section_slug>

    Otherwise, fall back to the old behaviour.
    """
    comp = Competitor.query.get_or_404(competitor_id)

    if comp.competition_id:
        comp_row = Competition.query.get(comp.competition_id)
        if comp_row and comp_row.slug:
            return redirect(
                f"/comp/{comp_row.slug}/competitor/{competitor_id}/section/{section_slug}"
            )

    # --- Fallback: old behaviour (no competition attached) ---

    viewer_id = session.get("competitor_id")
    is_admin = session.get("admin_ok", False)

    # Not logged in as competitor and not admin -> no access to climbs page
    if not viewer_id and not is_admin:
        return redirect("/")

    if is_admin:
        target_id = competitor_id
    else:
        target_id = viewer_id
        if competitor_id != viewer_id:
            return redirect(f"/competitor/{viewer_id}/section/{section_slug}")

    comp = Competitor.query.get_or_404(target_id)
    section = Section.query.filter_by(slug=section_slug).first_or_404()

    # 🔹 All sections for the tabs (toggle between sections from here)
    all_sections = Section.query.order_by(Section.name).all()

    # Leaderboard rows to figure out competitor position
    rows, _ = build_leaderboard(None)
    position = None
    for r in rows:
        if r["competitor_id"] == target_id:
            position = r["position"]
            break

    # ------------- SECTION CLIMBS (what feeds the map) ----------------
    section_climbs = (
        SectionClimb.query
        .filter(
            SectionClimb.section_id == section.id,
            SectionClimb.x_percent.isnot(None),
            SectionClimb.y_percent.isnot(None),
        )
        .order_by(SectionClimb.climb_number)
        .all()
    )

    print("=== SECTION CLIMBS FOR", section.id, section.name, "===")
    print([
        {
            "id": sc.id,
            "climb_number": sc.climb_number,
            "section_id": sc.section_id,
            "x": sc.x_percent,
            "y": sc.y_percent,
        }
        for sc in section_climbs
    ])

    if not section_climbs:
        print("No climbs matched this section – DEBUG: falling back to ALL climbs")
        section_climbs = (
            SectionClimb.query
            .filter(
                SectionClimb.x_percent.isnot(None),
                SectionClimb.y_percent.isnot(None),
            )
            .order_by(SectionClimb.climb_number)
            .all()
        )

    # ------------- CLIMB NUMBERS / COLOURS / POINTS -------------------
    climbs = [sc.climb_number for sc in section_climbs]

    colours = {
        sc.climb_number: sc.colour
        for sc in section_climbs
        if sc.colour
    }

    max_points = {
        sc.climb_number: sc.base_points
        for sc in section_climbs
        if sc.base_points is not None
    }

    scores = Score.query.filter_by(competitor_id=target_id).all()
    existing = {s.climb_number: s for s in scores}

    per_climb_points = {
        s.climb_number: points_for(s.climb_number, s.attempts, s.topped)
        for s in scores
    }

    total_points = competitor_total_points(
        target_id,
        comp_row.id if comp_row else None
    )


    can_edit = True  # if you got here, you're either that competitor or admin

    # There is no competition context here, so we can't resolve a specific gym.
    # You can later update this to infer a default gym if you like.
    gym_map_url = None

    return render_template(
        "competitor.html",
        competitor=comp,
        climbs=climbs,
        existing=existing,
        total_points=total_points,
        section=section,
        colours=colours,
        position=position,
        max_points=max_points,
        per_climb_points=per_climb_points,
        nav_active="sections",
        can_edit=can_edit,
        viewer_id=viewer_id,
        is_admin=is_admin,
        section_climbs=section_climbs,
        sections=all_sections,
        gym_map_url=gym_map_url,
    )



# --- Register new competitors (staff use only, separate page for now) ---


@app.route("/register", methods=["GET", "POST"])
def register_competitor():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        gender = request.form.get("gender", "Inclusive").strip()

        if not name:
            return render_template(
                "register.html", error="Name is required.", competitor=None
            )

        if gender not in ("Male", "Female", "Inclusive"):
            gender = "Inclusive"

        current_comp = get_current_comp()

        comp = Competitor(
            name=name,
            gender=gender,
            competition_id=current_comp.id if current_comp else None,
        )
        db.session.add(comp)
        db.session.commit()
        invalidate_leaderboard_cache()

        return render_template(
            "register.html", error=None, competitor=comp
        )

    return render_template("register.html", error=None, competitor=None)


@app.route("/comp/<slug>/join", methods=["GET", "POST"])
def public_register_for_comp(slug):
    """
    Self-service registration for a specific competition, chosen by slug.

    URL example:
      /comp/uc-collingwood-boulder-bash/join

    After registration:
      - If already registered: go straight to scoring for this comp
      - If newly registered: go straight to scoring for this comp
    """
    comp = get_comp_or_404(slug)

    # Keep comp context for the rest of the flow (login/verify/nav)
    if comp and comp.slug:
        session["active_comp_slug"] = comp.slug

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        gender = request.form.get("gender", "Inclusive").strip()
        email = (request.form.get("email") or "").strip().lower()

        error = None
        if not name:
            error = "Please enter your name."
        elif not email:
            error = "Please enter your email."

        if gender not in ("Male", "Female", "Inclusive"):
            gender = "Inclusive"

        if error:
            return render_template(
                "register_public.html",
                comp=comp,
                error=error,
                name=name,
                gender=gender,
                email=email,
            )

        # --- Competition-aware email handling ---

        # 1) Already registered for THIS competition?
        existing_for_comp = Competitor.query.filter(
            Competitor.competition_id == comp.id,
            Competitor.email == email,
        ).first()

        if existing_for_comp:
            # Remember competitor + comp context
            session["competitor_id"] = existing_for_comp.id
            session["active_comp_slug"] = comp.slug

            # If they were previously an admin in this browser, don't carry that into normal competitor mode
            session.pop("admin_ok", None)
            session.pop("admin_is_super", None)
            session.pop("admin_gym_ids", None)

            return redirect(f"/comp/{comp.slug}/competitor/{existing_for_comp.id}/sections")

        # 2) Existing competitor record elsewhere (global unique email is still in place)
        existing_global = Competitor.query.filter_by(email=email).first()
        if existing_global:
            # If it's a "zombie" record with no competition, claim it for this comp (fixes your current bug)
            if not existing_global.competition_id:
                existing_global.name = name or existing_global.name
                existing_global.gender = gender or existing_global.gender
                existing_global.competition_id = comp.id
                db.session.commit()
                invalidate_leaderboard_cache()

                session["competitor_id"] = existing_global.id
                session["active_comp_slug"] = comp.slug

                session.pop("admin_ok", None)
                session.pop("admin_is_super", None)
                session.pop("admin_gym_ids", None)

                return redirect(f"/comp/{comp.slug}/competitor/{existing_global.id}/sections")

            # Otherwise, they are registered for a DIFFERENT comp.
            # Until we remove the global unique constraint, we cannot create a second competitor row for this email.
            error = (
                "That email is already registered for another competition. "
                "Use 'Log back into your scoring' from the competition you joined."
            )
            return render_template(
                "register_public.html",
                comp=comp,
                error=error,
                name=name,
                gender=gender,
                email=email,
            )

        # 3) Brand new registration: create competitor tied to THIS competition
        new_competitor = Competitor(
            name=name,
            gender=gender,
            email=email,
            competition_id=comp.id,
        )
        db.session.add(new_competitor)
        db.session.commit()
        invalidate_leaderboard_cache()

        session["competitor_id"] = new_competitor.id
        session["active_comp_slug"] = comp.slug

        session.pop("admin_ok", None)
        session.pop("admin_is_super", None)
        session.pop("admin_gym_ids", None)

        return redirect(f"/comp/{comp.slug}/competitor/{new_competitor.id}/sections")

    # GET: show blank form
    return render_template(
        "register_public.html",
        comp=comp,
        error=None,
        name="",
        gender="Inclusive",
        email="",
    )
    
@app.route("/logout")
def logout():
    # Clear everything auth-related (competitor + admin + comp context)
    session.pop("competitor_id", None)
    session.pop("login_email", None)
    session.pop("active_comp_slug", None)

    session.pop("admin_ok", None)
    session.pop("admin_is_super", None)
    session.pop("admin_gym_ids", None)

    # Send them to your public-ish home
    return redirect("/my-comps")


@app.route("/join", methods=["GET", "POST"])
@app.route("/join/", methods=["GET", "POST"])
def public_register():
    """
    Self-service registration for competitors (for the current active competition).

    - This is what the QR code at the desk should point to.
    - No admin password, just name + category + email.
    - After registration, redirect to Home (/my-comps).
    """
    current_comp = get_current_comp()

    # If there is no active competition, show a friendly message instead of crashing.
    if not current_comp:
        # You can swap this to a dedicated 'no active comp' template later if you like
        return render_template(
            "register_public.html",
            error="There is no active competition right now. Please check back when your gym starts a comp.",
            name="",
            gender="Inclusive",
            email="",
        )

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        gender = request.form.get("gender", "Inclusive").strip()
        email = (request.form.get("email") or "").strip().lower()

        error = None
        if not name:
            error = "Please enter your name."
        elif not email:
            error = "Please enter your email."

        if gender not in ("Male", "Female", "Inclusive"):
            gender = "Inclusive"

        # Check uniqueness of email
        if not error and email:
            existing = Competitor.query.filter_by(email=email).first()
            if existing:
                error = "That email is already registered. Use it to log back into your scoring."

        if error:
            return render_template(
                "register_public.html",
                error=error,
                name=name,
                gender=gender,
                email=email,
            )

        # Create competitor in the current active competition
        comp = Competitor(
            name=name,
            gender=gender,
            email=email,
            competition_id=current_comp.id,
        )
        db.session.add(comp)
        db.session.commit()
        invalidate_leaderboard_cache()

        # Remember this competitor on this device
        session["competitor_id"] = comp.id

        # After signup, go to Home (Upcoming Comps)
        return redirect("/my-comps")

    # GET: show blank form
    return render_template(
        "register_public.html",
        error=None,
        name="",
        gender="Inclusive",
        email="",
    )


# --- Score API ---

@app.route("/api/score", methods=["POST"])
def api_save_score():
    data = request.get_json(force=True, silent=True) or {}

    try:
        competitor_id = int(data.get("competitor_id", 0))
        climb_number = int(data.get("climb_number", 0))
        attempts = int(data.get("attempts", 1))
        topped = bool(data.get("topped", False))
    except (TypeError, ValueError):
        return "Invalid payload", 400

    if competitor_id <= 0 or climb_number <= 0:
        return "Invalid competitor or climb number", 400

    # --- Auth: competitor themself, admin, or local sim in debug ---
    viewer_id = session.get("competitor_id")
    is_admin = session.get("admin_ok", False)

    # Allow your local 500-competitor sim (no session) when running in debug on localhost
    if (
        not viewer_id
        and not is_admin
        and app.debug
        and request.remote_addr in ("127.0.0.1", "::1")
    ):
        is_admin = True  # treat local debug caller as trusted

    if viewer_id != competitor_id and not is_admin:
        return "Not allowed", 403

    comp = Competitor.query.get(competitor_id)
    if not comp:
        return "Competitor not found", 404

    # Scope everything to the competitor's competition (source of truth)
    if not comp.competition_id:
        return "Competitor not registered for a competition", 400

    current_comp = Competition.query.get(comp.competition_id)
    if not current_comp:
        return "Competition not found", 404

    # ✅ NEW: block edits once the comp is finished
    if comp_is_finished(current_comp):
        return "Competition finished — scoring locked", 403

    # Ensure this climb exists in THIS competition (so correct gym/map)
    sc = (
        SectionClimb.query
        .join(Section, Section.id == SectionClimb.section_id)
        .filter(
            SectionClimb.climb_number == climb_number,
            Section.competition_id == current_comp.id,
        )
        .first()
    )

    if not sc:
        return "Unknown climb number for this competition", 400

    # enforce at least 1 attempt
    if attempts < 1:
        attempts = 1
    elif attempts > 50:
        attempts = 50

    score = Score.query.filter_by(
        competitor_id=competitor_id, climb_number=climb_number
    ).first()

    if not score:
        score = Score(
            competitor_id=competitor_id,
            climb_number=climb_number,
            attempts=attempts,
            topped=topped,
        )
        db.session.add(score)
    else:
        score.attempts = attempts
        score.topped = topped

    db.session.commit()
    invalidate_leaderboard_cache()

    points = points_for(climb_number, attempts, topped, current_comp.id)

    return jsonify(
        {
            "ok": True,
            "competitor_id": competitor_id,
            "climb_number": climb_number,
            "attempts": attempts,
            "topped": topped,
            "points": points,
        }
    )


@app.route("/api/score/<int:competitor_id>")
def api_get_scores(competitor_id):
    Competitor.query.get_or_404(competitor_id)
    scores = (
        Score.query.filter_by(competitor_id=competitor_id)
        .order_by(Score.climb_number)
        .all()
    )
    out = []
    out_append = out.append  # tiny micro-optimisation because why not
    for s in scores:
        out_append(
            {
                "climb_number": s.climb_number,
                "attempts": s.attempts,
                "topped": s.topped,
                "points": points_for(
                    s.climb_number, s.attempts, s.topped
                ),
            }
        )
    return jsonify(out)


# --- Leaderboard pages ---

@app.route("/leaderboard")
def leaderboard_all():
    cid_raw = request.args.get("cid", "").strip()
    competitor = None
    if cid_raw.isdigit():
        competitor = Competitor.query.get(int(cid_raw))

    comp = get_viewer_comp()
    if not comp:
        flash("Pick a competition first to view the leaderboard.", "warning")
        return redirect("/my-comps")

    rows, category_label = build_leaderboard(None, competition_id=comp.id)
    current_competitor_id = session.get("competitor_id")

    return render_template(
        "leaderboard.html",
        leaderboard=rows,
        category=category_label,
        competitor=competitor,
        current_competitor_id=current_competitor_id,
        nav_active="leaderboard",
        comp=comp,
        comp_slug=comp.slug,
    )


@app.route("/leaderboard/<category>")
def leaderboard_by_category(category):
    cid_raw = request.args.get("cid", "").strip()
    competitor = None
    if cid_raw.isdigit():
        competitor = Competitor.query.get(int(cid_raw))

    comp = get_viewer_comp()
    if not comp:
        flash("Pick a competition first to view the leaderboard.", "warning")
        return redirect("/my-comps")

    rows, category_label = build_leaderboard(category, competition_id=comp.id)
    current_competitor_id = session.get("competitor_id")

    return render_template(
        "leaderboard.html",
        leaderboard=rows,
        category=category_label,
        competitor=competitor,
        current_competitor_id=current_competitor_id,
        nav_active="leaderboard",
        comp=comp,
        comp_slug=comp.slug,
    )


@app.route("/api/leaderboard")
def api_leaderboard():
    category = request.args.get("category")

    comp = get_viewer_comp()
    if not comp:
        return jsonify({"category": "No competition selected", "rows": []})

    rows, category_label = build_leaderboard(category, competition_id=comp.id)

    for r in rows:
        if r["last_update"] is not None:
            r["last_update"] = r["last_update"].isoformat()

    return jsonify({"category": category_label, "rows": rows})

# --- Admin (simple password-protected utility) ---

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "letmein123")

ADMIN_EMAILS_RAW = os.getenv("ADMIN_EMAILS", "")
ADMIN_EMAILS = {
    e.strip().lower()
    for e in ADMIN_EMAILS_RAW.split(",")
    if e.strip()
}


def is_admin_email(email: str) -> bool:
    return (email or "").strip().lower() in ADMIN_EMAILS


@app.route("/admin", methods=["GET", "POST"])
def admin_page():
    message = None
    error = None
    search_results = None
    search_query = ""
    is_admin = session.get("admin_ok", False)

    if request.method == "POST":
        action = request.form.get("action")

        # Handle login separately
        if action == "login":
            password = request.form.get("password", "")
            if password != ADMIN_PASSWORD:
                error = "Incorrect admin password."
            else:
                # Password-based admin = super admin
                session["admin_ok"] = True
                session["admin_is_super"] = True
                session["admin_gym_ids"] = None
                is_admin = True
                message = "Admin access granted."

        else:
            # For all other actions, require that admin has been unlocked
            if not is_admin:
                error = "Please enter the admin password first."
            else:
                if action == "reset_all":
                    # Super-admin only (server-side enforcement)
                    if not admin_is_super():
                        abort(403)

                    # For this action, require password *again* each time
                    password = request.form.get("password", "")
                    if password != ADMIN_PASSWORD:
                        error = "Incorrect admin password."
                    else:
                        # Delete scores, section climbs, competitors, sections
                        Score.query.delete()
                        SectionClimb.query.delete()
                        Competitor.query.delete()
                        Section.query.delete()
                        db.session.commit()
                        invalidate_leaderboard_cache()
                        message = "All competitors, scores, sections, and section climbs have been deleted."

                elif action == "delete_competitor":
                    raw_id = request.form.get("competitor_id", "").strip()
                    if not raw_id.isdigit():
                        error = "Please provide a valid competitor number."
                    else:
                        cid = int(raw_id)
                        comp = Competitor.query.get(cid)
                        if not comp:
                            error = f"Competitor {cid} not found."
                        else:
                            Score.query.filter_by(competitor_id=cid).delete()
                            db.session.delete(comp)
                            db.session.commit()
                            invalidate_leaderboard_cache()
                            message = f"Competitor {cid} and their scores have been deleted."

                elif action == "create_competitor":
                    name = request.form.get("new_name", "").strip()
                    gender = request.form.get("new_gender", "Inclusive").strip()

                    if not name:
                        error = "Competitor name is required."
                    else:
                        if gender not in ("Male", "Female", "Inclusive"):
                            gender = "Inclusive"
                        current_comp = get_current_comp()
                        comp = Competitor(
                            name=name,
                            gender=gender,
                            competition_id=current_comp.id if current_comp else None,
                        )
                        db.session.add(comp)
                        db.session.commit()
                        invalidate_leaderboard_cache()
                        message = f"Competitor created: {comp.name} (#{comp.id}, {comp.gender})"

                elif action == "create_section":
                    name = request.form.get("section_name", "").strip()

                    if not name:
                        error = "Please provide a section name."
                    else:
                        slug = slugify(name)
                        existing = Section.query.filter_by(slug=slug).first()
                        if existing:
                            slug = f"{slug}-{int(datetime.utcnow().timestamp())}"

                        current_comp = get_current_comp()

                        s = Section(
                            name=name,
                            slug=slug,
                            start_climb=0,
                            end_climb=0,
                            competition_id=current_comp.id if current_comp else None,
                            gym_id=current_comp.gym_id if current_comp else None,
                        )

                        db.session.add(s)
                        db.session.commit()
                        message = f"Section created: {name}. You can now add climbs via Edit."

                elif action == "search_competitor":
                    search_query = request.form.get("search_name", "").strip()

                    if not search_query:
                        error = "Please enter a name to search."
                    else:
                        pattern = f"%{search_query}%"
                        current_comp = get_current_comp()
                        if current_comp:
                            search_results = (
                                Competitor.query
                                .filter(
                                    Competitor.competition_id == current_comp.id,
                                    Competitor.name.ilike(pattern),
                                )
                                .order_by(Competitor.name, Competitor.id)
                                .all()
                            )
                        else:
                            search_results = []
                        if not search_results:
                            message = f"No competitors found matching '{search_query}'."

    current_comp = get_current_comp()

    if current_comp:
        sections = (
            Section.query
            .filter(Section.competition_id == current_comp.id)
            .order_by(Section.name)
            .all()
        )
    else:
        sections = Section.query.order_by(Section.name).all()

    return render_template(
        "admin.html",
        message=message,
        error=error,
        sections=sections,
        search_results=search_results,
        search_query=search_query,
        is_admin=is_admin,
        current_comp=current_comp,  
    )


@app.route("/admin/comp/<slug>")
def admin_comp(slug):
    """
    Per-competition admin dashboard.

    - Requires admin session (session["admin_ok"] = True)
    - Looks up the competition by slug
    - Loads sections for that competition
    - Reuses the existing admin.html template
    """
    # Make sure only admins can see this
    if not session.get("admin_ok"):
        return redirect("/login")

    # Find the competition or 404 if the slug is invalid
    comp = Competition.query.filter_by(slug=slug).first_or_404()

    # Gym-level permission check
    if not admin_can_manage_competition(comp):
        abort(403)
        
    session["admin_comp_id"] = comp.id

    # Load sections for this competition (if Section has competition_id)
    sections = (
        Section.query
        .filter_by(competition_id=comp.id)
        .order_by(Section.name.asc())
        .all()
    )

    # Reuse admin.html but now with a specific competition in context
    return render_template(
        "admin.html",
        is_admin=True,
        competition=comp,
        sections=sections,
    )

@app.route("/admin/section/<int:section_id>/edit", methods=["GET", "POST"])
def edit_section(section_id):
    # Require an unlocked admin session
    if not session.get("admin_ok"):
        return redirect("/admin")

    section = Section.query.get_or_404(section_id)

    # Admin-selected comp context:
    # - On GET: comp_id from querystring
    # - On POST: comp_id from hidden form field
    # - Fallback: session["admin_comp_id"]
    comp_id = None

    if request.method == "POST":
        raw = (request.form.get("comp_id") or "").strip()
        if raw.isdigit():
            comp_id = int(raw)
    else:
        comp_id = request.args.get("comp_id", type=int)

    if not comp_id:
        comp_id = session.get("admin_comp_id")

    if not comp_id:
        return redirect("/admin/comps")

    current_comp = Competition.query.get(comp_id)
    if not current_comp:
        return redirect("/admin/comps")

    # Keep session in sync (so other admin routes stay consistent)
    session["admin_comp_id"] = current_comp.id

    # Ensure this section belongs to the comp we're editing
    if section.competition_id != current_comp.id:
        abort(404)

    # Gym-level permission check (super admin OR gym admin for this gym)
    if not admin_can_manage_competition(current_comp):
        abort(403)

    error = None
    message = None

    if request.method == "POST":
        action = request.form.get("action")

        if action == "save_section":
            name = request.form.get("name", "").strip()
            if not name:
                error = "Section name is required."
            else:
                section.name = name
                db.session.commit()
                message = "Section name updated."

        elif action == "add_climb":
            climb_raw = request.form.get("climb_number", "").strip()
            colour = request.form.get("colour", "").strip()

            base_raw = request.form.get("base_points", "").strip()
            penalty_raw = request.form.get("penalty_per_attempt", "").strip()
            cap_raw = request.form.get("attempt_cap", "").strip()

            # basic validation
            if not climb_raw.isdigit():
                error = "Please enter a valid climb number."
            elif base_raw == "" or penalty_raw == "" or cap_raw == "":
                error = "Please enter base points, penalty per attempt, and attempt cap."
            elif not (
                base_raw.lstrip("-").isdigit()
                and penalty_raw.lstrip("-").isdigit()
                and cap_raw.lstrip("-").isdigit()
            ):
                error = "Base points, penalty per attempt, and attempt cap must be whole numbers."
            else:
                climb_number = int(climb_raw)
                base_points = int(base_raw)
                penalty_per_attempt = int(penalty_raw)
                attempt_cap = int(cap_raw)

                if climb_number <= 0:
                    error = "Climb number must be positive."
                elif base_points < 0 or penalty_per_attempt < 0 or attempt_cap <= 0:
                    error = "Base points and penalty must be ≥ 0 and attempt cap must be > 0."
                else:
                    # Uniqueness check should include gym_id (your schema wants gym separation)
                    existing = (
                        SectionClimb.query
                        .filter_by(
                            section_id=section.id,
                            climb_number=climb_number,
                            gym_id=current_comp.gym_id,
                        )
                        .first()
                    )

                    if existing:
                        error = f"Climb {climb_number} is already in this section."
                    else:
                        sc = SectionClimb(
                            section_id=section.id,
                            gym_id=current_comp.gym_id,
                            climb_number=climb_number,
                            colour=colour or None,
                            base_points=base_points,
                            penalty_per_attempt=penalty_per_attempt,
                            attempt_cap=attempt_cap,
                        )
                        db.session.add(sc)
                        db.session.commit()
                        message = f"Climb {climb_number} added to {section.name}."

        elif action == "delete_climb":
            climb_id_raw = request.form.get("climb_id", "").strip()
            if not climb_id_raw.isdigit():
                error = "Invalid climb selection."
            else:
                climb_id = int(climb_id_raw)
                sc = SectionClimb.query.get(climb_id)

                if not sc or sc.section_id != section.id:
                    error = "Climb not found in this section."
                else:
                    # Optional extra safety: ensure you're not deleting across gyms
                    if current_comp.gym_id and sc.gym_id and sc.gym_id != current_comp.gym_id:
                        abort(403)

                    # Delete all scores for this climb (for all competitors)
                    Score.query.filter_by(climb_number=sc.climb_number).delete()

                    # Then delete the climb config itself
                    db.session.delete(sc)
                    db.session.commit()
                    invalidate_leaderboard_cache()
                    message = (
                        f"Climb {sc.climb_number} removed from {section.name}, "
                        "and all associated scores were deleted."
                    )

        elif action == "delete_section":
            # Find all climb numbers in this section
            section_climbs = SectionClimb.query.filter_by(section_id=section.id).all()
            climb_numbers = [sc.climb_number for sc in section_climbs]

            # Delete all scores for those climbs (for all competitors)
            if climb_numbers:
                Score.query.filter(Score.climb_number.in_(climb_numbers)).delete()

            # Delete the section's climbs
            SectionClimb.query.filter_by(section_id=section.id).delete()

            # Delete the section itself
            db.session.delete(section)
            db.session.commit()
            invalidate_leaderboard_cache()

            # Keep comp context on redirect
            return redirect(f"/admin/comp/{current_comp.slug}" if current_comp.slug else "/admin/comps")

    climbs = (
        SectionClimb.query
        .filter_by(section_id=section.id)
        .order_by(SectionClimb.climb_number)
        .all()
    )

    return render_template(
        "admin_section_edit.html",
        section=section,
        climbs=climbs,
        error=error,
        message=message,
        current_comp=current_comp,
        current_comp_id=current_comp.id,
    )

@app.route("/admin/map")
def admin_map():
    """
    Map-based climb creation/edit view.
    Admin can click the gym map, then fill climb config and save.
    Loads the *admin-selected* competition (not the public active comp).
    """
    if not session.get("admin_ok"):
        return redirect("/admin")

    # 1) Prefer explicit comp_id in querystring (coming from Manage page link)
    comp_id = request.args.get("comp_id", type=int)

    # 2) Fallback to "admin currently editing" comp stored in session
    if not comp_id:
        comp_id = session.get("admin_comp_id")

    # 3) Final fallback (not ideal, but avoids hard crash)
    if comp_id:
        current_comp = Competition.query.get(comp_id)
    else:
        current_comp = get_current_comp()

    if not current_comp:
        # No competition found — bounce back to admin comps list
        return redirect("/admin/comps")

    # Only allow admins who can manage this competition's gym
    if not admin_can_manage_competition(current_comp):
        abort(403)

    gym_map_url = None
    if current_comp and current_comp.gym:
        gym_map_url = current_comp.gym.map_image_path

    sections = (
        Section.query
        .filter(Section.competition_id == current_comp.id)
        .order_by(Section.name)
        .all()
    )

    section_ids = [s.id for s in sections]
    if section_ids:
        climbs = (
            SectionClimb.query
            .filter(
                SectionClimb.section_id.in_(section_ids),
                # keep this only if gym_id is truly correct/needed in your schema
                SectionClimb.gym_id == current_comp.gym_id,
            )
            .all()
        )
    else:
        climbs = []

    gym_name = current_comp.gym.name if getattr(current_comp, "gym", None) else None
    comp_name = current_comp.name

    # keep comp context in template (for hidden inputs + link building)
    current_comp_id = current_comp.id

    return render_template(
        "admin_map.html",
        sections=sections,
        climbs=climbs,
        gym_map_url=gym_map_url,
        gym_name=gym_name,
        comp_name=comp_name,
        current_comp_id=current_comp_id,  
    )


@app.route("/admin/comps", methods=["GET", "POST"])
def admin_competitions():
    """
    Admin UI to manage competitions:
    - List all competitions (filtered by gym access for gym admins)
    - Create a new competition:
        - super admin: can create for any gym
        - gym admin: can create only for gyms they manage
    - Set a competition as the single active comp
    - Archive (deactivate) a competition
    """
    if not session.get("admin_ok"):
        return redirect("/admin")
    
    if admin_is_super():
        gyms = Gym.query.order_by(Gym.name).all()
    else:
        allowed_gym_ids = get_session_admin_gym_ids()
        if allowed_gym_ids:
            gyms = (
                Gym.query
                .filter(Gym.id.in_(allowed_gym_ids))
                .order_by(Gym.name)
                .all()
            )
        else:
            gyms = []

    message = None
    error = None
    is_super = admin_is_super()
    

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()

        if action == "create_comp":
            name = (request.form.get("name") or "").strip()
            slug_raw = (request.form.get("slug") or "").strip().lower()

            start_date = (request.form.get("start_date") or "").strip()
            start_time = (request.form.get("start_time") or "").strip()
            end_date = (request.form.get("end_date") or "").strip()
            end_time = (request.form.get("end_time") or "").strip()

            is_active_flag = bool(request.form.get("is_active"))

            if not name:
                error = "Competition name is required."
            else:
                # slug: either provided or derived from name
                slug_val = slug_raw or slugify(name)
                existing_slug = Competition.query.filter_by(slug=slug_val).first()
                if existing_slug:
                    # ensure uniqueness by timestamp suffix
                    slug_val = f"{slug_val}-{int(datetime.utcnow().timestamp())}"

                def parse_dt(date_str, time_str):
                    if not date_str:
                        return None
                    try:
                        if not time_str:
                            return datetime.strptime(date_str, "%Y-%m-%d")
                        return datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
                    except ValueError:
                        return None

                start_at = parse_dt(start_date, start_time)
                end_at = parse_dt(end_date, end_time)

                # gym selection must come from gym_id (dropdown)
                gym_id_raw = (request.form.get("gym_id") or "").strip()
                gym = None

                if not gym_id_raw.isdigit():
                    error = "Please select a gym."
                else:
                    gym_id = int(gym_id_raw)

                    if not is_super:
                        # Gym admins can only create comps for their allowed gyms
                        allowed = get_session_admin_gym_ids()
                        if gym_id not in allowed:
                            error = "You are not allowed to create a competition for that gym."

                    if not error:
                        gym = Gym.query.get(gym_id)
                        if not gym:
                            error = "Selected gym not found."

                if not error:
                    comp = Competition(
                        name=name,
                        gym_name=gym.name if gym else None,  # legacy text field (optional)
                        gym=gym,                              # formal relationship
                        slug=slug_val,
                        start_at=start_at,
                        end_at=end_at,
                        is_active=is_active_flag,
                    )
                    db.session.add(comp)
                    db.session.commit()

                    # If this new comp is active, make all others inactive
                    if is_active_flag:
                        all_comps = Competition.query.all()
                        for c in all_comps:
                            c.is_active = (c.id == comp.id)
                        db.session.commit()

                    message = f"Competition '{comp.name}' created."

        elif action == "set_active":
            raw_id = (request.form.get("competition_id") or "").strip()
            if not raw_id.isdigit():
                error = "Invalid competition id."
            else:
                cid = int(raw_id)
                comp = Competition.query.get(cid)
                if not comp:
                    error = "Competition not found."
                elif not admin_can_manage_competition(comp):
                    error = "You are not allowed to manage this competition."
                else:
                    all_comps = Competition.query.all()
                    for c in all_comps:
                        c.is_active = (c.id == comp.id)
                    db.session.commit()
                    message = f"'{comp.name}' is now the active competition."

        elif action == "archive":
            raw_id = (request.form.get("competition_id") or "").strip()
            if not raw_id.isdigit():
                error = "Invalid competition id."
            else:
                cid = int(raw_id)
                comp = Competition.query.get(cid)
                if not comp:
                    error = "Competition not found."
                elif not admin_can_manage_competition(comp):
                    error = "You are not allowed to manage this competition."
                else:
                    comp.is_active = False
                    db.session.commit()
                    message = f"Competition '{comp.name}' has been archived (inactive)."

    # Always show current list, but filter for gym admins
    comps_query = Competition.query
    if not admin_is_super():
        allowed_gym_ids = get_session_admin_gym_ids()
        if allowed_gym_ids:
            comps_query = comps_query.filter(Competition.gym_id.in_(allowed_gym_ids))
        else:
            comps_query = comps_query.filter(False)  # no comps

    comps = (
        comps_query
        .order_by(Competition.start_at.asc(), Competition.created_at.asc())
        .all()
    )

    return render_template(
        "admin_comps.html",
        competitions=comps,
        message=message,
        gyms=gyms,
        error=error,
    )

    

@app.route("/admin/comp/<int:competition_id>/configure")
def admin_configure_competition(competition_id):
    """
    Set this competition as the active one for editing
    and then send the admin to the main admin page where
    they can manage sections, climbs, map, etc.
    """
    if not session.get("admin_ok"):
        return redirect("/admin")

    comp = Competition.query.get_or_404(competition_id)

    # Only allow admins who can manage this gym
    if not admin_can_manage_competition(comp):
        abort(403)

    # Make this the active competition (editing context)
    all_comps = Competition.query.all()
    for c in all_comps:
        c.is_active = (c.id == comp.id)
    db.session.commit()

    print(
        f"[ADMIN CONFIGURE] Now editing competition #{comp.id} – {comp.name}",
        file=sys.stderr,
    )

    # Send them to the existing admin hub where they can create sections, climbs, etc.
    return redirect("/admin")

@app.route("/admin/map/add-climb", methods=["POST"])
def admin_map_add_climb():
    """
    Handle form submission from the map when admin clicks and adds a climb.
    Uses the admin-selected competition context (comp_id), NOT the public "current" comp.
    """
    if not session.get("admin_ok"):
        return redirect("/admin")

    # 1) Get comp_id from POST (hidden field), fallback to session
    comp_id_raw = (request.form.get("comp_id") or "").strip()
    comp_id = int(comp_id_raw) if comp_id_raw.isdigit() else session.get("admin_comp_id")

    if not comp_id:
        return redirect("/admin/comps")

    current_comp = Competition.query.get(comp_id)
    if not current_comp:
        return redirect("/admin/comps")

    # Keep session in sync (so /admin/map GET fallback works too)
    session["admin_comp_id"] = current_comp.id

    # Gym-level permission check (super admin OR gym admin for this gym)
    if not admin_can_manage_competition(current_comp):
        abort(403)

    section_id_raw = (request.form.get("section_id") or "").strip()
    new_section_name = (request.form.get("new_section_name") or "").strip()
    climb_raw = (request.form.get("climb_number") or "").strip()
    colour = (request.form.get("colour") or "").strip()

    base_raw = (request.form.get("base_points") or "").strip()
    penalty_raw = (request.form.get("penalty_per_attempt") or "").strip()
    cap_raw = (request.form.get("attempt_cap") or "").strip()

    x_raw = (request.form.get("x_percent") or "").strip()
    y_raw = (request.form.get("y_percent") or "").strip()

    error = None

    # 2) Decide which section to use (existing or new)
    section = None
    if section_id_raw.isdigit():
        section = Section.query.get(int(section_id_raw))
        # Must belong to this comp (and optionally same gym)
        if not section or section.competition_id != current_comp.id:
            section = None

    if not section and new_section_name:
        slug = slugify(new_section_name)

        # Make uniqueness scoped to this competition (prevents cross-comp collisions)
        existing = Section.query.filter_by(competition_id=current_comp.id, slug=slug).first()
        if existing:
            slug = f"{slug}-{int(datetime.utcnow().timestamp())}"

        section = Section(
            name=new_section_name,
            slug=slug,
            start_climb=0,
            end_climb=0,
            competition_id=current_comp.id,
            gym_id=current_comp.gym_id,
        )
        db.session.add(section)
        db.session.flush()  # get section.id without full commit

    if not section:
        error = "Please choose an existing section or enter a new section name."

    # 3) Basic validation of numbers
    if not error:
        if not climb_raw.isdigit():
            error = "Please enter a valid climb number."
        elif base_raw == "" or penalty_raw == "" or cap_raw == "":
            error = "Please enter base points, penalty per attempt, and attempt cap."
        elif not (base_raw.isdigit() and penalty_raw.isdigit() and cap_raw.isdigit()):
            error = "Base points, penalty per attempt, and attempt cap must be whole numbers."

    if not error:
        climb_number = int(climb_raw)
        base_points = int(base_raw)
        penalty_per_attempt = int(penalty_raw)
        attempt_cap = int(cap_raw)

        if climb_number <= 0:
            error = "Climb number must be positive."
        elif base_points < 0 or penalty_per_attempt < 0 or attempt_cap <= 0:
            error = "Base points, penalty must be ≥ 0 and attempt cap > 0."

    # 4) Coordinates
    if not error:
        try:
            x_percent = float(x_raw)
            y_percent = float(y_raw)
        except ValueError:
            error = "Internal error: invalid click coordinates. Please try again."

    if error:
        return redirect(f"/admin/map?comp_id={current_comp.id}")

    # 5) Enforce uniqueness of climb_number within THIS competition (+ gym)
    # This matches your rule: "Each climb number can only exist once across all sections."
    conflict = (
        db.session.query(SectionClimb)
        .join(Section, SectionClimb.section_id == Section.id)
        .filter(
            Section.competition_id == current_comp.id,
            SectionClimb.gym_id == current_comp.gym_id,
            SectionClimb.climb_number == climb_number,
        )
        .first()
    )
    if conflict:
        return redirect(f"/admin/map?comp_id={current_comp.id}")

    sc = SectionClimb(
        section_id=section.id,
        gym_id=current_comp.gym_id,
        climb_number=climb_number,
        colour=colour or None,
        base_points=base_points,
        penalty_per_attempt=penalty_per_attempt,
        attempt_cap=attempt_cap,
        x_percent=x_percent,
        y_percent=y_percent,
    )
    db.session.add(sc)
    db.session.commit()

    return redirect(f"/admin/map?comp_id={current_comp.id}")


# --- Startup ---

if __name__ == "__main__":
    port = 5001
    if "--port" in sys.argv:
        try:
            idx = sys.argv.index("--port")
            port = int(sys.argv[idx + 1])
        except Exception:
            pass
    app.run(debug=False, port=port)
