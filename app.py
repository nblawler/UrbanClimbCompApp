from flask import Flask, render_template, request, redirect, jsonify
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint
from datetime import datetime
from typing import Optional
from ClimbScores_config import CLIMB_SCORES
import os
import sys
import re

app = Flask(__name__)

# --- Database setup ---
DB_URL = os.getenv("DATABASE_URL", "sqlite:///scoring.db")
app.config["SQLALCHEMY_DATABASE_URI"] = DB_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# Number of climbs = how many entries in the config
NUM_CLIMBS = len(CLIMB_SCORES)


# --- Models ---


class Competitor(db.Model):
	# competitor number (auto-incremented)
	id = db.Column(db.Integer, primary_key=True)
	name = db.Column(db.String(120), nullable=False)
	gender = db.Column(db.String(20), nullable=False, default="Inclusive")
	created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Score(db.Model):
	__tablename__ = "scores"

	id = db.Column(db.Integer, primary_key=True)
	competitor_id = db.Column(db.Integer, db.ForeignKey("competitor.id"), nullable=False)
	climb_number = db.Column(db.Integer, nullable=False)
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


class SectionClimb(db.Model):
	id = db.Column(db.Integer, primary_key=True)
	section_id = db.Column(db.Integer, db.ForeignKey("section.id"), nullable=False)
	climb_number = db.Column(db.Integer, nullable=False)
	colour = db.Column(db.String(80), nullable=True)

	section = db.relationship("Section", backref=db.backref("climbs", lazy=True))

	__table_args__ = (
		UniqueConstraint("section_id", "climb_number", name="uq_section_climb"),
	)


# --- Scoring function ---


def points_for(climb_number, attempts, topped):
	"""
	Points apply only if topped.

	Full points on first attempt.
	Penalty starts from attempt #2, capped at 5 attempts for penalty purposes.

	Example (base=100, penalty=10):
	 - attempts=1 -> 100
	 - attempts=2 -> 90
	 - attempts=3 -> 80
	 - attempts>=6 -> same as attempts=5 (cap)
	"""
	cfg = CLIMB_SCORES.get(climb_number)
	if not cfg or not topped:
		return 0

	base = cfg["base"]
	penalty = cfg["penalty"]

	# sanity-clamp attempts
	if attempts < 1:
		attempts = 1
	elif attempts > 50:
		attempts = 50

	# only attempts from 2 onward incur penalty; cap at 5
	penalty_attempts = max(0, min(attempts, 5) - 1)

	return max(int(base - penalty * penalty_attempts), 0)


# --- Helpers ---


def slugify(name: str) -> str:
	s = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-")
	return s or "section"


def build_leaderboard(category=None):
	"""Build leaderboard rows, optionally filtered by gender category."""
	q = Competitor.query
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
		return [], category_label

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
		key = (row["total_points"], row["tops"], row["attempts_on_tops"])
		if key != prev_key:
			pos += 1
		prev_key = key
		row["position"] = pos

	return rows, category_label


def competitor_total_points(comp_id: int) -> int:
	scores = Score.query.filter_by(competitor_id=comp_id).all()
	return sum(points_for(s.climb_number, s.attempts, s.topped) for s in scores)


# --- Routes ---


@app.route("/")
def index():
	# just render the home page where competitors enter their number
	return render_template("index.html", error=None)


@app.route("/competitor", methods=["POST"])
def enter_competitor():
	cid_raw = request.form.get("competitor_id", "").strip()

	if not cid_raw.isdigit():
		return render_template(
			"index.html", error="Please enter a valid competitor number."
		)

	cid = int(cid_raw)
	comp = Competitor.query.get(cid)
	if not comp:
		return render_template(
			"index.html",
			error="Competitor not found. Please check with the desk.",
		)

	return redirect(f"/competitor/{cid}/sections")


@app.route("/competitor/<int:competitor_id>")
def competitor_redirect(competitor_id):
	# Backwards compatibility: redirect plain competitor URL to sections
	return redirect(f"/competitor/{competitor_id}/sections")


@app.route("/competitor/<int:competitor_id>/sections")
def competitor_sections(competitor_id):
	comp = Competitor.query.get_or_404(competitor_id)
	sections = Section.query.order_by(Section.name).all()
	total_points = competitor_total_points(competitor_id)
	return render_template(
		"competitor_sections.html",
		competitor=comp,
		sections=sections,
		total_points=total_points,
	)


@app.route("/competitor/<int:competitor_id>/section/<section_slug>")
def competitor_section_climbs(competitor_id, section_slug):
	comp = Competitor.query.get_or_404(competitor_id)
	section = Section.query.filter_by(slug=section_slug).first_or_404()

	# Use the climbs explicitly configured for this section
	section_climbs = (
		SectionClimb.query
		.filter_by(section_id=section.id)
		.order_by(SectionClimb.climb_number)
		.all()
	)

	# List of climb numbers for the template
	climbs = [sc.climb_number for sc in section_climbs]

	# Optional colour/grade labels
	colours = {sc.climb_number: sc.colour for sc in section_climbs if sc.colour}

	# Existing scores for this competitor
	scores = Score.query.filter_by(competitor_id=competitor_id).all()
	existing = {s.climb_number: s for s in scores}

	total_points = competitor_total_points(competitor_id)

	return render_template(
		"competitor.html",
		competitor=comp,
		climbs=climbs,
		existing=existing,
		total_points=total_points,
		section=section,
		colours=colours,
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

		comp = Competitor(name=name, gender=gender)
		db.session.add(comp)
		db.session.commit()

		return render_template(
			"register.html", error=None, competitor=comp
		)

	return render_template("register.html", error=None, competitor=None)


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

	if climb_number not in CLIMB_SCORES:
		return "Unknown climb number", 400

	comp = Competitor.query.get(competitor_id)
	if not comp:
		return "Competitor not found", 404

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
	rows, category_label = build_leaderboard(None)
	return render_template(
		"leaderboard.html",
		leaderboard=rows,
		category=category_label,
	)


@app.route("/leaderboard/<category>")
def leaderboard_by_category(category):
	rows, category_label = build_leaderboard(category)
	return render_template(
		"leaderboard.html",
		leaderboard=rows,
		category=category_label,
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

	if request.method == "POST":
		password = request.form.get("password", "")
		if password != ADMIN_PASSWORD:
			error = "Incorrect admin password."
		else:
			action = request.form.get("action")

			if action == "reset_all":
				# Delete scores, section climbs, competitors, sections
				Score.query.delete()
				SectionClimb.query.delete()
				Competitor.query.delete()
				Section.query.delete()
				db.session.commit()
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
						message = f"Competitor {cid} and their scores have been deleted."

			elif action == "create_competitor":
				name = request.form.get("new_name", "").strip()
				gender = request.form.get("new_gender", "Inclusive").strip()

				if not name:
					error = "Competitor name is required."
				else:
					if gender not in ("Male", "Female", "Inclusive"):
						gender = "Inclusive"
					comp = Competitor(name=name, gender=gender)
					db.session.add(comp)
					db.session.commit()
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

  					# start_climb / end_climb are not used to define climbs anymore;
					# they can stay as 0 or be used later for metadata if you want.
					s = Section(
						name=name,
						slug=slug,
						start_climb=0,
						end_climb=0,
					)
					db.session.add(s)
					db.session.commit()
					message = f"Section created: {name}. You can now add climbs via Edit."

	sections = Section.query.order_by(Section.name).all()
	return render_template("admin.html", message=message, error=error, sections=sections)


@app.route("/admin/section/<int:section_id>/edit", methods=["GET", "POST"])
def edit_section(section_id):
	section = Section.query.get_or_404(section_id)
	error = None
	message = None

	if request.method == "POST":
		password = request.form.get("password", "")
		if password != ADMIN_PASSWORD:
			error = "Incorrect admin password."
		else:
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

				if not climb_raw.isdigit():
					error = "Please enter a valid climb number."
				else:
					climb_number = int(climb_raw)
					if climb_number not in CLIMB_SCORES:
						error = "That climb number is not in the scoring config."
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
						db.session.delete(sc)
						db.session.commit()
						message = f"Climb {sc.climb_number} removed from {section.name}."

			elif action == "delete_section":
				SectionClimb.query.filter_by(section_id=section.id).delete()
				db.session.delete(section)
				db.session.commit()
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


# --- Startup ---


with app.app_context():
	db.create_all()

if __name__ == "__main__":
	port = 5001
	if "--port" in sys.argv:
		try:
			idx = sys.argv.index("--port")
			port = int(sys.argv[idx + 1])
		except Exception:
			pass
	app.run(debug=True, port=port)