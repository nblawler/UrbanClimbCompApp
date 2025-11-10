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

NUM_CLIMBS = int(os.getenv("NUM_CLIMBS", 66))	# Default to 10 climbs
ADMIN_PASSWORD = "climbadmin"	# change for real comp


# --- No-cache for API responses ---
@app.after_request
def add_no_store(resp):
	if request.path.startswith("/api/"):
		resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
		resp.headers["Pragma"] = "no-cache"
	return resp


# --- Models ---
class Competitor(db.Model):
	id = db.Column(db.Integer, primary_key=True)
	gender = db.Column(db.String(32), nullable=False, default="Gender Inclusive")
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


# --- Scoring configuration ---
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
 	12: {"base": 100, "penalty": 10},
	12: {"base": 110, "penalty": 10},
	13: {"base": 120, "penalty": 10},
	14: {"base": 130, "penalty": 10},
	15: {"base": 140, "penalty": 10},
	16: {"base": 150, "penalty": 12},
	17: {"base": 160, "penalty": 12},
	18: {"base": 170, "penalty": 12},
	19: {"base": 180, "penalty": 12},
	20: {"base": 200, "penalty": 15},
 	21: {"base": 100, "penalty": 10},
	22: {"base": 110, "penalty": 10},
	23: {"base": 120, "penalty": 10},
	24: {"base": 130, "penalty": 10},
	25: {"base": 140, "penalty": 10},
	26: {"base": 150, "penalty": 12},
	27: {"base": 160, "penalty": 12},
	28: {"base": 170, "penalty": 12},
	29: {"base": 180, "penalty": 12},
	30: {"base": 200, "penalty": 15},
 	31: {"base": 100, "penalty": 10},
	32: {"base": 110, "penalty": 10},
	33: {"base": 120, "penalty": 10},
	34: {"base": 130, "penalty": 10},
	35: {"base": 140, "penalty": 10},
	36: {"base": 150, "penalty": 12},
	37: {"base": 160, "penalty": 12},
	38: {"base": 170, "penalty": 12},
	39: {"base": 180, "penalty": 12},
	40: {"base": 200, "penalty": 15},
 	41: {"base": 100, "penalty": 10},
	42: {"base": 110, "penalty": 10},
	43: {"base": 120, "penalty": 10},
	44: {"base": 130, "penalty": 10},
	45: {"base": 140, "penalty": 10},
	46: {"base": 150, "penalty": 12},
	47: {"base": 160, "penalty": 12},
	48: {"base": 170, "penalty": 12},
	49: {"base": 180, "penalty": 12},
	50: {"base": 200, "penalty": 15},
 	51: {"base": 100, "penalty": 10},
	52: {"base": 110, "penalty": 10},
	53: {"base": 120, "penalty": 10},
	54: {"base": 130, "penalty": 10},
	55: {"base": 140, "penalty": 10},
	56: {"base": 150, "penalty": 12},
	57: {"base": 160, "penalty": 12},
	58: {"base": 170, "penalty": 12},
	59: {"base": 180, "penalty": 12},
	60: {"base": 200, "penalty": 15},
 	61: {"base": 100, "penalty": 10},
	62: {"base": 110, "penalty": 10},
	63: {"base": 120, "penalty": 10},
	64: {"base": 130, "penalty": 10},
	65: {"base": 140, "penalty": 10},
	66: {"base": 150, "penalty": 12},
}


def points_for(climb_number: int, attempts: int, topped: bool) -> int:
	"""
	Points apply only if topped.

	Full points on first attempt.
	Penalty starts from attempt #2, capped at 5 attempts for penalty purposes.

	Example (base=100, penalty=10):
	 - attempts=1 -> 100
	 - attempts=2 -> 90
	 - attempts=3 -> 80
	 - attempts=6 -> also 60 (cap at 5 attempts)
	"""
	cfg = CLIMB_SCORES.get(climb_number)
	if not cfg or not topped:
		return 0

	base = cfg["base"]
	penalty = cfg["penalty"]

	# Cap raw attempts between 1 and 50
	if attempts < 1:
		attempts = 1
	elif attempts > 50:
		attempts = 50

	# Only attempts from 2 onwards incur penalty, capped at 5 total attempts
	# So penalty_attempts = 0 for attempt 1, then 1,2,3,4 for attempts 2..5, then stays at 4
	penalty_attempts = max(0, min(attempts, 5) - 1)

	return max(int(base - penalty * penalty_attempts), 0)



# --- Routes: Home & Competitor entry ---
@app.route("/")
def index():
	return render_template("index.html")


@app.route("/competitor", methods=["POST"])
def enter_competitor():
	raw_id = (request.form.get("competitor_id") or "").strip()
	gender = (request.form.get("gender") or "Gender Inclusive").strip()

	if not raw_id.isdigit() or int(raw_id) <= 0:
		return render_template("index.html", error="Please enter a valid competitor number (positive integer).")

	if gender not in ("Male", "Female", "Gender Inclusive"):
		return render_template("index.html", error="Please select a valid category.")

	cid = int(raw_id)
	comp = Competitor.query.get(cid)
	if comp is None:
		comp = Competitor(id=cid, gender=gender)
		db.session.add(comp)
	else:
		if not comp.gender:
			comp.gender = gender

	db.session.commit()
	return redirect(f"/competitor/{cid}")


@app.route("/competitor/<int:cid>")
def competitor_page(cid):
	comp = Competitor.query.get_or_404(cid)
	existing = {s.climb_number: s for s in comp.scores}
	climbs = list(range(1, NUM_CLIMBS + 1))
	return render_template("competitor.html", competitor=comp, climbs=climbs, existing=existing)


# --- API: upsert and fetch scores ---
@app.route("/api/score", methods=["POST"])
def save_score():
	data = request.get_json(force=True, silent=True) or {}

	try:
		cid = int(data.get("competitor_id"))
		climb = int(data.get("climb_number"))
		attempts = int(data.get("attempts"))
		topped = bool(data.get("topped"))
	except Exception:
		return jsonify({"ok": False, "error": "Invalid payload"}), 400

	# enforce bounds on attempts
	if attempts < 1:
		attempts = 1
	elif attempts > 50:
		attempts = 50

	if climb < 1 or climb > NUM_CLIMBS:
		return jsonify({"ok": False, "error": f"climb_number must be 1..{NUM_CLIMBS}"}), 400

	comp = Competitor.query.get(cid)
	if not comp:
		# created via API without form → default to Gender Inclusive
		comp = Competitor(id=cid, gender="Gender Inclusive")
		db.session.add(comp)
		db.session.flush()

	score = Score.query.filter_by(competitor_id=cid, climb_number=climb).first()
	if not score:
		score = Score(competitor_id=cid, climb_number=climb)
		db.session.add(score)

	score.attempts = attempts
	score.topped = topped
	db.session.commit()

	return jsonify({"ok": True})


@app.route("/api/score/<int:cid>")
def get_scores(cid):
	comp = Competitor.query.get(cid)
	if not comp:
		return jsonify([])

	return jsonify([
		{
			"climb_number": s.climb_number,
			"attempts": s.attempts,
			"topped": s.topped,
			"points": points_for(s.climb_number, s.attempts, s.topped),
		}
		for s in comp.scores
	])


# --- Leaderboard helpers & routes ---
def compute_leaderboard_for_gender(gender_label: str):
	comps = Competitor.query.filter_by(gender=gender_label).all()
	if not comps:
		return []

	comp_ids = [c.id for c in comps]
	all_scores = Score.query.filter(Score.competitor_id.in_(comp_ids)).all()

	by_comp = {}
	for s in all_scores:
		by_comp.setdefault(s.competitor_id, []).append(s)

	rows = []
	for c in comps:
		scores = by_comp.get(c.id, [])
		tops = sum(1 for s in scores if s.topped)
		attempts_on_tops = sum(s.attempts for s in scores if s.topped)
		total_points = sum(points_for(s.climb_number, s.attempts, s.topped) for s in scores)

		rows.append({
			"competitor_id": c.id,
			"tops": tops,
			"attempts_on_tops": attempts_on_tops,
			"total_points": total_points,
		})

	rows.sort(key=lambda r: (-r["total_points"], -r["tops"], r["attempts_on_tops"]))
	pos = 0
	prev_key = None
	for row in rows:
		key = (row["total_points"], row["tops"], row["attempts_on_tops"])
		if key != prev_key:
			pos += 1
		prev_key = key
		row["position"] = pos

	return rows


@app.route("/leaderboard")
def leaderboard_index():
	return render_template("leaderboard_index.html")


@app.route("/leaderboard/<category>")
def leaderboard_category(category):
	slug_map = {
		"male": "Male",
		"female": "Female",
		"inclusive": "Gender Inclusive",
	}
	gender_label = slug_map.get(category.lower())
	if not gender_label:
		return "Unknown category", 404

	rows = compute_leaderboard_for_gender(gender_label)
	return render_template("leaderboard_gender.html", category=gender_label, rows=rows)


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
	print("== DB URI:", app.config["SQLALCHEMY_DATABASE_URI"], "CWD:", os.getcwd())
	port = 5001
	if "--port" in sys.argv:
		try:
			port = int(sys.argv[sys.argv.index("--port") + 1])
		except Exception:
			pass
	app.run(debug=True, port=port)
