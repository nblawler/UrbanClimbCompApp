from flask import Flask, render_template, request, redirect, jsonify
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint
from datetime import datetime
import os
import sys

app = Flask(__name__)

# --- Database setup ---
DB_URL = os.getenv("DATABASE_URL", "sqlite:///scoring.db")
app.config["SQLALCHEMY_DATABASE_URI"] = DB_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

NUM_CLIMBS = int(os.getenv("NUM_CLIMBS", 10))	# Default to 10 climbs

# --- Simple admin password ---
ADMIN_PASSWORD = "climbadmin"	# change this before running the real comp


# --- No-cache for API responses ---
@app.after_request
def add_no_store(resp):
	# prevent browsers/CDNs from caching API JSON
	if request.path.startswith("/api/"):
		resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
		resp.headers["Pragma"] = "no-cache"
	return resp


# --- Models ---
class Competitor(db.Model):
	id = db.Column(db.Integer, primary_key=True)
	created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Score(db.Model):
	__tablename__ = "scores"

	id = db.Column(db.Integer, primary_key=True)
	competitor_id = db.Column(db.Integer, db.ForeignKey("competitor.id"), nullable=False)
	climb_number = db.Column(db.Integer, nullable=False)
	attempts = db.Column(db.Integer, nullable=False, default=1)
	topped = db.Column(db.Boolean, nullable=False, default=False)
	updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

	__table_args__ = (UniqueConstraint("competitor_id", "climb_number", name="uq_competitor_climb"),)

	competitor = db.relationship("Competitor", backref=db.backref("scores", lazy=True))


# --- Per-climb scoring config ---
CLIMB_SCORES = {
	1: {"base": 100, "penalty": 10},
	2: {"base": 110, "penalty": 10},
	3: {"base": 120, "penalty": 10},
	4: {"base": 130, "penalty": 10},
	5: {"base": 140, "penalty": 10},
	6: {"base": 150, "penalty": 12},
	7: {"base": 160, "penalty": 12},
	8: {"base": 170, "penalty": 12},
	9: {"base": 180, "penalty": 12},
	10: {"base": 200, "penalty": 15},
}


def points_for(climb_number: int, attempts: int, topped: bool) -> int:
	"""
	Points apply only if topped:
		points = max(base - penalty * min(attempts, 5), 0)
	"""
	cfg = CLIMB_SCORES.get(climb_number)
	if not cfg or not topped:
		return 0

	max_penalty_attempts = 5	# cap the penalty at 5 attempts
	effective_attempts = min(attempts, max_penalty_attempts)
	return max(int(cfg["base"] - cfg["penalty"] * effective_attempts), 0)


# --- Routes ---
@app.route("/")
def index():
	return render_template("index.html")


@app.route("/competitor", methods=["POST"])
def enter_competitor():
	raw = (request.form.get("competitor_id") or "").strip()
	if not raw.isdigit() or int(raw) <= 0:
		return render_template("index.html", error="Please enter a valid competitor number (positive integer).")

	cid = int(raw)
	comp = Competitor.query.get(cid)
	if comp is None:
		comp = Competitor(id=cid)
		db.session.add(comp)
		db.session.commit()

	return redirect(f"/competitor/{cid}")


@app.route("/competitor/<int:cid>")
def competitor_page(cid):
	competitor = Competitor.query.get_or_404(cid)
	existing = {s.climb_number: s for s in Score.query.filter_by(competitor_id=cid).all()}
	climbs = list(range(1, NUM_CLIMBS + 1))
	total_points = sum(points_for(s.climb_number, s.attempts, s.topped) for s in existing.values())
	return render_template("competitor.html", competitor=competitor, climbs=climbs, existing=existing, total_points=total_points)


@app.route("/api/score", methods=["POST"])
def upsert_score():
	data = request.get_json(force=True, silent=True) or {}
	try:
		competitor_id = int(data.get("competitor_id"))
		climb_number = int(data.get("climb_number"))
		attempts = int(data.get("attempts"))
		topped = bool(data.get("topped"))
	except Exception:
		return jsonify({"ok": False, "error": "Invalid payload"}), 400

	# Enforce bounds: attempts must be 1..50
	if attempts < 1:
		attempts = 1
	elif attempts > 50:
		attempts = 50

	if not (1 <= climb_number <= NUM_CLIMBS):
		return jsonify({"ok": False, "error": f"climb_number must be 1..{NUM_CLIMBS}"}), 400

	comp = Competitor.query.get(competitor_id)
	if comp is None:
		comp = Competitor(id=competitor_id)
		db.session.add(comp)
		db.session.flush()

	s = Score.query.filter_by(competitor_id=competitor_id, climb_number=climb_number).one_or_none()
	if s is None:
		s = Score(competitor_id=competitor_id, climb_number=climb_number, attempts=attempts, topped=topped)
		db.session.add(s)
	else:
		s.attempts = attempts
		s.topped = topped

	db.session.commit()

	return jsonify({
		"ok": True,
		"score": {
			"competitor_id": s.competitor_id,
			"climb_number": s.climb_number,
			"attempts": s.attempts,
			"topped": s.topped,
			"updated_at": s.updated_at.isoformat(),
		}
	})


@app.route("/api/score/<int:cid>")
def get_scores(cid):
	rows = Score.query.filter_by(competitor_id=cid).order_by(Score.climb_number).all()
	return jsonify([
		{"climb_number": r.climb_number, "attempts": r.attempts, "topped": r.topped}
		for r in rows
	])


@app.route("/api/competitor/<int:cid>/totals")
def competitor_totals(cid):
	rows = Score.query.filter_by(competitor_id=cid).all()
	tops = sum(1 for r in rows if r.topped)
	attempts_on_tops = sum(r.attempts for r in rows if r.topped)
	total_points = sum(points_for(r.climb_number, r.attempts, r.topped) for r in rows)
	return jsonify({
		"competitor_id": cid,
		"tops": tops,
		"attempts_on_tops": attempts_on_tops,
		"total_points": total_points
	})


@app.route("/leaderboard")
def leaderboard_page():
	# Pull all competitors and their scores once
	comps = Competitor.query.all()
	all_scores = Score.query.all()

	# Index scores by competitor
	by_comp = {}
	for s in all_scores:
		by_comp.setdefault(s.competitor_id, []).append(s)

	leaderboard = []
	for c in comps:
		scores = by_comp.get(c.id, [])
		tops = sum(1 for s in scores if s.topped)
		attempts_on_tops = sum(s.attempts for s in scores if s.topped)
		total_points = sum(points_for(s.climb_number, s.attempts, s.topped) for s in scores)
		last_update = max((s.updated_at for s in scores), default=None)

		leaderboard.append({
			"competitor_id": c.id,
			"tops": tops,
			"attempts_on_tops": attempts_on_tops,
			"total_points": total_points,
			"last_update": last_update.isoformat() if last_update else "",
		})

	# Sort primarily by points desc, then tops desc, then attempts asc
	leaderboard.sort(key=lambda r: (-r["total_points"], -r["tops"], r["attempts_on_tops"]))

	# Assign positions with stable ties
	pos = 0
	prev_key = None
	for row in leaderboard:
		key = (row["total_points"], row["tops"], row["attempts_on_tops"])
		if key != prev_key:
			pos = pos + 1
		prev_key = key
		row["position"] = pos

	return render_template("leaderboard.html", leaderboard=leaderboard)


@app.route("/api/leaderboard")
def leaderboard_api():
	comps = Competitor.query.all()
	all_scores = Score.query.all()

	by_comp = {}
	for s in all_scores:
		by_comp.setdefault(s.competitor_id, []).append(s)

	out = []
	for c in comps:
		scores = by_comp.get(c.id, [])
		tops = sum(1 for s in scores if s.topped)
		attempts_on_tops = sum(s.attempts for s in scores if s.topped)
		total_points = sum(points_for(s.climb_number, s.attempts, s.topped) for s in scores)
		last_update = max((s.updated_at for s in scores), default=None)

		out.append({
			"competitor_id": c.id,
			"tops": tops,
			"attempts_on_tops": attempts_on_tops,
			"total_points": total_points,
			"last_update": last_update.isoformat() if last_update else None,
		})

	out.sort(key=lambda r: (-r["total_points"], -r["tops"], r["attempts_on_tops"]))

	pos = 0
	prev_key = None
	for row in out:
		key = (row["total_points"], row["tops"], row["attempts_on_tops"])
		if key != prev_key:
			pos = pos + 1
		prev_key = key
		row["position"] = pos

	return jsonify(out)


# --- Admin panel ---
@app.route("/admin", methods=["GET", "POST"])
def admin_panel():
	message = None
	error = None

	if request.method == "POST":
		password = (request.form.get("password") or "").strip()
		if password != ADMIN_PASSWORD:
			error = "Incorrect password."
		else:
			action = request.form.get("action")
			if action == "delete_one":
				raw_cid = (request.form.get("competitor_id") or "").strip()
				if not raw_cid.isdigit():
					error = "Please enter a valid competitor number."
				else:
					cid = int(raw_cid)
					# delete scores first (FK)
					deleted_scores = Score.query.filter_by(competitor_id=cid).delete()
					comp = Competitor.query.get(cid)
					if comp:
						db.session.delete(comp)
						db.session.commit()
						message = f"Deleted competitor #{cid} and {deleted_scores} scores."
					else:
						db.session.commit()
						message = f"No competitor #{cid} found. {deleted_scores} scores (if any) removed."
			elif action == "reset_all":
				Score.query.delete()
				Competitor.query.delete()
				db.session.commit()
				message = "All competitors and scores have been removed."
			else:
				error = "Unknown action."

	return render_template("admin.html", message=message, error=error)


# --- Startup ---
with app.app_context():
	db.create_all()

if __name__ == "__main__":
	# Helpful debug: show which DB file and working directory are in use
	print("== DB URI:", app.config["SQLALCHEMY_DATABASE_URI"], "CWD:", os.getcwd())

	port = 5001
	if "--port" in sys.argv:
		try:
			idx = sys.argv.index("--port")
			port = int(sys.argv[idx + 1])
		except Exception:
			pass
	app.run(debug=True, port=port)