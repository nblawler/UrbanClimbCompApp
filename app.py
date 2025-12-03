from flask import Flask, render_template, request, redirect, jsonify, session, abort
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
	"Urban Climb Comp <onboarding@resend.dev>",  # fallback; override in Render
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


class Competition(db.Model):
	__tablename__ = "competition"

	id = db.Column(db.Integer, primary_key=True)

	# Public-facing name, e.g. "UC Collingwood Boulder Blitz"
	name = db.Column(db.String(160), nullable=False)

	# Optional extra context
	gym_name = db.Column(db.String(160), nullable=True)

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
	email = db.Column(db.String(255), nullable=True, unique=True)
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
	section_id = db.Column(db.Integer, db.ForeignKey("section.id"), nullable=False)
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

	# where this climb sits on the Collingwood map (% of width/height)
	x_percent = db.Column(db.Float, nullable=True)
	y_percent = db.Column(db.Float, nullable=True)

	section = db.relationship("Section", backref=db.backref("climbs", lazy=True))

	__table_args__ = (
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


def get_current_comp():
	"""
	For now: single active competition.
	Later we can pick by slug / subdomain / QR etc.
	"""
	comp = (
		Competition.query
		.filter_by(is_active=True)
		.order_by(Competition.start_at.asc())
		.first()
	)
	if not comp:
		abort(500, "No active competition configured")
	return comp

def get_comp_or_404(slug: str) -> Competition:
	"""
	Look up a competition by slug.
	For now we allow any slug; later you can restrict to is_active=True.
	"""
	comp = Competition.query.filter_by(slug=slug).first_or_404()
	return comp

# --- Scoring function ---


def points_for(climb_number, attempts, topped):
	"""
	Calculate points for a climb using ONLY DB config.

	Rules:
	- If not topped -> 0 points.
	- Full base points on first attempt (no penalty).
	- From attempt #2 onward, each attempt applies a penalty.
	- Penalty is capped at `attempt_cap` attempts:
		attempts beyond the cap are still recorded but do not
		reduce the score further.
	- If no SectionClimb config exists for this climb_number, or
	  config fields are missing, return 0.
	"""
	if not topped:
		return 0

	# sanity-clamp attempts recorded
	if attempts < 1:
		attempts = 1
	elif attempts > 50:
		attempts = 50

	# Per-climb config must exist in DB
	sc = SectionClimb.query.filter_by(climb_number=climb_number).first()
	if not sc or sc.base_points is None or sc.penalty_per_attempt is None:
		return 0

	base = sc.base_points
	penalty = sc.penalty_per_attempt
	cap = sc.attempt_cap if sc.attempt_cap and sc.attempt_cap > 0 else 5

	# only attempts from 2 onward incur penalty; cap at `cap`
	penalty_attempts = max(0, min(attempts, cap) - 1)

	return max(int(base - penalty * penalty_attempts), 0)


# --- Helpers ---


def slugify(name: str) -> str:
	"""Create URL friendly string ("The Slab" -> "the-slab")"""
	s = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-")
	return s or "section"


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


def build_leaderboard(category=None):
	"""
	Build leaderboard rows, optionally filtered by gender category.

	Now cached in memory for LEADERBOARD_CACHE_TTL seconds so we
	don't recompute on every request under load.
	"""
	# --- cache lookup ---
	key = _normalise_category_key(category)
	now = time.time()
	cached = LEADERBOARD_CACHE.get(key)
	if cached:
		rows, category_label, ts = cached
		if now - ts <= LEADERBOARD_CACHE_TTL:
			return rows, category_label

	current_comp = get_current_comp()

	# --- base query, scoped to current competition ---
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

	comps = q.all()
	if not comps:
		rows = []
		LEADERBOARD_CACHE[key] = (rows, category_label, now)
		return rows, category_label

	comp_ids = [c.id for c in comps]
	if comp_ids:
		all_scores = Score.query.filter(Score.competitor_id.in_(comp_ids)).all()
	else:
		all_scores = []

	by_comp = {}
	for s in all_scores:
		by_comp.setdefault(s.competitor_id, []).append(s)

	rows = []
	for c in comps:
		scores = by_comp.get(c.id, [])
		tops = sum(1 for s in scores if s.topped)
		attempts_on_tops = sum(s.attempts for s in scores if s.topped)
		total_points = sum(
			points_for(s.climb_number, s.attempts, s.topped) for s in scores
		)
		last_update = None
		if scores:
			last_update = max(s.updated_at for s in scores if s.updated_at is not None)

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
	rows.sort(
		key=lambda r: (-r["total_points"], -r["tops"], r["attempts_on_tops"])
	)

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
	LEADERBOARD_CACHE[key] = (rows, category_label, now)

	return rows, category_label


def competitor_total_points(comp_id: int) -> int:
	scores = Score.query.filter_by(competitor_id=comp_id).all()
	return sum(points_for(s.climb_number, s.attempts, s.topped) for s in scores)


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
	Landing page:
	- New competitor → /join
	- Returning competitor → /login (email + 6-digit code)
	"""
	return render_template("index.html")

@app.route("/competitions")
def competitions_index():
    """
    Simple list of all competitions.
    For now it's read-only; later we'll wire this into per-comp flows.
    """
    comps = (
        Competition.query
        .order_by(Competition.start_at.asc().nullsfirst())  # if start_at mostly filled
        .all()
    )

    # If your SQLAlchemy / SQLite combo complains about nullsfirst(),
    # just fall back to created_at:
    # comps = Competition.query.order_by(Competition.created_at.desc()).all()

    return render_template("competitions.html", competitions=comps)



@app.route("/resume")
def resume_competitor():
	"""
	Resume scoring for the last competitor on this device.
	Use this as the target for the 'Return to my scoring' QR.
	"""
	cid = session.get("competitor_id")
	if not cid:
		# No remembered competitor on this device
		return redirect("/")

	comp = Competitor.query.get(cid)
	if not comp:
		# Competitor was deleted; clear and go home
		session.pop("competitor_id", None)
		return redirect("/")

	# If this competitor is tied to a competition, send them to the slugged URL
	if comp.competition_id:
		comp_row = Competition.query.get(comp.competition_id)
		if comp_row and comp_row.slug:
			return redirect(f"/comp/{comp_row.slug}/competitor/{cid}/sections")

	# Fallback: old-style URL
	return redirect(f"/competitor/{cid}/sections")



# --- Email login: request code ---


@app.route("/login", methods=["GET", "POST"])
def login_request():
	"""
	Step 1: user enters their email, we generate a 6-digit code and send it.
	"""
	error = None
	message = None
	email = ""

	if request.method == "POST":
		email = (request.form.get("email") or "").strip().lower()
		if not email:
			error = "Please enter your email."
		else:
			comp = Competitor.query.filter_by(email=email).first()
			if not comp:
				error = "We couldn't find that email. If you're new, please register first."
			else:
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

				# Store email in session just for convenience between forms
				session["login_email"] = email

				# IMPORTANT: redirect to the verify route, so form posts to /login/verify
				return redirect("/login/verify")

	return render_template(
		"login_request.html",
		email=email,
		error=error,
		message=message,
	)


# --- Email login: verify code ---


@app.route("/login/verify", methods=["GET", "POST"])
def login_verify():
	"""
	Step 2: user enters the 6-digit code they received.
	"""
	error = None
	message = None

	# Pre-fill email from session if available
	email = (session.get("login_email") or "").strip().lower()

	if request.method == "POST":
		email = (request.form.get("email") or "").strip().lower()
		code = (request.form.get("code") or "").strip()

		if not email or not code:
			error = "Please enter both your email and the code."
		else:
			comp = Competitor.query.filter_by(email=email).first()
			if not comp:
				error = "We couldn't find that email. Please check or register first."
			else:
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
					# Mark code as used and log in the user
					login_code.used = True
					db.session.commit()

					session["competitor_id"] = comp.id

					# NEW: if this email is in ADMIN_EMAILS, grant admin access
					if is_admin_email(email):
						session["admin_ok"] = True
						print(f"[ADMIN LOGIN] {email} is an admin; admin_ok set in session", file=sys.stderr)

					# Clear transient login email
					session.pop("login_email", None)

					return redirect(f"/competitor/{comp.id}/sections")
	else:
		# GET: if we already have an email (i.e. just sent a code), show a helpful message
		if email and not message:
			message = "We've emailed you a 6-digit code. Enter it below to log back into your scoring."

	return render_template(
		"login_verify.html",
		email=email,
		error=error,
		message=message,
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

	# 🔹 Try to infer this competitor's competition + slug (if any)
	comp_row = None
	comp_slug = None
	if comp.competition_id:
		comp_row = Competition.query.get(comp.competition_id)
		if comp_row and comp_row.slug:
			comp_slug = comp_row.slug

	# Legacy behaviour: still show all sections (or you can scope by comp_row if you want)
	sections = Section.query.order_by(Section.name).all()
	total_points = competitor_total_points(target_id)

	# Leaderboard position
	rows, _ = build_leaderboard(None)
	position = None
	for r in rows:
		if r["competitor_id"] == target_id:
			position = r["position"]
			break

	# Whether this viewer can edit attempts
	can_edit = (viewer_id == target_id or is_admin)

	# All climbs with coordinates → feed dots to map
	map_climbs = (
		SectionClimb.query
		.filter(
			SectionClimb.x_percent.isnot(None),
			SectionClimb.y_percent.isnot(None),
		)
		.order_by(SectionClimb.climb_number)
		.all()
	)

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
	)


@app.route("/comp/<slug>/competitor/<int:competitor_id>/sections")
def comp_competitor_sections(slug, competitor_id):
	"""
	Sections index page, scoped to a specific competition slug.

	Non-admins are *forced* to their own competitor id from the session.
	Changing the id in the URL does not let you see or edit someone else's sections.
	"""
	current_comp = get_comp_or_404(slug)

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

	comp = Competitor.query.get_or_404(target_id)

	# Make sure this competitor actually belongs to this competition
	if comp.competition_id != current_comp.id:
		abort(404)

	sections = (
		Section.query
		.filter(Section.competition_id == current_comp.id)
		.order_by(Section.name)
		.all()
	)
	total_points = competitor_total_points(target_id)

	# Leaderboard position (still using current-comp-scoped leaderboard)
	rows, _ = build_leaderboard(None)
	position = None
	for r in rows:
		if r["competitor_id"] == target_id:
			position = r["position"]
			break

	# Whether this viewer can edit attempts
	can_edit = (viewer_id == target_id or is_admin)

	# All climbs with coordinates → feed dots to map, scoped to this comp's sections
	if sections:
		section_ids = [s.id for s in sections]
		map_climbs = (
			SectionClimb.query
			.filter(
				SectionClimb.section_id.in_(section_ids),
				SectionClimb.x_percent.isnot(None),
				SectionClimb.y_percent.isnot(None),
			)
			.order_by(SectionClimb.climb_number)
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
	)


# --- Competitor stats page: My Stats + Overall Stats ---

@app.route("/comp/<slug>/competitor/<int:competitor_id>/stats")
@app.route("/comp/<slug>/competitor/<int:competitor_id>/stats/<string:mode>")
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

	total_points = competitor_total_points(competitor_id)

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

	# Leaderboard position (still using shared helper for now)
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
			# Personal
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
					# Top band is "harder" below
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

	# --- Fallback: your original single-comp logic (unchanged) ---

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

	- Optional competitor context via ?cid=123
	- Optional mode via ?mode=personal|global

	  mode=personal -> emphasise this competitor's performance
	  mode=global   -> emphasise global difficulty (default)
	"""

	current_comp = get_current_comp()

	# --- Mode selection: personal vs global context ---
	mode = (request.args.get("mode", "global") or "global").strip().lower()
	if mode not in ("personal", "global"):
		mode = "global"

	# Did we come here from the Climber Stats page?
	from_climber = (request.args.get("from_climber", "0") == "1")

	# --- Optional competitor context via ?cid= ---
	cid_raw = request.args.get("cid", "").strip()
	competitor = None
	total_points = None
	position = None

	if cid_raw.isdigit():
		competitor = Competitor.query.get(int(cid_raw))
		if competitor:
			# total points for this competitor
			total_points = competitor_total_points(competitor.id)

			# leaderboard position for this competitor
			rows, _ = build_leaderboard(None)
			for r in rows:
				if r["competitor_id"] == competitor.id:
					position = r["position"]
					break

	# All section mappings for this climb, scoped to current competition's sections
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
			SectionClimb.section_id.in_(section_ids_for_comp),
		)
		.all()
	)

	if not section_climbs:
		# If the climb isn't configured at all (for this comp), show not-configured message
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

	# All scores for this climb, but only for competitors in this competition
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

	# --- Global difficulty band for this climb (matches global heatmap) ---
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

	# Per-competitor breakdown
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
				"points": points_for(s.climb_number, s.attempts, s.topped),
				"updated_at": s.updated_at,
			}
		)

	# Sort per-competitor list: topped first, then by attempts asc
	per_competitor.sort(key=lambda r: (not r["topped"], r["attempts"]))

	# --- find this competitor's row (if any) so Jinja doesn't have to ---
	personal_row = None
	if competitor:
		for row in per_competitor:
			if row["competitor_id"] == competitor.id:
				personal_row = row
				break

	# nav_active: respect Climber Stats when we came from there
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
	rows, _ = build_leaderboard(None)
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

	# TEMP: if this comes back empty, show ALL climbs (with coords) for this comp
	if not section_climbs:
		print("No climbs matched this section – DEBUG: falling back to ALL climbs for this comp")
		section_ids = [s.id for s in all_sections]
		if section_ids:
			section_climbs = (
				SectionClimb.query
				.filter(
					SectionClimb.section_id.in_(section_ids),
					SectionClimb.x_percent.isnot(None),
					SectionClimb.y_percent.isnot(None),
				)
				.order_by(SectionClimb.climb_number)
				.all()
			)
		else:
			section_climbs = []

	# ------------- CLIMB NUMBERS / COLOURS / POINTS -------------------
	climbs = [sc.climb_number for sc in section_climbs]

	colours = {
		sc.climb_number: sc.colour
		for sc in section_climbs
		if sc.colour
	}

	# Max points per climb (base_points)
	max_points = {
		sc.climb_number: sc.base_points
		for sc in section_climbs
		if sc.base_points is not None
	}

	scores = Score.query.filter_by(competitor_id=target_id).all()
	existing = {s.climb_number: s for s in scores}

	# pre-compute per-climb points for this competitor
	per_climb_points = {
		s.climb_number: points_for(s.climb_number, s.attempts, s.topped)
		for s in scores
	}

	total_points = competitor_total_points(target_id)

	can_edit = True  # if you got here, you're either that competitor or admin

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

	total_points = competitor_total_points(target_id)

	can_edit = True  # if you got here, you're either that competitor or admin

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
			competition_id=current_comp.id,
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
	"""
	comp = get_comp_or_404(slug)

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

		# Check uniqueness of email (global for now; you *could* scope per comp later)
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

		# Create competitor tied to THIS competition
		new_competitor = Competitor(
			name=name,
			gender=gender,
			email=email,
			competition_id=comp.id,
		)
		db.session.add(new_competitor)
		db.session.commit()
		invalidate_leaderboard_cache()

		# Remember this competitor on this device
		session["competitor_id"] = new_competitor.id

		return redirect(f"/comp/{slug}/competitor/{new_competitor.id}/sections")

	# GET: show blank form
	return render_template(
		"register_public.html",
		error=None,
		name="",
		gender="Inclusive",
		email="",
	)


@app.route("/join", methods=["GET", "POST"])
@app.route("/join/", methods=["GET", "POST"])
def public_register():
	"""
	Self-service registration for competitors.
	- This is what the QR code at the desk should point to.
	- No admin password, just name + category + email.
	- After registration, redirect straight to their sections page.
	"""
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

		current_comp = get_current_comp()

		# Create competitor
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

		# Straight to their sections page to start logging
		return redirect(f"/competitor/{comp.id}/sections")

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

	# Ensure this climb exists in the DB config
	sc = SectionClimb.query.filter_by(climb_number=climb_number).first()
	if not sc:
		return "Unknown climb number", 400

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

	points = points_for(climb_number, attempts, topped)

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
	# Optional competitor context via ?cid=123 (used mainly for back-links)
	cid_raw = request.args.get("cid", "").strip()
	competitor = None
	if cid_raw.isdigit():
		competitor = Competitor.query.get(int(cid_raw))

	rows, category_label = build_leaderboard(None)
	current_competitor_id = session.get("competitor_id")

	return render_template(
		"leaderboard.html",
		leaderboard=rows,
		category=category_label,
		competitor=competitor,
		current_competitor_id=current_competitor_id,
		nav_active="leaderboard",
	)


@app.route("/leaderboard/<category>")
def leaderboard_by_category(category):
	# Optional competitor context via ?cid=123 (used mainly for back-links)
	cid_raw = request.args.get("cid", "").strip()
	competitor = None
	if cid_raw.isdigit():
		competitor = Competitor.query.get(int(cid_raw))

	rows, category_label = build_leaderboard(category)
	current_competitor_id = session.get("competitor_id")

	return render_template(
		"leaderboard.html",
		leaderboard=rows,
		category=category_label,
		competitor=competitor,
		current_competitor_id=current_competitor_id,
		nav_active="leaderboard",
	)


@app.route("/api/leaderboard")
def api_leaderboard():
	category = request.args.get("category")
	rows, category_label = build_leaderboard(category)
	# convert datetime to isoformat for JSON
	for r in rows:
		if r["last_update"] is not None:
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

	if request.method == "POST":
		action = request.form.get("action")

		# Handle login separately
		if action == "login":
			password = request.form.get("password", "")
			if password != ADMIN_PASSWORD:
				error = "Incorrect admin password."
			else:
				session["admin_ok"] = True
				is_admin = True
				message = "Admin access granted."
		else:
			# For all other actions, require that admin has been unlocked
			if not is_admin:
				error = "Please enter the admin password first."
			else:
				if action == "reset_all":
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
							competition_id=current_comp.id,
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

						# start_climb / end_climb are not used to define climbs anymore;
						# they can stay as 0 or be used later for metadata if you want.
						s = Section(
							name=name,
							slug=slug,
							start_climb=0,
							end_climb=0,
							competition_id=current_comp.id,
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

	current_comp = None
	try:
		current_comp = get_current_comp()
	except Exception:
		current_comp = None

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
	)


@app.route("/admin/section/<int:section_id>/edit", methods=["GET", "POST"])
def edit_section(section_id):
	# Require an unlocked admin session
	if not session.get("admin_ok"):
		return redirect("/admin")

	section = Section.query.get_or_404(section_id)
	current_comp = get_current_comp()
	if section.competition_id is not None and section.competition_id != current_comp.id:
		abort(404)

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
				error = "Please enter base points, penalty per attempt, and attempt cap for this climb."
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
					existing = SectionClimb.query.filter_by(
						section_id=section.id,
						climb_number=climb_number
					).first()
					if existing:
						error = f"Climb {climb_number} is already in this section."
					else:
						sc = SectionClimb(
							section_id=section.id,
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
					# Delete all scores for this climb (for all competitors)
					Score.query.filter_by(climb_number=sc.climb_number).delete()

					# Then delete the climb config itself
					db.session.delete(sc)
					db.session.commit()
					invalidate_leaderboard_cache()
					message = f"Climb {sc.climb_number} removed from {section.name}, and all associated scores were deleted."

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

			return redirect("/admin")

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
	)


@app.route("/admin/map")
def admin_map():
	"""
	Map-based climb creation/edit view.
	Admin can click the gym map, then fill climb config and save.
	"""
	if not session.get("admin_ok"):
		return redirect("/admin")

	current_comp = get_current_comp()

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
			.filter(SectionClimb.section_id.in_(section_ids))
			.all()
		)
	else:
		climbs = []

	return render_template(
		"admin_map.html",
		sections=sections,
		climbs=climbs
  )
	

@app.route("/admin/map/add-climb", methods=["POST"])
def admin_map_add_climb():
	"""
	Handle form submission from the map when admin clicks and adds a climb.
	"""
	if not session.get("admin_ok"):
		return redirect("/admin")

	current_comp = get_current_comp()

	section_id_raw = request.form.get("section_id", "").strip()
	new_section_name = (request.form.get("new_section_name") or "").strip()
	climb_raw = request.form.get("climb_number", "").strip()
	colour = (request.form.get("colour") or "").strip()

	base_raw = request.form.get("base_points", "").strip()
	penalty_raw = request.form.get("penalty_per_attempt", "").strip()
	cap_raw = request.form.get("attempt_cap", "").strip()

	x_raw = request.form.get("x_percent", "").strip()
	y_raw = request.form.get("y_percent", "").strip()

	error = None

	# 1) Decide which section to use (existing or new)
	section = None
	if section_id_raw and section_id_raw.isdigit():
		section = Section.query.get(int(section_id_raw))
		if section and section.competition_id != current_comp.id:
			section = None

	if not section and new_section_name:
		slug = slugify(new_section_name)
		existing = Section.query.filter_by(slug=slug).first()
		if existing:
			slug = f"{slug}-{int(datetime.utcnow().timestamp())}"

		section = Section(
			name=new_section_name,
			slug=slug,
			start_climb=0,
			end_climb=0,
			competition_id=current_comp.id,
		)
		db.session.add(section)
		db.session.flush()  # get section.id without full commit

	if not section:
		error = "Please choose an existing section or enter a new section name."

	# 2) Basic validation of numbers
	if not error:
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

	if not error:
		climb_number = int(climb_raw)
		base_points = int(base_raw)
		penalty_per_attempt = int(penalty_raw)
		attempt_cap = int(cap_raw)

		if climb_number <= 0:
			error = "Climb number must be positive."
		elif base_points < 0 or penalty_per_attempt < 0 or attempt_cap <= 0:
			error = "Base points, penalty must be ≥ 0 and attempt cap > 0."

	# 3) Coordinates
	if not error:
		try:
			x_percent = float(x_raw)
			y_percent = float(y_raw)
		except ValueError:
			error = "Internal error: invalid click coordinates. Please try again."

	if error:
		# Simple redirect for now (could add flash messaging)
		return redirect("/admin/map")

	# 4) Check climb uniqueness within section
	existing = SectionClimb.query.filter_by(
		section_id=section.id,
		climb_number=climb_number
	).first()
	if existing:
		# Just redirect; in real life you'd show a targeted error
		return redirect("/admin/map")

	sc = SectionClimb(
		section_id=section.id,
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

	return redirect("/admin/map")


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

