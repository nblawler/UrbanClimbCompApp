from flask import Flask, render_template, request, redirect, jsonify, session, abort, flash, url_for
from functools import wraps
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from typing import Optional
import json
import os
import sys
import re
import time
import hashlib
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

class Account(db.Model):
    __tablename__ = "account"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), nullable=False, unique=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    competitors = db.relationship("Competitor", back_populates="account")
    login_codes = db.relationship("LoginCode", back_populates="account")
    gym_admins = db.relationship("GymAdmin", back_populates="account")

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

    # Legacy for now (existing DB column). Keep it so old data loads.
    competitor_id = db.Column(
        db.Integer,
        db.ForeignKey("competitor.id"),
        nullable=False,
        index=True,
    )

    # NEW: stable identity
    account_id = db.Column(
        db.Integer,
        db.ForeignKey("account.id"),
        nullable=False,
        index=True,
    )

    gym_id = db.Column(
        db.Integer,
        db.ForeignKey("gym.id"),
        nullable=False,
        index=True,
    )

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    account = db.relationship("Account", back_populates="gym_admins")

    __table_args__ = (
        UniqueConstraint("account_id", "gym_id", name="uq_gym_admin_account_gym"),
    )


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
    __tablename__ = "competitor"

    id = db.Column(db.Integer, primary_key=True)

    name = db.Column(db.String(120), nullable=False)
    gender = db.Column(db.String(20), nullable=False, default="Inclusive")

    # Keep for now (legacy) — but your logic should treat Account.email as source of truth.
    email = db.Column(db.String(255), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    competition_id = db.Column(
        db.Integer,
        db.ForeignKey("competition.id"),
        nullable=True,
        index=True,
    )

    # NEW: the stable user identity
    account_id = db.Column(
        db.Integer,
        db.ForeignKey("account.id"),
        nullable=False,
        index=True,
    )

    account = db.relationship("Account", back_populates="competitors")

    competition = db.relationship(
        "Competition",
        back_populates="competitors",
    )

    __table_args__ = (
        # NEW uniqueness: one competitor per comp per account
        UniqueConstraint("competition_id", "account_id", name="uq_competition_account"),
    )


class DoublesTeam(db.Model):
    __tablename__ = "doubles_team"

    id = db.Column(db.Integer, primary_key=True)
    competition_id = db.Column(db.Integer, db.ForeignKey("competition.id", ondelete="CASCADE"), nullable=False)

    competitor_a_id = db.Column(db.Integer, db.ForeignKey("competitor.id", ondelete="CASCADE"), nullable=False)
    competitor_b_id = db.Column(db.Integer, db.ForeignKey("competitor.id", ondelete="CASCADE"), nullable=False)

    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class DoublesInvite(db.Model):
    __tablename__ = "doubles_invite"

    id = db.Column(db.Integer, primary_key=True)
    competition_id = db.Column(db.Integer, db.ForeignKey("competition.id", ondelete="CASCADE"), nullable=False)
    inviter_competitor_id = db.Column(db.Integer, db.ForeignKey("competitor.id", ondelete="CASCADE"), nullable=False)

    invitee_email = db.Column(db.Text, nullable=False)
    token_hash = db.Column(db.Text, nullable=False)

    status = db.Column(db.Text, nullable=False)  # 'pending','accepted','declined','expired','cancelled'
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    accepted_at = db.Column(db.DateTime(timezone=True), nullable=True)

class Score(db.Model):
    __tablename__ = "scores"

    id = db.Column(db.Integer, primary_key=True)

    competitor_id = db.Column(
        db.Integer,
        db.ForeignKey("competitor.id"),
        nullable=False,
        index=True,
    )

    # Keep climb_number because your table has it NOT NULL and your stats/points use it.
    climb_number = db.Column(
        db.Integer,
        nullable=False,
        index=True,
    )

    attempts = db.Column(db.Integer, nullable=False, default=0)
    topped = db.Column(db.Boolean, nullable=False, default=False)

    # NEW: match DB column (NOT NULL, FK in DB is optional but we enforce linkage in code)
    section_climb_id = db.Column(
        db.Integer,
        db.ForeignKey("section_climb.id"),
        nullable=False,
        index=True,
    )

    # NEW: match DB column
    flashed = db.Column(db.Boolean, nullable=False, default=False)

    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        # match your DB constraint name + meaning
        UniqueConstraint("competitor_id", "section_climb_id", name="uq_competitor_section_climb"),
    )

    competitor = db.relationship("Competitor", backref=db.backref("scores", lazy=True))
    section_climb = db.relationship("SectionClimb")


class Section(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)

    slug = db.Column(db.String(120), nullable=False)

    start_climb = db.Column(db.Integer, nullable=False, default=0)
    end_climb = db.Column(db.Integer, nullable=False, default=0)

    gym_id = db.Column(db.Integer, db.ForeignKey("gym.id"), nullable=True, index=True)
    gym = db.relationship("Gym")

    competition_id = db.Column(
        db.Integer,
        db.ForeignKey("competition.id"),
        nullable=True,
        index=True,
    )

    competition = db.relationship("Competition", back_populates="sections")

    # Polygon boundary stored as JSON string
    # Format: [{"x": 12.34, "y": 56.78}, ...] where x/y are % of image width/height
    boundary_points_json = db.Column(db.Text, nullable=True)

    __table_args__ = (
        db.UniqueConstraint("competition_id", "name", name="uq_section_comp_name"),
        db.UniqueConstraint("competition_id", "slug", name="uq_section_comp_slug"),
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
    __tablename__ = "login_code"

    id = db.Column(db.Integer, primary_key=True)

    # Legacy column still exists in DB — keep it so old rows still load.
    competitor_id = db.Column(db.Integer, db.ForeignKey("competitor.id"), nullable=False)

    # NEW: real identity
    account_id = db.Column(db.Integer, db.ForeignKey("account.id"), nullable=False, index=True)

    code = db.Column(db.String(6), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used = db.Column(db.Boolean, default=False, nullable=False)

    competitor = db.relationship("Competitor")
    account = db.relationship("Account", back_populates="login_codes")

# --- Competition helper ---

def set_pending_join(comp_slug: str, email: str, name: str, gender: str):
    session["pending_join"] = True
    session["pending_join_slug"] = (comp_slug or "").strip()
    session["pending_join_email"] = (email or "").strip().lower()
    session["pending_join_name"] = (name or "").strip()
    session["pending_join_gender"] = (gender or "").strip()

def clear_pending_join():
    session.pop("pending_join", None)
    session.pop("pending_join_slug", None)
    session.pop("pending_join_email", None)
    session.pop("pending_join_name", None)
    session.pop("pending_join_gender", None)

def has_pending_join() -> bool:
    return bool(session.get("pending_join"))


def admin_can_manage_gym_id(gym_id: int) -> bool:
    if session.get("admin_is_super"):
        return True

    allowed_ids = session.get("admin_gym_ids") or []
    try:
        allowed_ids = [int(x) for x in allowed_ids]
    except Exception:
        allowed_ids = []

    return int(gym_id) in allowed_ids

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

def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def get_or_create_account_for_email(email: str) -> Account:
    email = normalize_email(email)
    if not email:
        raise ValueError("email required")

    acct = Account.query.filter_by(email=email).first()
    if acct:
        return acct

    acct = Account(email=email)
    db.session.add(acct)
    db.session.commit()
    return acct


def get_account_for_session() -> Optional[Account]:
    # Prefer explicit session account_id if present
    account_id = session.get("account_id")
    if account_id:
        acct = Account.query.get(account_id)
        if acct:
            return acct

    # Fallback: derive from competitor_email
    email = normalize_email(session.get("competitor_email"))
    if not email:
        return None
    return Account.query.filter_by(email=email).first()


def get_admin_gym_ids_for_email(email: str) -> list[int]:
    email = normalize_email(email)
    if not email:
        return []

    acct = Account.query.filter_by(email=email).first()
    if not acct:
        return []

    return [ga.gym_id for ga in GymAdmin.query.filter_by(account_id=acct.id).all()]
    
def establish_gym_admin_session_for_email(email: str) -> None:
    """
    Single source of truth for admin session flags.
    Uses Account + GymAdmin.account_id (stable even if comp competitors are deleted).
    Also clears stale admin_comp_id if the admin can't manage it anymore.
    """
    email = normalize_email(email)

    # Always reset admin session first (prevents stale perms)
    session["admin_ok"] = False
    session["admin_is_super"] = False
    session["admin_gym_ids"] = []
    session.pop("admin_comp_id", None)

    if not email:
        return

    # Super admin (password-based) stays separate — don't set here.
    # This function is for "gym admin by membership".
    acct = Account.query.filter_by(email=email).first()
    if not acct:
        return

    gym_ids = [
        ga.gym_id
        for ga in GymAdmin.query.filter_by(account_id=acct.id).all()
        if ga.gym_id is not None
    ]

    if gym_ids:
        session["admin_ok"] = True
        session["admin_is_super"] = False
        session["admin_gym_ids"] = sorted(list(set(gym_ids)))

    # If there was an admin_comp_id previously, only keep it if allowed
    # (we popped it above, so nothing to do here unless you want to restore it safely later)


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

def _parse_boundary_points(raw) -> list[dict]:
    """
    Accepts:
      - list of dicts [{"x":..,"y":..}, ...]
      - JSON string of that list
    Returns cleaned list with floats clamped 0..100.
    """
    if raw is None:
        return []

    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return []
        try:
            raw = json.loads(raw)
        except Exception:
            return []

    if not isinstance(raw, list):
        return []

    cleaned = []
    for p in raw:
        if not isinstance(p, dict):
            continue
        try:
            x = float(p.get("x"))
            y = float(p.get("y"))
        except Exception:
            continue

        # clamp to 0..100 (since we're storing % coords)
        x = max(0.0, min(100.0, x))
        y = max(0.0, min(100.0, y))
        cleaned.append({"x": x, "y": y})

    return cleaned


def _boundary_to_json(points: list[dict]) -> str:
    return json.dumps(points, separators=(",", ":"))


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


def admin_can_manage_gym(gym) -> bool:
    if not gym:
        return False

    # Super-admin can manage all gyms
    if session.get("admin_is_super"):
        return True

    gym_ids = session.get("admin_gym_ids") or []
    return gym.id in gym_ids


def admin_can_manage_competition(comp) -> bool:
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
    Build leaderboard rows.

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
          "name",              # "A + B"
          "total_points",
          "position"
        }

    Scoping:
    - If competition_id is provided -> use that competition
    - Else if slug is provided -> look up that competition by slug
    - Else -> fall back to get_current_comp()

    Cache is per (competition_id, category_key).
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

    # --- cache lookup (scoped per competition + category) ---
    cat_key = _normalise_category_key(category)
    cache_key = (current_comp.id, cat_key)

    now = time.time()
    cached = LEADERBOARD_CACHE.get(cache_key)
    if cached:
        rows, category_label, ts = cached
        if now - ts <= LEADERBOARD_CACHE_TTL:
            return rows, category_label

    # --- detect doubles mode early ---
    norm = (category or "").strip().lower()
    is_doubles = norm.startswith("doub")  # matches "doubles"

    if is_doubles:
        # Build singles totals once (All) so doubles can sum partner points
        singles_rows, _ = build_leaderboard(None, competition_id=current_comp.id)

        points_by_id = {r["competitor_id"]: r["total_points"] for r in singles_rows}
        name_by_id = {r["competitor_id"]: r["name"] for r in singles_rows}

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

            total_points = points_by_id.get(a_id, 0) + points_by_id.get(b_id, 0)

            rows.append(
                {
                    "team_id": t.id,
                    "a_id": a_id,
                    "b_id": b_id,
                    "a_name": a_name,
                    "b_name": b_name,
                    "name": f"{a_name} + {b_name}",
                    "total_points": total_points,
                }
            )

        # Sort: points desc, then stable name tie-break
        rows.sort(key=lambda r: (-r["total_points"], r["name"]))

        # Assign positions with ties sharing the same place
        pos = 0
        prev_key = None
        for row in rows:
            k = (row["total_points"],)
            if k != prev_key:
                pos += 1
            prev_key = k
            row["position"] = pos

        category_label = "Doubles"
        LEADERBOARD_CACHE[cache_key] = (rows, category_label, now)
        return rows, category_label

    # --- singles mode (existing logic) ---
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

    rows.sort(key=lambda r: (-r["total_points"], -r["tops"], r["attempts_on_tops"]))

    pos = 0
    prev_key = None
    for row in rows:
        k = (row["total_points"], row["tops"], row["attempts_on_tops"])
        if k != prev_key:
            pos += 1
        prev_key = k
        row["position"] = pos

    LEADERBOARD_CACHE[cache_key] = (rows, category_label, now)
    return rows, category_label


def build_doubles_leaderboard(competition_id):
    teams = DoublesTeam.query.filter_by(competition_id=competition_id).all()
    if not teams:
        return [], "Doubles"

    # First get singles leaderboard rows so we know points
    singles_rows, _ = build_leaderboard(None, competition_id=competition_id)

    points_by_id = {r["competitor_id"]: r["total_points"] for r in singles_rows}

    rows = []
    for team in teams:
        a = Competitor.query.get(team.competitor_a_id)
        b = Competitor.query.get(team.competitor_b_id)

        total_points = (
            points_by_id.get(team.competitor_a_id, 0)
            + points_by_id.get(team.competitor_b_id, 0)
        )

        rows.append({
            "team_id": team.id,
            "name": f"{a.name} + {b.name}",
            "total_points": total_points,
        })

    # sort descending by total points
    rows.sort(key=lambda r: -r["total_points"])

    # assign positions
    pos = 0
    prev_pts = None
    for row in rows:
        if row["total_points"] != prev_pts:
            pos += 1
        prev_pts = row["total_points"]
        row["position"] = pos

    return rows, "Doubles"


def build_doubles_rows(singles_rows, competition_id: int):
    """
    Build doubles leaderboard rows from:
    - singles_rows: output from build_leaderboard(...) (already category-filtered)
    - competition_id: current competition scope

    Filtering rule:
    - If the leaderboard is category-filtered (Male/Female/Inclusive), singles_rows will only include those competitors.
      We only include doubles teams where BOTH partners are in singles_rows.
    """

    # competitor_id -> total_points + name lookup (from the already-scoped singles leaderboard)
    totals_by_id = {r["competitor_id"]: r["total_points"] for r in singles_rows}
    name_by_id = {r["competitor_id"]: r["name"] for r in singles_rows}

    teams = DoublesTeam.query.filter_by(competition_id=competition_id).all()

    doubles_rows = []
    for t in teams:
        a_id = t.competitor_a_id
        b_id = t.competitor_b_id

        # Only include teams where BOTH partners are in the current singles_rows scope
        # (so category leaderboards behave sensibly)
        if a_id not in totals_by_id or b_id not in totals_by_id:
            continue

        a_pts = totals_by_id.get(a_id, 0)
        b_pts = totals_by_id.get(b_id, 0)

        doubles_rows.append({
            "team_id": t.id,
            "a_id": a_id,
            "b_id": b_id,
            "a_name": name_by_id.get(a_id, f"#{a_id}"),
            "b_name": name_by_id.get(b_id, f"#{b_id}"),
            "total_points": a_pts + b_pts,
        })

    # sort by total desc
    doubles_rows.sort(key=lambda r: (-r["total_points"], r["a_name"], r["b_name"]))

    # assign positions with ties sharing the same place
    pos = 0
    prev = None
    for r in doubles_rows:
        k = (r["total_points"],)
        if k != prev:
            pos += 1
        prev = k
        r["position"] = pos

    return doubles_rows



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

def send_scoring_link_via_email(email: str, comp_name: str, scoring_url: str):
    """
    Email the user a direct link to their scoring page for a comp.
    """
    # Dev / fallback path
    if not RESEND_API_KEY:
        print(f"[SCORING LINK - DEV ONLY] {email} -> {scoring_url}", file=sys.stderr)
        return

    html = f"""
      <div style="font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; padding: 16px;">
        <p>Hey climber 👋</p>
        <p>Your scoring link for <strong>{comp_name}</strong> is ready:</p>
        <p style="margin: 12px 0;">
          <a href="{scoring_url}" style="display: inline-block; padding: 10px 14px; border-radius: 10px; background: #1a2942; color: #fff; text-decoration: none;">
            Open scoring
          </a>
        </p>
        <p style="color:#667; font-size: 13px;">If the button doesn’t work, copy/paste this link:</p>
        <p style="font-size: 12px; word-break: break-all;">{scoring_url}</p>
      </div>
    """

    try:
        params = {
            "from": RESEND_FROM_EMAIL,
            "to": [email],
            "subject": f"Your scoring link — {comp_name}",
            "html": html,
        }
        resend.Emails.send(params)
        print(f"[SCORING LINK] Sent scoring link to {email}", file=sys.stderr)
    except Exception as e:
        print(f"[SCORING LINK] Failed to send via Resend: {e}", file=sys.stderr)


@app.context_processor
def inject_nav_context():
    """
    Controls whether competition-only nav appears.

    show_comp_nav is TRUE only when:
    - we have a resolved competition context
    - comp is LIVE
    - the logged-in account is REGISTERED for THAT comp (Competitor row with competition_id)
    - NOT in the middle of pending join/verify
    - NOT currently on auth/admin pages
    """

    # --- hard blocks by route (auth/admin should never show comp-only nav) ---
    path = (request.path or "")
    if (
        path.startswith("/login")
        or path.startswith("/signup")
        or path.startswith("/admin")
    ):
        return dict(nav_comp=None, show_comp_nav=False)

    comp = get_viewer_comp()

    # No comp context -> no comp nav
    if not comp:
        return dict(nav_comp=None, show_comp_nav=False)

    # Comp must be LIVE
    if not comp_is_live(comp):
        return dict(nav_comp=None, show_comp_nav=False)

    # --- Pending join/verify flags should hide comp nav (prevents bypassing flow) ---
    pending_join_slug = (session.get("pending_join_slug") or "").strip()
    pending_comp_verify = (session.get("pending_comp_verify") or "").strip()

    # If *any* pending join exists, hide comp nav
    if pending_join_slug:
        return dict(nav_comp=None, show_comp_nav=False)

    # If verify is pending for THIS comp, hide comp nav
    if pending_comp_verify and pending_comp_verify == comp.slug:
        return dict(nav_comp=None, show_comp_nav=False)

    # --- Viewer must be registered for THIS comp ---
    account_id = session.get("account_id")
    viewer_id = session.get("competitor_id")

    viewer_registered_for_comp = False

    # Preferred: account-based check (most reliable)
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
            # Optional: keep session competitor_id aligned (helps nav links behave)
            if viewer_id != registered.id:
                session["competitor_id"] = registered.id
                session["competitor_email"] = registered.email

    # Fallback: session competitor_id check (legacy / edge cases)
    if not viewer_registered_for_comp and viewer_id:
        viewer = Competitor.query.get(viewer_id)
        if viewer and viewer.competition_id == comp.id:
            viewer_registered_for_comp = True

    show_comp_nav = bool(viewer_registered_for_comp)

    # Only expose nav_comp when comp nav should actually show
    nav_comp = comp if show_comp_nav else None

    return dict(
        nav_comp=nav_comp,
        show_comp_nav=show_comp_nav,
    )



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

    IMPORTANT:
    - A single email can be registered in multiple comps (multiple Competitor rows).
    - So "Keep scoring" must link to the Competitor row for THAT competition,
      not just the current session competitor_id.
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
        if comp_is_live(c):
            status = "live"
            status_label = "This comp is live — tap to register."
            opens_at = None

        elif comp_is_finished(c):
            status = "finished"
            status_label = "This comp has finished — registration is closed."
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

        # --- IMPORTANT: resolve the correct competitor row for THIS comp ---
        my_scoring_url = None
        if competitor and competitor.email:
            competitor_for_comp = (
                Competitor.query
                .filter(
                    Competitor.email == competitor.email,
                    Competitor.competition_id == c.id,
                )
                .first()
            )

            if competitor_for_comp:
                if c.slug:
                    my_scoring_url = f"/comp/{c.slug}/competitor/{competitor_for_comp.id}/sections"
                else:
                    my_scoring_url = f"/competitor/{competitor_for_comp.id}/sections"

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

@app.route("/login/verify", methods=["GET", "POST"])
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


@app.route("/competitor/<int:competitor_id>")
def competitor_redirect(competitor_id):
    """
    Canonical redirect for a competitor "profile" URL.

    Goal:
    - If this competitor belongs to a competition with a slug, redirect to the
      competition-scoped sections URL:
        /comp/<slug>/competitor/<id>/sections

    - Otherwise, fall back to the legacy sections URL:
        /competitor/<id>/sections

    Notes:
    - This route should never render a template.
    - It’s safe for shared links / old emails / old QR codes.
    """
    comp = Competitor.query.get_or_404(competitor_id)

    # If competitor is attached to a competition, prefer slugged canonical route
    if comp.competition_id:
        comp_row = Competition.query.get(comp.competition_id)
        if comp_row and comp_row.slug:
            return redirect(f"/comp/{comp_row.slug}/competitor/{competitor_id}/sections")

    # Fallback: legacy route
    return redirect(f"/competitor/{competitor_id}/sections")


@app.route("/competitor/<int:competitor_id>/sections")
def competitor_sections(competitor_id):
    """
    Sections index page (legacy URL).

    Rules:
    - Non-admins are forced to their own competitor id from the session.
    - If the competitor row is not registered for a competition -> kick to /my-comps.
    - If competition exists and has a slug -> redirect to slugged route.
    - Only allow access when that competition is LIVE.
    - Leaderboard + sections + map dots are scoped to THIS competition (not "active comp").
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

    competitor = Competitor.query.get_or_404(target_id)

    # Must belong to a competition. If not, this is an "Account" row or stale session.
    if not competitor.competition_id:
        session.pop("active_comp_slug", None)
        flash("You’re not registered in a competition yet. Pick a comp to join.", "warning")
        return redirect("/my-comps")

    comp_row = Competition.query.get(competitor.competition_id)
    if not comp_row:
        session.pop("active_comp_slug", None)
        flash("That competition no longer exists. Please join again.", "warning")
        return redirect("/my-comps")

    # If the comp has a slug, push everyone to the canonical slugged route.
    # (This prevents legacy routes from becoming the main flow.)
    if comp_row.slug:
        return redirect(f"/comp/{comp_row.slug}/competitor/{target_id}/sections")

    # LIVE gate (scheduled or finished comps should not allow scoring/nav pages)
    if not comp_is_live(comp_row):
        session.pop("active_comp_slug", None)
        if comp_is_finished(comp_row):
            flash("That competition has finished — scoring is locked.", "warning")
        else:
            flash("That competition isn’t live yet — scoring will open when it starts.", "warning")
        return redirect("/my-comps")

    # Only enforce gym-level permissions when an admin is viewing SOMEONE ELSE
    if is_admin and viewer_id and target_id != viewer_id:
        if not admin_can_manage_competition(comp_row):
            abort(403)

    # --- Gym map + gym name (DB-driven) ---
    gym_name = None
    gym_map_path = None
    if comp_row.gym:
        gym_name = comp_row.gym.name
        gym_map_path = comp_row.gym.map_image_path

    # Legacy var (keep during transition)
    gym_map_url = get_gym_map_url_for_competition(comp_row)

    # Scope sections to THIS competition
    sections = (
        Section.query
        .filter(Section.competition_id == comp_row.id)
        .order_by(Section.name)
        .all()
    )

    total_points = competitor_total_points(target_id, comp_row.id)

    # IMPORTANT: Leaderboard must be scoped to THIS competition
    rows, _ = build_leaderboard(None, competition_id=comp_row.id)
    position = None
    for r in rows:
        if r["competitor_id"] == target_id:
            position = r["position"]
            break

    can_edit = (viewer_id == target_id or is_admin)

    # Map dots: only climbs with coords for THIS competition’s sections
    section_ids = [s.id for s in sections]
    if section_ids:
        q = (
            SectionClimb.query
            .filter(
                SectionClimb.section_id.in_(section_ids),
                SectionClimb.x_percent.isnot(None),
                SectionClimb.y_percent.isnot(None),
            )
            .order_by(SectionClimb.climb_number)
        )

        # Optional safety: if SectionClimb has gym_id populated, keep it consistent
        if comp_row.gym_id:
            q = q.filter(SectionClimb.gym_id == comp_row.gym_id)

        map_climbs = q.all()
    else:
        map_climbs = []

    return render_template(
        "competitor_sections.html",
        competitor=competitor,
        sections=sections,
        total_points=total_points,
        position=position,
        nav_active="sections",
        viewer_id=viewer_id,
        is_admin=is_admin,
        can_edit=can_edit,
        map_climbs=map_climbs,
        comp=comp_row,
        comp_slug=None,  # legacy route has no slug canonical

        # New template vars
        gym_name=gym_name,
        gym_map_path=gym_map_path,

        # Legacy
        gym_map_url=gym_map_url,
    )
    
def _utcnow():
    return datetime.now(timezone.utc)

def _make_token() -> str:
    return secrets.token_urlsafe(32)

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@app.route("/comp/<slug>/doubles/invite", methods=["POST"])
def doubles_invite(slug):
    viewer_id = session.get("competitor_id")
    if not viewer_id:
        return redirect(url_for("login", next=request.path))

    comp = Competition.query.filter_by(slug=slug).first_or_404()

    me = Competitor.query.filter_by(id=viewer_id, competition_id=comp.id).first()
    if not me:
        abort(403)

    # 1) locked already?
    existing_team = DoublesTeam.query.filter(
        DoublesTeam.competition_id == comp.id,
        ((DoublesTeam.competitor_a_id == viewer_id) | (DoublesTeam.competitor_b_id == viewer_id))
    ).first()
    if existing_team:
        flash("You’re already locked into a doubles team for this comp.", "error")
        return redirect(f"/comp/{slug}/doubles")

    # 2) validate email
    invitee_email = (request.form.get("email") or "").strip().lower()
    if not invitee_email:
        flash("Enter an email address.", "error")
        return redirect(f"/comp/{slug}/doubles")

    my_email = (me.email or "").strip().lower()
    if my_email and invitee_email == my_email:
        flash("You can’t invite yourself. That’s just singles with extra paperwork.", "error")
        return redirect(f"/comp/{slug}/doubles")

    # 3) only one pending invite at a time
    pending = DoublesInvite.query.filter_by(
        competition_id=comp.id,
        inviter_competitor_id=viewer_id,
        status="pending"
    ).first()
    if pending:
        flash(f"You already invited {pending.invitee_email}. You can’t invite someone else until that’s resolved.", "error")
        return redirect(f"/comp/{slug}/doubles")

    # 4) create invite row
    token = _make_token()
    inv = DoublesInvite(
        competition_id=comp.id,
        inviter_competitor_id=viewer_id,
        invitee_email=invitee_email,
        token_hash=_hash_token(token),
        status="pending",
        expires_at=_utcnow() + timedelta(hours=48),
    )
    db.session.add(inv)
    db.session.commit()

    accept_url = url_for("doubles_accept", slug=slug, _external=True) + f"?token={token}"

    # 5) send doubles invite email via Resend (same pattern as login code)

    if not RESEND_API_KEY:
        print(f"[DOUBLES INVITE - DEV ONLY] {invitee_email} -> {accept_url}", file=sys.stderr)
    else:
        html = f"""
          <div style="font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; padding: 16px;">
            <p>Hey climber 👋</p>
            <p><strong>{me.name}</strong> has invited you to form a doubles team for:</p>
            <p style="font-weight: 600; margin: 8px 0;">{comp.name}</p>

            <p>Click below to accept:</p>

            <p style="margin: 16px 0;">
              <a href="{accept_url}"
                 style="display:inline-block; padding:10px 18px; border-radius:999px; background:#111; color:#fff; text-decoration:none;">
                 Accept Doubles Invite
              </a>
            </p>

            <p>This link expires in 48 hours.</p>
          </div>
        """

        try:
            params = {
                "from": RESEND_FROM_EMAIL,
                "to": [invitee_email],
                "subject": f"Doubles invite for {comp.name}",
                "html": html,
            }
            resend.Emails.send(params)
            print(f"[DOUBLES INVITE] Sent doubles invite to {invitee_email}", file=sys.stderr)
        except Exception as e:
            print(f"[DOUBLES INVITE] Failed to send via Resend: {e}", file=sys.stderr)

    flash("Invite sent. Waiting for them to accept.", "success")
    return redirect(f"/comp/{slug}/doubles")


@app.route("/comp/<slug>/doubles/accept", methods=["GET"])
def doubles_accept(slug):
    token = (request.args.get("token") or "").strip()
    if not token:
        flash("Missing doubles token.", "error")
        return redirect(f"/comp/{slug}/doubles")

    viewer_id = session.get("competitor_id")
    if not viewer_id:
        # Force login then come back here
        return redirect(url_for("login", next=request.url))

    comp = Competition.query.filter_by(slug=slug).first_or_404()

    invite = DoublesInvite.query.filter_by(
        competition_id=comp.id,
        token_hash=_hash_token(token),
        status="pending"
    ).first()

    if not invite:
        flash("That doubles link is invalid or already used.", "error")
        return redirect(f"/comp/{slug}/doubles")

    if invite.expires_at < _utcnow():
        invite.status = "expired"
        db.session.commit()
        flash("That doubles link expired. Ask them to resend.", "error")
        return redirect(f"/comp/{slug}/doubles")

    me = Competitor.query.filter_by(id=viewer_id, competition_id=comp.id).first()
    if not me:
        abort(403)

    # Make sure the logged-in user is the intended invitee
    if (me.email or "").strip().lower() != (invite.invitee_email or "").strip().lower():
        flash("This invite was sent to a different email address.", "error")
        return redirect(f"/comp/{slug}/doubles")

    # Ensure inviter isn't already locked in a team
    inviter_team = DoublesTeam.query.filter(
        DoublesTeam.competition_id == comp.id,
        ((DoublesTeam.competitor_a_id == invite.inviter_competitor_id) |
         (DoublesTeam.competitor_b_id == invite.inviter_competitor_id))
    ).first()
    if inviter_team:
        flash("The inviter is already in a doubles team. This invite can’t be used.", "error")
        invite.status = "cancelled"
        db.session.commit()
        return redirect(f"/comp/{slug}/doubles")

    # Ensure invitee (me) isn't already locked in a team
    my_team = DoublesTeam.query.filter(
        DoublesTeam.competition_id == comp.id,
        ((DoublesTeam.competitor_a_id == viewer_id) | (DoublesTeam.competitor_b_id == viewer_id))
    ).first()
    if my_team:
        flash("You’re already in a doubles team. This invite can’t be used.", "error")
        invite.status = "cancelled"
        db.session.commit()
        return redirect(f"/comp/{slug}/doubles")

    # Create the team (order doesn't matter; DB unique index enforces no duplicates)
    team = DoublesTeam(
        competition_id=comp.id,
        competitor_a_id=invite.inviter_competitor_id,
        competitor_b_id=viewer_id,
    )
    db.session.add(team)

    invite.status = "accepted"
    invite.accepted_at = _utcnow()

    db.session.commit()

    flash("Doubles team created! You’re locked in and will appear on the doubles leaderboard.", "success")
    return redirect(f"/comp/{slug}/doubles")

@app.route("/comp/<slug>/doubles/cancel", methods=["POST"])
def doubles_cancel(slug):
    viewer_id = session.get("competitor_id")
    if not viewer_id:
        return redirect(url_for("login", next=request.path))

    comp = Competition.query.filter_by(slug=slug).first_or_404()

    # Only the inviter can cancel their pending invite
    inv = DoublesInvite.query.filter_by(
        competition_id=comp.id,
        inviter_competitor_id=viewer_id,
        status="pending"
    ).order_by(DoublesInvite.created_at.desc()).first()

    if not inv:
        flash("No pending invite to cancel.", "error")
        return redirect(f"/comp/{slug}/doubles")

    inv.status = "cancelled"
    db.session.commit()

    flash("Invite cancelled.", "success")
    return redirect(f"/comp/{slug}/doubles")

@app.route("/comp/<slug>/doubles/resend", methods=["POST"])
def doubles_resend(slug):
    viewer_id = session.get("competitor_id")
    if not viewer_id:
        return redirect(url_for("login", next=request.path))

    comp = Competition.query.filter_by(slug=slug).first_or_404()

    me = Competitor.query.filter_by(id=viewer_id, competition_id=comp.id).first()
    if not me:
        abort(403)

    inv = DoublesInvite.query.filter_by(
        competition_id=comp.id,
        inviter_competitor_id=viewer_id,
        status="pending"
    ).order_by(DoublesInvite.created_at.desc()).first()

    if not inv:
        flash("No pending invite to resend.", "error")
        return redirect(f"/comp/{slug}/doubles")

    # Rotate token
    token = _make_token()
    inv.token_hash = _hash_token(token)
    inv.expires_at = _utcnow() + timedelta(hours=48)
    db.session.commit()

    accept_url = url_for("doubles_accept", slug=slug, _external=True) + f"?token={token}"

    # Send via Resend (same pattern as doubles_invite)
    if not RESEND_API_KEY:
        print(f"[DOUBLES INVITE RESEND - DEV ONLY] {inv.invitee_email} -> {accept_url}", file=sys.stderr)
    else:
        html = f"""
          <div style="font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; padding: 16px;">
            <p>Hey climber 👋</p>
            <p><strong>{me.name}</strong> is reminding you about a doubles invite for:</p>
            <p style="font-weight: 600; margin: 8px 0;">{comp.name}</p>

            <p>Click below to accept:</p>

            <p style="margin: 16px 0;">
              <a href="{accept_url}"
                 style="display:inline-block; padding:10px 18px; border-radius:999px; background:#111; color:#fff; text-decoration:none;">
                 Accept Doubles Invite
              </a>
            </p>

            <p>This link expires in 48 hours.</p>
          </div>
        """
        try:
            params = {
                "from": RESEND_FROM_EMAIL,
                "to": [inv.invitee_email],
                "subject": f"Reminder: Doubles invite for {comp.name}",
                "html": html,
            }
            resend.Emails.send(params)
            print(f"[DOUBLES INVITE] Resent doubles invite to {inv.invitee_email}", file=sys.stderr)
        except Exception as e:
            print(f"[DOUBLES INVITE] Failed to resend via Resend: {e}", file=sys.stderr)

    flash("Invite resent.", "success")
    return redirect(f"/comp/{slug}/doubles")


@app.route("/comp/<slug>/doubles", methods=["GET"])
def doubles_home(slug):
    viewer_id = session.get("competitor_id")
    if not viewer_id:
        return redirect(url_for("login", next=request.path))

    comp = Competition.query.filter_by(slug=slug).first_or_404()

    competitor = Competitor.query.filter_by(id=viewer_id, competition_id=comp.id).first()
    if not competitor:
        abort(403)

    # Team (if locked in)
    team = DoublesTeam.query.filter(
        DoublesTeam.competition_id == comp.id,
        ((DoublesTeam.competitor_a_id == viewer_id) | (DoublesTeam.competitor_b_id == viewer_id))
    ).first()

    partner = None
    if team:
        partner_id = team.competitor_b_id if team.competitor_a_id == viewer_id else team.competitor_a_id
        partner = Competitor.query.filter_by(id=partner_id, competition_id=comp.id).first()

    # Pending invite (if not in team)
    pending = None
    if not team:
        pending = DoublesInvite.query.filter_by(
            competition_id=comp.id,
            inviter_competitor_id=viewer_id,
            status="pending"
        ).order_by(DoublesInvite.created_at.desc()).first()

    return render_template(
        "doubles.html",
        comp=comp,
        competitor=competitor,
        comp_slug=slug,
        nav_active="doubles",
        team=team,
        partner=partner,
        pending=pending,
    )



@app.route("/comp/<slug>/competitor/<int:competitor_id>/sections")
def comp_competitor_sections(slug, competitor_id):
    """
    Competitor scoring page (comp-scoped).

    Key rule:
    - If the logged-in account is registered for this competition, allow them to score
      regardless of admin_ok.
    - Admin powers are additive (view others), not restrictive.
    """

    current_comp = Competition.query.filter_by(slug=slug).first_or_404()

    account_id = session.get("account_id")
    is_admin = session.get("admin_ok", False)

    if not account_id and not is_admin:
        return redirect("/")

    acct = Account.query.get(account_id) if account_id else None
    if account_id and not acct:
        # stale session
        for k in ["account_id", "competitor_id", "active_comp_slug", "competitor_email"]:
            session.pop(k, None)
        return redirect("/login")

    # 1) If logged-in account exists, try to resolve THEIR registered competitor row for this comp
    registered = None
    if acct:
        registered = (
            Competitor.query
            .filter(
                Competitor.account_id == acct.id,
                Competitor.competition_id == current_comp.id,
            )
            .first()
        )

    # 2) NORMAL COMPETITOR ACCESS (preferred, even if admin_ok=True)
    if registered:
        # Heal stale URLs: always force to the correct competitor id for this account+comp
        if competitor_id != registered.id:
            return redirect(f"/comp/{slug}/competitor/{registered.id}/sections")

        # Establish correct scoring context
        session["competitor_id"] = registered.id
        session["competitor_email"] = registered.email or (acct.email if acct else None)
        session["active_comp_slug"] = slug

        target_id = registered.id
        can_edit = True

    # 3) NOT REGISTERED: allow ADMIN VIEW (with gym permission gate)
    else:
        if not is_admin:
            # Not registered and not admin -> must join
            session.pop("competitor_id", None)
            session.pop("active_comp_slug", None)
            return redirect(f"/comp/{slug}/join")

        # Admin viewing a competitor (must be in this comp)
        comp = Competitor.query.get_or_404(competitor_id)
        if not comp.competition_id or comp.competition_id != current_comp.id:
            abort(404)

        if not admin_can_manage_competition(current_comp):
            abort(403)

        target_id = comp.id
        can_edit = True

    # --- Gym map + gym name (DB-driven) ---
    gym_name = None
    gym_map_path = None
    if current_comp.gym:
        gym_name = current_comp.gym.name
        gym_map_path = current_comp.gym.map_image_path

    gym_map_url = get_gym_map_url_for_competition(current_comp)

    # Sections scoped to THIS competition
    sections = (
        Section.query
        .filter(Section.competition_id == current_comp.id)
        .order_by(Section.name)
        .all()
    )

    total_points = competitor_total_points(target_id, current_comp.id)

    rows, _ = build_leaderboard(None, competition_id=current_comp.id)
    position = None
    for r in rows:
        if r["competitor_id"] == target_id:
            position = r["position"]
            break

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
        if current_comp.gym_id:
            q = q.filter(SectionClimb.gym_id == current_comp.gym_id)

        map_climbs = q.order_by(SectionClimb.climb_number).all()
    else:
        map_climbs = []

    comp_row = Competitor.query.get_or_404(target_id)

    return render_template(
        "competitor_sections.html",
        competitor=comp_row,
        sections=sections,
        total_points=total_points,
        position=position,
        nav_active="sections",
        viewer_id=session.get("competitor_id"),
        is_admin=is_admin,
        can_edit=can_edit,
        map_climbs=map_climbs,
        comp=current_comp,
        comp_slug=slug,
        gym_name=gym_name,
        gym_map_path=gym_map_path,
        gym_map_url=gym_map_url,
    )



# --- Competitor stats page: My Stats + Overall Stats ---

@app.route("/comp/<slug>/competitor/<int:competitor_id>/stats")
@app.route("/comp/<slug>/competitor/<int:competitor_id>/stats/<string:mode>")
def comp_competitor_stats(slug, competitor_id, mode="my"):
    """
    Stats for a competitor, scoped to a specific competition slug.

    HARD RULE:
    - If comp is NOT LIVE, stats are unavailable.
    - If comp is FINISHED, stats are locked. (handled explicitly too)

    mode:
    - "my"       personal stats
    - "overall"  overall stats
    - "climber"  spectator-ish view of a competitor (still blocked if not live)
    """
    current_comp = get_comp_or_404(slug)

    # Block anything not live (scheduled or finished)
    if not comp_is_live(current_comp):
        # If it’s finished, be explicit
        if comp_is_finished(current_comp):
            flash("That competition has finished — stats are locked.", "warning")
        else:
            flash("That competition isn’t live yet — stats aren’t available.", "warning")

        # prevent stale nav context hanging around
        session.pop("active_comp_slug", None)
        return redirect("/my-comps")

    # Normalise mode
    mode = (mode or "my").lower()
    if mode not in ("my", "overall", "climber"):
        mode = "my"

    comp = Competitor.query.get_or_404(competitor_id)

    # Competitor must belong to this competition
    if comp.competition_id != current_comp.id:
        abort(404)

    total_points = competitor_total_points(competitor_id, current_comp.id)

    # Who is viewing?
    viewer_id = session.get("competitor_id")
    viewer_is_self = (viewer_id == competitor_id)
    is_admin = session.get("admin_ok", False)

    # Optional public view flag (still requires comp live)
    view_mode = request.args.get("view", "").lower()
    is_public_view = (view_mode == "public" and not viewer_is_self)

    # If not self and not admin, allow only public view
    if not viewer_is_self and not is_admin and not is_public_view:
        return redirect(f"/comp/{slug}/competitor/{viewer_id}/stats/{mode}") if viewer_id else redirect("/")

    # Sections only for this competition
    sections = (
        Section.query
        .filter_by(competition_id=current_comp.id)
        .order_by(Section.name)
        .all()
    )

    # Personal scores
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
            {"attempts_total": 0, "tops": 0, "flashes": 0, "competitors": set()},
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
                sec_points += points_for(score.climb_number, score.attempts, score.topped, current_comp.id)

                if score.topped and score.attempts == 1:
                    status = "flashed"
                elif score.topped:
                    status = "topped-late"
                else:
                    status = "not-topped"
            else:
                status = "skipped"

            personal_cells.append({"climb_number": sc.climb_number, "status": status})

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

            global_cells.append({"climb_number": sc.climb_number, "status": g_status})

        efficiency = (sec_tops / sec_attempts) if sec_attempts > 0 else 0.0

        section_stats.append(
            {"section": sec, "tops": sec_tops, "attempts": sec_attempts, "efficiency": efficiency, "points": sec_points}
        )

        personal_heatmap_sections.append({"section": sec, "climbs": personal_cells})
        global_heatmap_sections.append({"section": sec, "climbs": global_cells})

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

    HARD RULE:
    - Only available when there is a LIVE competition in context.
    """
    comp = get_viewer_comp() or get_current_comp()

    if not comp or not comp_is_live(comp):
        session.pop("active_comp_slug", None)
        flash("There’s no live competition right now — climb stats are unavailable.", "warning")
        return redirect("/my-comps")

    # Mode selection
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
            total_points = competitor_total_points(competitor.id, comp.id)
            rows, _ = build_leaderboard(None, competition_id=comp.id)
            for r in rows:
                if r["competitor_id"] == competitor.id:
                    position = r["position"]
                    break

    comp_sections = Section.query.filter(Section.competition_id == comp.id).all()
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
        nav_active = "climber_stats" if from_climber else ("my_stats" if mode == "personal" else "overall_stats")
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
            Competitor.competition_id == comp.id,
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
    avg_attempts_per_comp = (total_attempts / num_competitors) if num_competitors > 0 else 0.0
    avg_attempts_on_tops = (sum(s.attempts for s in scores if s.topped) / tops) if tops > 0 else 0.0

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
        comps = {c.id: c for c in Competitor.query.filter(Competitor.id.in_(competitor_ids)).all()}

    per_competitor = []
    for s in scores:
        c = comps.get(s.competitor_id)
        per_competitor.append(
            {
                "competitor_id": s.competitor_id,
                "name": c.name if c else f"#{s.competitor_id}",
                "attempts": s.attempts,
                "topped": s.topped,
                "points": points_for(s.climb_number, s.attempts, s.topped, comp.id),
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

    nav_active = "climber_stats" if from_climber else ("my_stats" if mode == "personal" else "overall_stats")

    return render_template(
        "climb_stats.html",
        climb_number=climb_number,
        has_config=True,
        sections=[sections_by_id[sc.section_id] for sc in section_climbs if sc.section_id in sections_by_id],
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
def comp_competitor_section_climbs(slug, competitor_id, section_slug):
    """
    Key fix:
    - Build `existing` keyed by section_climb_id (matches DB uniqueness)
    - Also provide `existing_by_number` for backward compatibility
    """

    current_comp = get_comp_or_404(slug)

    if not comp_is_live(current_comp):
        session.pop("active_comp_slug", None)
        if comp_is_finished(current_comp):
            flash("That competition has finished — scoring is locked.", "warning")
        else:
            flash("That competition isn’t live yet — scoring isn’t available.", "warning")
        return redirect("/my-comps")

    viewer_id = session.get("competitor_id")
    is_admin = session.get("admin_ok", False)
    account_id = session.get("account_id")

    if not viewer_id and not is_admin and not account_id:
        return redirect("/")

    # Resolve target competitor:
    # - If logged-in account is registered for this comp, force to that competitor row
    target_id = None
    if account_id:
        acct = Account.query.get(account_id)
        if acct:
            registered = (
                Competitor.query
                .filter(
                    Competitor.account_id == acct.id,
                    Competitor.competition_id == current_comp.id,
                )
                .first()
            )
            if registered:
                target_id = registered.id
                # heal session
                session["competitor_id"] = registered.id
                session["competitor_email"] = registered.email or acct.email
                session["active_comp_slug"] = current_comp.slug

    if target_id is None:
        # fall back to admin / legacy behaviour
        if is_admin:
            target_id = competitor_id
        else:
            target_id = viewer_id
            if not target_id:
                return redirect("/")
            if competitor_id != target_id:
                return redirect(f"/comp/{slug}/competitor/{target_id}/section/{section_slug}")

    competitor = Competitor.query.get_or_404(target_id)

    if not competitor.competition_id:
        session.pop("active_comp_slug", None)
        flash("You’re not registered in a competition yet. Pick a comp to join.", "warning")
        return redirect("/my-comps")

    if competitor.competition_id != current_comp.id:
        abort(404)

    # Admin permission if viewing someone else
    if is_admin and viewer_id and target_id != viewer_id:
        if not admin_can_manage_competition(current_comp):
            abort(403)

    section = (
        Section.query
        .filter_by(slug=section_slug, competition_id=current_comp.id)
        .first_or_404()
    )

    all_sections = (
        Section.query
        .filter_by(competition_id=current_comp.id)
        .order_by(Section.name)
        .all()
    )

    rows, _ = build_leaderboard(None, competition_id=current_comp.id)
    position = next((r["position"] for r in rows if r["competitor_id"] == target_id), None)

    # IMPORTANT: include climbs even if coords are missing (so score cards exist)
    section_climbs = (
        SectionClimb.query
        .filter(SectionClimb.section_id == section.id)
        .order_by(SectionClimb.climb_number)
        .all()
    )

    # UI card list uses climb numbers (fine), but SCORE LOOKUP should use section_climb_id
    climbs = [sc.climb_number for sc in section_climbs]

    colours = {sc.climb_number: sc.colour for sc in section_climbs if sc.colour}
    max_points = {sc.climb_number: sc.base_points for sc in section_climbs if sc.base_points is not None}

    # Pull all scores for this competitor (scoped to this comp)
    scores = (
        Score.query
        .join(Competitor, Competitor.id == Score.competitor_id)
        .filter(
            Score.competitor_id == target_id,
            Competitor.competition_id == current_comp.id,
        )
        .all()
    )

    # FIX: index by section_climb_id (source of truth)
    existing = {s.section_climb_id: s for s in scores if s.section_climb_id is not None}

    # Backward-compatible index by climb_number (only safe if comp has unique climb_numbers)
    existing_by_number = {s.climb_number: s for s in scores}

    per_climb_points = {
        s.climb_number: points_for(s.climb_number, s.attempts, s.topped, current_comp.id)
        for s in scores
    }

    total_points = competitor_total_points(target_id, current_comp.id)
    gym_map_url = get_gym_map_url_for_competition(current_comp)

    return render_template(
        "competitor.html",
        competitor=competitor,
        climbs=climbs,
        existing=existing,                      # NEW: keyed by section_climb_id
        existing_by_number=existing_by_number,  # Legacy helper for templates still using climb_number
        total_points=total_points,
        section=section,
        colours=colours,
        position=position,
        max_points=max_points,
        per_climb_points=per_climb_points,
        nav_active="sections",
        can_edit=True,
        viewer_id=session.get("competitor_id"),
        is_admin=is_admin,
        section_climbs=section_climbs,
        sections=all_sections,
        gym_map_url=gym_map_url,
        comp=current_comp,
        comp_slug=slug,
    )



@app.route("/competitor/<int:competitor_id>/section/<section_slug>")
def competitor_section_climbs(competitor_id, section_slug):
    """
    DROP-IN replacement for legacy per-section route.

    Key fix:
    - Still supports old URLs, but builds `existing` keyed by section_climb_id
    - If competitor is in a slugged comp, redirect to canonical route
    """

    comp = Competitor.query.get_or_404(competitor_id)

    if comp.competition_id:
        comp_row = Competition.query.get(comp.competition_id)
        if comp_row and comp_row.slug:
            return redirect(
                f"/comp/{comp_row.slug}/competitor/{competitor_id}/section/{section_slug}"
            )

    viewer_id = session.get("competitor_id")
    is_admin = session.get("admin_ok", False)

    if not viewer_id and not is_admin:
        return redirect("/")

    target_id = competitor_id if is_admin else viewer_id
    if not target_id:
        return redirect("/")
    if not is_admin and competitor_id != target_id:
        return redirect(f"/competitor/{target_id}/section/{section_slug}")

    competitor = Competitor.query.get_or_404(target_id)

    # Legacy sections are not competition scoped; keep behaviour, but safer to require section exists
    section = Section.query.filter_by(slug=section_slug).first_or_404()
    all_sections = Section.query.order_by(Section.name).all()

    # climbs in this section (include even without coords so score cards exist)
    section_climbs = (
        SectionClimb.query
        .filter(SectionClimb.section_id == section.id)
        .order_by(SectionClimb.climb_number)
        .all()
    )

    climbs = [sc.climb_number for sc in section_climbs]
    colours = {sc.climb_number: sc.colour for sc in section_climbs if sc.colour}
    max_points = {sc.climb_number: sc.base_points for sc in section_climbs if sc.base_points is not None}

    # Scores (legacy: unscoped if no competition context)
    if competitor.competition_id:
        comp_row = Competition.query.get(competitor.competition_id)
        scores = (
            Score.query
            .join(Competitor, Competitor.id == Score.competitor_id)
            .filter(
                Score.competitor_id == target_id,
                Competitor.competition_id == comp_row.id,
            )
            .all()
        )
        per_climb_points = {
            s.climb_number: points_for(s.climb_number, s.attempts, s.topped, comp_row.id)
            for s in scores
        }
        total_points = competitor_total_points(target_id, comp_row.id)
    else:
        scores = Score.query.filter_by(competitor_id=target_id).all()
        per_climb_points = {
            s.climb_number: points_for(s.climb_number, s.attempts, s.topped)
            for s in scores
        }
        total_points = competitor_total_points(target_id)

    # FIX: index by section_climb_id
    existing = {s.section_climb_id: s for s in scores if s.section_climb_id is not None}
    existing_by_number = {s.climb_number: s for s in scores}

    rows, _ = build_leaderboard(None, competition_id=competitor.competition_id) if competitor.competition_id else build_leaderboard(None)
    position = next((r["position"] for r in rows if r["competitor_id"] == target_id), None)

    return render_template(
        "competitor.html",
        competitor=competitor,
        climbs=climbs,
        existing=existing,                      
        existing_by_number=existing_by_number,  # legacy helper
        total_points=total_points,
        section=section,
        colours=colours,
        position=position,
        max_points=max_points,
        per_climb_points=per_climb_points,
        nav_active="sections",
        can_edit=True,
        viewer_id=viewer_id,
        is_admin=is_admin,
        section_climbs=section_climbs,
        sections=all_sections,
        gym_map_url=None,
    )


# --- Register new competitors (staff use only, separate page for now) ---


@app.route("/register", methods=["GET", "POST"])
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

@app.route("/comp/<slug>/join", methods=["GET", "POST"])
def public_register_for_comp(slug):
    comp = get_comp_or_404(slug)

    # Competition must be live
    if not comp_is_live(comp):
        flash("That competition isn’t live — registration is closed.", "warning")
        for k in [
            "pending_join_slug",
            "pending_join_name",
            "pending_join_gender",
            "pending_comp_verify",
            "active_comp_slug",
        ]:
            session.pop(k, None)
        return redirect("/my-comps")

    # Must have an account_id in session (NOT competitor_id, NOT competitor_email)
    account_id = session.get("account_id")
    if not account_id:
        # IMPORTANT: when not logged in, do NOT establish comp context in session
        session.pop("competitor_id", None)
        session.pop("active_comp_slug", None)

        flash("Please log in first to join this competition.", "warning")
        next_path = f"/comp/{comp.slug}/join"
        return redirect(f"/login?slug={comp.slug}&next={quote(next_path)}")

    # Fetch account (now safe)
    acct = Account.query.get(account_id)
    if not acct:
        # Session is stale/bad: clear and force login
        for k in ["account_id", "competitor_id", "competitor_email", "active_comp_slug"]:
            session.pop(k, None)

        flash("Please log in again to continue.", "warning")
        next_path = f"/comp/{comp.slug}/join"
        return redirect(f"/login?slug={comp.slug}&next={quote(next_path)}")

    # Check if this ACCOUNT is already registered for THIS comp
    existing_for_comp = (
        Competitor.query
        .filter(
            Competitor.account_id == acct.id,
            Competitor.competition_id == comp.id,
        )
        .first()
    )

    # If already registered -> establish comp context + competitor_id and go score
    if existing_for_comp and request.method == "GET":
        session["competitor_id"] = existing_for_comp.id
        session["competitor_email"] = acct.email
        session["active_comp_slug"] = comp.slug
        session.pop("pending_comp_verify", None)
        return redirect(f"/comp/{comp.slug}/competitor/{existing_for_comp.id}/sections")

    # Not registered yet (GET): make sure there is NO sticky scoring context
    if request.method == "GET" and not existing_for_comp:
        session.pop("competitor_id", None)
        session.pop("active_comp_slug", None)
        # also clear any stale pending join state for a different comp
        session.pop("pending_comp_verify", None)

        return render_template(
            "register_public.html",
            comp=comp,
            error=None,
            name="",
            gender="Inclusive",
        )

    # POST: attempt to register (this is where we *can* establish comp context)
    name = (request.form.get("name") or "").strip()
    gender = (request.form.get("gender") or "Inclusive").strip()
    if gender not in ("Male", "Female", "Inclusive"):
        gender = "Inclusive"

    if not name:
        return render_template(
            "register_public.html",
            comp=comp,
            error="Please enter your name.",
            name=name,
            gender=gender,
        )

    # If already registered (POST) -> go score (and set context)
    if existing_for_comp:
        session["competitor_id"] = existing_for_comp.id
        session["competitor_email"] = acct.email
        session["active_comp_slug"] = comp.slug
        session.pop("pending_comp_verify", None)
        return redirect(f"/comp/{comp.slug}/competitor/{existing_for_comp.id}/sections")

    # Store pending join details (for delayed verify)
    session["pending_join_slug"] = comp.slug
    session["pending_join_name"] = name
    session["pending_join_gender"] = gender
    session["pending_comp_verify"] = comp.slug

    session["login_email"] = acct.email
    session["competitor_email"] = acct.email

    # IMPORTANT: we can set active_comp_slug during the verify flow
    # because user is actively joining this comp now
    session["active_comp_slug"] = comp.slug

    # Send code against ACCOUNT
    code = f"{secrets.randbelow(1_000_000):06d}"
    now = datetime.utcnow()

    # Need a shell competitor_id for legacy LoginCode column
    shell = (
        Competitor.query
        .filter(
            Competitor.account_id == acct.id,
            Competitor.competition_id.is_(None),
        )
        .order_by(Competitor.created_at.desc())
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

    login_code = LoginCode(
        competitor_id=shell.id,    # legacy
        account_id=acct.id,        # real
        code=code,
        created_at=now,
        expires_at=now + timedelta(minutes=10),
        used=False,
    )
    db.session.add(login_code)
    db.session.commit()

    send_login_code_via_email(acct.email, code)

    flash(f"We sent a 6-digit code to {acct.email}.", "success")
    next_path = f"/comp/{comp.slug}/join"
    return redirect(f"/login/verify?slug={comp.slug}&next={quote(next_path)}")

    
@app.route("/logout")
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


@app.route("/join", methods=["GET", "POST"])
@app.route("/join/", methods=["GET", "POST"])
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



# --- Score API ---

@app.route("/api/score", methods=["POST"])
def api_save_score():
    """
    Save/upsert a score.

    Preferred payload (new):
      {
        "competitor_id": 123,
        "section_climb_id": 456,
        "attempts": 2,
        "topped": true
      }

    Legacy payload (still supported):
      {
        "competitor_id": 123,
        "climb_number": 17,
        "attempts": 2,
        "topped": true
      }

    Why:
    - DB uniqueness is (competitor_id, section_climb_id)
    - climb_number alone can be ambiguous if the same number exists in multiple sections
    """

    data = request.get_json(force=True, silent=True) or {}

    # ---- parse basics ----
    try:
        competitor_id = int(data.get("competitor_id", 0))
    except (TypeError, ValueError):
        return "Invalid competitor_id", 400

    # attempts + topped
    try:
        attempts = int(data.get("attempts", 1))
    except (TypeError, ValueError):
        attempts = 1
    topped = bool(data.get("topped", False))

    # payload may contain either section_climb_id or climb_number
    section_climb_id_raw = data.get("section_climb_id", None)
    climb_number_raw = data.get("climb_number", None)

    section_climb_id = None
    climb_number = None

    if section_climb_id_raw is not None:
        try:
            section_climb_id = int(section_climb_id_raw)
        except (TypeError, ValueError):
            return "Invalid section_climb_id", 400

    if climb_number_raw is not None:
        try:
            climb_number = int(climb_number_raw)
        except (TypeError, ValueError):
            return "Invalid climb_number", 400

    if competitor_id <= 0:
        return "Invalid competitor_id", 400

    if section_climb_id is None and (climb_number is None or climb_number <= 0):
        return "Missing section_climb_id or climb_number", 400

    # ---- Auth: competitor themself, admin, or local sim in debug ----
    viewer_id = session.get("competitor_id")
    is_admin = session.get("admin_ok", False)

    if (
        not viewer_id
        and not is_admin
        and app.debug
        and request.remote_addr in ("127.0.0.1", "::1")
    ):
        is_admin = True

    if viewer_id != competitor_id and not is_admin:
        return "Not allowed", 403

    # ---- competitor + comp context ----
    comp_row = Competitor.query.get(competitor_id)
    if not comp_row:
        return "Competitor not found", 404

    if not comp_row.competition_id:
        return "Competitor not registered for a competition", 400

    current_comp = Competition.query.get(comp_row.competition_id)
    if not current_comp:
        return "Competition not found", 404

    # Block edits once the comp is finished
    if comp_is_finished(current_comp):
        return "Competition finished — scoring locked", 403

    # ---- resolve SectionClimb (source of truth) ----
    sc = None

    if section_climb_id is not None:
        sc = SectionClimb.query.get(section_climb_id)
        if not sc:
            return "Unknown section_climb_id", 400

        # Ensure this section climb belongs to THIS competition
        sec = Section.query.get(sc.section_id) if sc.section_id else None
        if not sec or sec.competition_id != current_comp.id:
            return "section_climb_id not in this competition", 400

    else:
        # Legacy lookup by climb_number scoped to THIS competition
        # IMPORTANT: if duplicates exist across sections, this is ambiguous.
        matches = (
            SectionClimb.query
            .join(Section, Section.id == SectionClimb.section_id)
            .filter(
                SectionClimb.climb_number == climb_number,
                Section.competition_id == current_comp.id,
            )
            .all()
        )

        if not matches:
            return "Unknown climb number for this competition", 400

        if len(matches) > 1:
            # This is exactly the score-card bug scenario.
            # Force clients/templates to use section_climb_id.
            return (
                "Ambiguous climb_number in this competition. "
                "Send section_climb_id instead.",
                400,
            )

        sc = matches[0]

    # ---- clamp attempts ----
    if attempts < 1:
        attempts = 1
    elif attempts > 50:
        attempts = 50

    # flashed = topped on attempt 1
    flashed = bool(topped and attempts == 1)

    # ---- upsert by (competitor_id, section_climb_id) ----
    score = (
        Score.query
        .filter_by(competitor_id=competitor_id, section_climb_id=sc.id)
        .first()
    )

    if not score:
        score = Score(
            competitor_id=competitor_id,
            section_climb_id=sc.id,
            climb_number=sc.climb_number,  # keep for stats/ordering
            attempts=attempts,
            topped=topped,
            flashed=flashed,
        )
        db.session.add(score)
    else:
        score.climb_number = sc.climb_number
        score.attempts = attempts
        score.topped = topped
        score.flashed = flashed

    db.session.commit()
    invalidate_leaderboard_cache()

    points = points_for(sc.climb_number, attempts, topped, current_comp.id)

    return jsonify(
        {
            "ok": True,
            "competitor_id": competitor_id,
            "climb_number": sc.climb_number,
            "section_climb_id": sc.id,
            "attempts": attempts,
            "topped": topped,
            "flashed": flashed,
            "points": points,
        }
    )


@app.route("/api/score/<int:competitor_id>")
def api_get_scores(competitor_id):
    """
    Return all scores for this competitor.

    IMPORTANT:
    - Returns BOTH section_climb_id and climb_number so the UI can map correctly.
    - Points are scoped to the competitor's competition.
    """

    competitor = Competitor.query.get_or_404(competitor_id)

    scores = (
        Score.query
        .filter_by(competitor_id=competitor_id)
        .order_by(Score.climb_number.asc(), Score.section_climb_id.asc())
        .all()
    )

    comp_id = competitor.competition_id

    out = []
    for s in scores:
        out.append(
            {
                "climb_number": s.climb_number,
                "section_climb_id": s.section_climb_id,
                "attempts": s.attempts,
                "topped": s.topped,
                "flashed": getattr(s, "flashed", False),
                "points": points_for(s.climb_number, s.attempts, s.topped, comp_id),
            }
        )

    return jsonify(out)

# --- Leaderboard pages ---

@app.route("/leaderboard")
def leaderboard_all():
    """
    Leaderboard page for the currently selected competition context.

    Rules:
    - Must have a selected competition context (get_viewer_comp()).
    - That competition must be LIVE to view leaderboard.
      (If you later want finished comps viewable read-only, we can relax this.)
    """
    # Optional highlighting of a competitor row
    cid_raw = (request.args.get("cid") or "").strip()
    competitor = Competitor.query.get(int(cid_raw)) if cid_raw.isdigit() else None

    comp = get_viewer_comp()

    # No comp context at all
    if not comp:
        flash("Pick a competition first to view the leaderboard.", "warning")
        return redirect("/my-comps")

    # Comp exists but isn't live -> clear stale comp context and bounce
    if not comp_is_live(comp):
        # Prevent stale slug from keeping comp-nav alive
        session.pop("active_comp_slug", None)
        flash("That competition isn’t live right now — leaderboard is unavailable.", "warning")
        return redirect("/my-comps")

    rows, category_label = build_leaderboard(None, competition_id=comp.id)
    doubles_rows = build_doubles_rows(rows, comp.id)

    current_competitor_id = session.get("competitor_id")

    return render_template(
        "leaderboard.html",
        leaderboard=rows,
        doubles_leaderboard=doubles_rows,
        category=category_label,
        competitor=competitor,
        current_competitor_id=current_competitor_id,
        nav_active="leaderboard",
        comp=comp,
        comp_slug=comp.slug,
    )

@app.route("/leaderboard/<category>")
def leaderboard_by_category(category):
    """
    Category leaderboard for the currently selected competition context.

    Categories:
    - all (handled by /leaderboard)
    - male / female / inclusive
    - doubles
    """
    cid_raw = (request.args.get("cid") or "").strip()
    competitor = Competitor.query.get(int(cid_raw)) if cid_raw.isdigit() else None

    comp = get_viewer_comp()

    if not comp:
        flash("Pick a competition first to view the leaderboard.", "warning")
        return redirect("/my-comps")

    if not comp_is_live(comp):
        session.pop("active_comp_slug", None)
        flash("That competition isn’t live right now — leaderboard is unavailable.", "warning")
        return redirect("/my-comps")

    # Build rows for this category (including doubles)
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
    """
    JSON leaderboard for the currently selected comp context.
    Supports category=doubles as well as male/female/inclusive.
    """
    category = request.args.get("category")

    comp = get_viewer_comp()
    if not comp:
        return jsonify({"category": "No competition selected", "rows": []})

    if not comp_is_live(comp):
        session.pop("active_comp_slug", None)
        return jsonify({"category": "Competition not live", "rows": []})

    rows, category_label = build_leaderboard(category, competition_id=comp.id)

    # JSON-safe datetime conversion (singles has last_update; doubles doesn't)
    for r in rows:
        if r.get("last_update") is not None:
            r["last_update"] = r["last_update"].isoformat()

    return jsonify({"category": category_label, "rows": rows})




# --- Admin (simple password-protected utility) ---

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "letmein123")


@app.route("/admin", methods=["GET", "POST"])
def admin_page():
    message = None
    error = None
    search_results = None
    search_query = ""
    is_admin = session.get("admin_ok", False)

    def resolve_admin_current_comp():
        """
        Admin pages should prefer the admin-selected competition context (session['admin_comp_id']).
        Only fall back to get_current_comp() if no admin context exists.
        """
        admin_comp_id = session.get("admin_comp_id")
        if admin_comp_id:
            comp = Competition.query.get(admin_comp_id)
            if comp:
                return comp
            # stale session value
            session.pop("admin_comp_id", None)
        return get_current_comp()

    # If an admin_comp_id is set, ensure this admin can manage it.
    # If not, clear it to prevent confusing 403 loops.
    current_comp = resolve_admin_current_comp()
    if current_comp and session.get("admin_comp_id"):
        if not admin_can_manage_competition(current_comp):
            session.pop("admin_comp_id", None)
            current_comp = None
            error = "You don’t have access to manage that competition. Please choose a different competition."

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

                # IMPORTANT: don't keep stale editing context from past sessions
                session.pop("admin_comp_id", None)

                is_admin = True
                message = "Admin access granted."

        else:
            # For all other actions, require that admin has been unlocked
            if not is_admin:
                error = "Please enter the admin password first."
            else:
                # Always resolve comp AFTER login and before handling actions
                current_comp = resolve_admin_current_comp()

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
                    # Require a selected admin comp to avoid deleting cross-comp records
                    if not current_comp:
                        error = "No competition selected. Go to Admin → Comps and click Manage first."
                    else:
                        raw_id = request.form.get("competitor_id", "").strip()
                        if not raw_id.isdigit():
                            error = "Please provide a valid competitor number."
                        else:
                            cid = int(raw_id)
                            comp = Competitor.query.get(cid)
                            if not comp:
                                error = f"Competitor {cid} not found."
                            elif comp.competition_id != current_comp.id:
                                error = f"Competitor {cid} is not in the currently selected competition."
                            else:
                                Score.query.filter_by(competitor_id=cid).delete()
                                db.session.delete(comp)
                                db.session.commit()
                                invalidate_leaderboard_cache()
                                message = f"Competitor {cid} and their scores have been deleted."

                elif action == "create_competitor":
                    if not current_comp:
                        error = "No competition selected. Go to Admin → Comps and click Manage first."
                    else:
                        name = request.form.get("new_name", "").strip()
                        gender = request.form.get("new_gender", "Inclusive").strip()

                        if not name:
                            error = "Competitor name is required."
                        else:
                            if gender not in ("Male", "Female", "Inclusive"):
                                gender = "Inclusive"

                            comp = Competitor(
                                name=name,
                                gender=gender,
                                competition_id=current_comp.id,
                            )
                            db.session.add(comp)
                            db.session.commit()
                            invalidate_leaderboard_cache()
                            message = f"Competitor created: {comp.name} (#{comp.id}, {comp.gender})"

                elif action == "create_section":
                    if not current_comp:
                        error = "No competition selected. Go to Admin → Comps and click Manage first."
                    else:
                        name = request.form.get("section_name", "").strip()
                        if not name:
                            error = "Please provide a section name."
                        else:
                            slug = slugify(name)

                            # scoped duplicate check (competition_id + slug)
                            existing = (
                                Section.query
                                .filter_by(competition_id=current_comp.id, slug=slug)
                                .first()
                            )
                            if existing:
                                slug = f"{slug}-{int(datetime.utcnow().timestamp())}"

                            s = Section(
                                name=name,
                                slug=slug,
                                start_climb=0,
                                end_climb=0,
                                competition_id=current_comp.id,
                                gym_id=current_comp.gym_id,
                            )

                            db.session.add(s)
                            db.session.commit()
                            message = f"Section created: {name}. You can now add climbs via Edit."

                elif action == "search_competitor":
                    if not current_comp:
                        error = "No competition selected. Go to Admin → Comps and click Manage first."
                    else:
                        search_query = request.form.get("search_name", "").strip()
                        if not search_query:
                            error = "Please enter a name to search."
                        else:
                            pattern = f"%{search_query}%"
                            search_results = (
                                Competitor.query
                                .filter(
                                    Competitor.competition_id == current_comp.id,
                                    Competitor.name.ilike(pattern),
                                )
                                .order_by(Competitor.name, Competitor.id)
                                .all()
                            )
                            if not search_results:
                                message = f"No competitors found matching '{search_query}'."

    # Always resolve current_comp AFTER any POST actions too
    current_comp = resolve_admin_current_comp()

    if current_comp:
        sections = (
            Section.query
            .filter(Section.competition_id == current_comp.id)
            .order_by(Section.name)
            .all()
        )
    else:
        sections = []

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
    
@app.route("/admin/api/comp/<int:comp_id>/section-boundaries")
def admin_api_comp_section_boundaries(comp_id):
    """
    Admin-only: return boundaries for all sections in this comp.
    Works even if the comp isn't live.
    """
    if not session.get("admin_ok"):
        return jsonify({"ok": False, "error": "Not admin"}), 403

    comp = Competition.query.get_or_404(comp_id)
    if not admin_can_manage_competition(comp):
        return jsonify({"ok": False, "error": "Forbidden"}), 403

    sections = Section.query.filter(Section.competition_id == comp.id).all()

    out = {}
    for s in sections:
        pts = _parse_boundary_points(s.boundary_points_json)
        out[str(s.id)] = pts  # include empty list too (useful for UI)

    return jsonify({"ok": True, "boundaries": out})



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

    # Helper: delete scores safely (scoped to this comp/gym/section when possible)
    def _delete_scores_for_climb_number(climb_number: int):
        q = Score.query.filter(Score.climb_number == climb_number)

        # Scope to competition if the column exists
        if hasattr(Score, "competition_id"):
            q = q.filter(Score.competition_id == current_comp.id)

        # Scope to gym if the column exists
        if hasattr(Score, "gym_id") and current_comp.gym_id:
            q = q.filter(Score.gym_id == current_comp.gym_id)

        # Scope to section if the column exists
        if hasattr(Score, "section_id"):
            q = q.filter(Score.section_id == section.id)

        q.delete(synchronize_session=False)

    def _delete_scores_for_climb_numbers(climb_numbers: list[int]):
        if not climb_numbers:
            return
        q = Score.query.filter(Score.climb_number.in_(climb_numbers))

        if hasattr(Score, "competition_id"):
            q = q.filter(Score.competition_id == current_comp.id)

        if hasattr(Score, "gym_id") and current_comp.gym_id:
            q = q.filter(Score.gym_id == current_comp.gym_id)

        if hasattr(Score, "section_id"):
            q = q.filter(Score.section_id == section.id)

        q.delete(synchronize_session=False)

    error = None
    message = None

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()

        if action == "save_section":
            name = request.form.get("name", "").strip()
            if not name:
                error = "Section name is required."
            else:
                section.name = name
                db.session.commit()
                invalidate_leaderboard_cache()
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
                    # Uniqueness check should include gym_id (gym separation)
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
                        invalidate_leaderboard_cache()
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
                    # Extra safety: ensure you're not deleting across gyms
                    if current_comp.gym_id and getattr(sc, "gym_id", None) and sc.gym_id != current_comp.gym_id:
                        abort(403)

                    # Delete scores for this climb number, scoped to this comp/gym/section when possible
                    _delete_scores_for_climb_number(sc.climb_number)

                    # Then delete the climb config itself
                    db.session.delete(sc)
                    db.session.commit()
                    invalidate_leaderboard_cache()
                    message = (
                        f"Climb {sc.climb_number} removed from {section.name}, "
                        "and all associated scores were deleted."
                    )

        elif action == "delete_section":
            # Find all climb numbers in this section (and gym, if applicable)
            section_climbs_q = SectionClimb.query.filter_by(section_id=section.id)
            if hasattr(SectionClimb, "gym_id") and current_comp.gym_id:
                section_climbs_q = section_climbs_q.filter(SectionClimb.gym_id == current_comp.gym_id)

            section_climbs = section_climbs_q.all()
            climb_numbers = [sc.climb_number for sc in section_climbs]

            # Delete all scores for those climbs (scoped where possible)
            _delete_scores_for_climb_numbers(climb_numbers)

            # Delete the section's climbs (scoped where possible)
            delete_climbs_q = SectionClimb.query.filter_by(section_id=section.id)
            if hasattr(SectionClimb, "gym_id") and current_comp.gym_id:
                delete_climbs_q = delete_climbs_q.filter(SectionClimb.gym_id == current_comp.gym_id)
            delete_climbs_q.delete(synchronize_session=False)

            # Delete the section itself
            db.session.delete(section)
            db.session.commit()
            invalidate_leaderboard_cache()

            # Keep comp context on redirect
            return redirect(f"/admin/comp/{current_comp.slug}" if current_comp.slug else "/admin/comps")

        else:
            error = "Unknown action."

    climbs_q = SectionClimb.query.filter_by(section_id=section.id)
    if hasattr(SectionClimb, "gym_id") and current_comp.gym_id:
        climbs_q = climbs_q.filter(SectionClimb.gym_id == current_comp.gym_id)

    climbs = climbs_q.order_by(SectionClimb.climb_number).all()

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

    # 1) Prefer explicit comp_id in querystring
    comp_id = request.args.get("comp_id", type=int)

    # 2) Fallback to session "admin currently editing" comp
    if not comp_id:
        comp_id = session.get("admin_comp_id")

    # If still no comp context, bounce to comps list (never use public current comp)
    if not comp_id:
        flash(
            "Pick a competition first (Admin → Comps → Manage) before opening the map editor.",
            "warning",
        )
        return redirect("/admin/comps")

    current_comp = Competition.query.get(comp_id)

    # Stale/invalid comp_id in session or URL
    if not current_comp:
        session.pop("admin_comp_id", None)
        flash(
            "That competition no longer exists (or your session is stale). Please choose a competition to manage.",
            "warning",
        )
        return redirect("/admin/comps")

    # Canonicalize URL so refresh/back/share works reliably
    # (If they arrived via /admin/map without comp_id, redirect to include it.)
    if request.args.get("comp_id", type=int) != current_comp.id:
        return redirect(f"/admin/map?comp_id={current_comp.id}")

    # Only allow admins who can manage this competition's gym
    if not admin_can_manage_competition(current_comp):
        abort(403)

    # Keep session in sync so POST can always fall back safely
    session["admin_comp_id"] = current_comp.id

    gym_map_url = current_comp.gym.map_image_path if current_comp.gym else None

    sections = (
        Section.query
        .filter(Section.competition_id == current_comp.id)
        .order_by(Section.name)
        .all()
    )

    section_ids = [s.id for s in sections]
    climbs = []
    if section_ids:
        q = SectionClimb.query.filter(SectionClimb.section_id.in_(section_ids))

        # Keep gym_id filter only if SectionClimb actually stores gym_id correctly.
        if current_comp.gym_id is not None:
            q = q.filter(SectionClimb.gym_id == current_comp.gym_id)

        climbs = q.all()

    gym_name = current_comp.gym.name if getattr(current_comp, "gym", None) else None
    comp_name = current_comp.name

    return render_template(
        "admin_map.html",
        sections=sections,
        climbs=climbs,
        gym_map_url=gym_map_url,
        gym_name=gym_name,
        comp_name=comp_name,
        current_comp_id=current_comp.id,  # template uses this for hidden input + links
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

    # Persist admin editing context so /admin, /admin/map, etc. know which comp you're editing
    session["admin_comp_id"] = comp.id

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
    # Debug: if this fires, the session cookie isn't present or SECRET_KEY mismatch
    if not session.get("admin_ok"):
        print("[ADMIN MAP ADD] admin_ok missing in session. session keys:", list(session.keys()), file=sys.stderr)
        flash("Admin session missing — please log in again.", "warning")
        return redirect("/admin")

    def back(comp_id=None):
        return redirect(f"/admin/map?comp_id={comp_id}") if comp_id else redirect("/admin/map")

    # 1) Get comp_id from POST (hidden field), fallback to session
    comp_id_raw = (request.form.get("comp_id") or "").strip()
    comp_id = int(comp_id_raw) if comp_id_raw.isdigit() else session.get("admin_comp_id")

    if not comp_id:
        flash("No competition context (comp_id missing). Open the map from Admin → Comps.", "warning")
        return redirect("/admin/comps")

    current_comp = Competition.query.get(comp_id)
    if not current_comp:
        flash("Competition not found.", "warning")
        return redirect("/admin/comps")

    # Keep session in sync
    session["admin_comp_id"] = current_comp.id

    # Permission check
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

    # ---- Choose section ----
    section = None

    if section_id_raw.isdigit():
        section = Section.query.get(int(section_id_raw))
        if not section or section.competition_id != current_comp.id:
            section = None

    if not section and new_section_name:
        slug = slugify(new_section_name)
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
        db.session.flush()  # get section.id

    if not section:
        flash("Please select an existing section or type a new section name.", "warning")
        db.session.rollback()
        return back(current_comp.id)

    # ---- Validate numbers ----
    if not climb_raw.isdigit():
        flash("Climb number must be a whole number.", "warning")
        db.session.rollback()
        return back(current_comp.id)

    if base_raw == "" or penalty_raw == "" or cap_raw == "":
        flash("Base points, penalty, and attempt cap are required.", "warning")
        db.session.rollback()
        return back(current_comp.id)

    if not (base_raw.lstrip("-").isdigit() and penalty_raw.lstrip("-").isdigit() and cap_raw.lstrip("-").isdigit()):
        flash("Base points, penalty, and attempt cap must be whole numbers.", "warning")
        db.session.rollback()
        return back(current_comp.id)

    climb_number = int(climb_raw)
    base_points = int(base_raw)
    penalty_per_attempt = int(penalty_raw)
    attempt_cap = int(cap_raw)

    if climb_number <= 0:
        flash("Climb number must be positive.", "warning")
        db.session.rollback()
        return back(current_comp.id)

    if base_points < 0 or penalty_per_attempt < 0 or attempt_cap <= 0:
        flash("Base points/penalty must be ≥ 0 and attempt cap must be > 0.", "warning")
        db.session.rollback()
        return back(current_comp.id)

    # ---- Coordinates ----
    try:
        x_percent = float(x_raw)
        y_percent = float(y_raw)
    except ValueError:
        flash("You need to click the map first (missing coordinates).", "warning")
        db.session.rollback()
        return back(current_comp.id)

    # ---- Conflict check (unique climb number within this competition) ----
    conflict = (
        db.session.query(SectionClimb)
        .join(Section, SectionClimb.section_id == Section.id)
        .filter(
            Section.competition_id == current_comp.id,
            SectionClimb.climb_number == climb_number,
        )
        .first()
    )
    if conflict:
        flash(f"Climb #{climb_number} already exists in this competition.", "warning")
        db.session.rollback()
        return back(current_comp.id)

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

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        flash(f"DB error saving climb: {e}", "warning")
        return back(current_comp.id)

    flash(f"Saved climb #{climb_number}.", "success")
    return back(current_comp.id)

@app.route("/admin/map/save-boundary", methods=["POST"])
def admin_map_save_boundary():
    """
    Save polygon boundary for a section.
    Payload can be JSON or form-encoded.

    Expects:
      - comp_id
      - section_id
      - points: JSON string or list of points [{x,y},...]
    """
    if not session.get("admin_ok"):
        return jsonify({"ok": False, "error": "Not admin"}), 403

    # Accept JSON body or form
    data = request.get_json(silent=True) or request.form.to_dict(flat=True)

    comp_id_raw = (data.get("comp_id") or "").strip()
    section_id_raw = (data.get("section_id") or "").strip()
    points_raw = data.get("points")

    if not comp_id_raw.isdigit() or not section_id_raw.isdigit():
        return jsonify({"ok": False, "error": "Missing comp_id or section_id"}), 400

    comp_id = int(comp_id_raw)
    section_id = int(section_id_raw)

    current_comp = Competition.query.get(comp_id)
    if not current_comp:
        return jsonify({"ok": False, "error": "Competition not found"}), 404

    if not admin_can_manage_competition(current_comp):
        return jsonify({"ok": False, "error": "Forbidden"}), 403

    section = Section.query.get(section_id)
    if not section or section.competition_id != current_comp.id:
        return jsonify({"ok": False, "error": "Section not found in this comp"}), 404

    points = _parse_boundary_points(points_raw)

    # Require at least 3 points for a polygon, or allow empty to "clear"
    if points and len(points) < 3:
        return jsonify({"ok": False, "error": "Polygon needs at least 3 points"}), 400

    section.boundary_points_json = _boundary_to_json(points) if points else None
    db.session.commit()

    return jsonify({"ok": True, "section_id": section.id, "points": points})

@app.route("/api/comp/<slug>/section-boundaries")
def api_comp_section_boundaries(slug):
    """
    Return boundaries for all sections in a competition.
    Used by competitor_sections page to zoom to polygon bounds.
    """
    comp = Competition.query.filter_by(slug=slug).first_or_404()

    # Only allow when comp is live (consistent with your UI rules)
    if not comp_is_live(comp):
        return jsonify({"ok": True, "boundaries": {}})

    sections = (
        Section.query
        .filter(Section.competition_id == comp.id)
        .all()
    )

    out = {}
    for s in sections:
        pts = _parse_boundary_points(s.boundary_points_json)
        if pts:
            out[str(s.id)] = pts

    return jsonify({"ok": True, "boundaries": out})



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
