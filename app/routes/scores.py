from flask import (
    Blueprint,
    request,
    session,
    jsonify,
    redirect,
    current_app,
    render_template,
    flash,
    make_response,
    abort,
)
import csv
import io
import time
from collections import defaultdict

from app.extensions import db
from app.models import Competition, Competitor, Score, Section, SectionClimb
from app.helpers.admin import admin_can_manage_competition
from app.helpers.competition import comp_is_finished, get_viewer_comp, comp_is_live
from app.helpers.leaderboard import (
    build_leaderboard,
    normalize_leaderboard_category,
    get_top_climbs_for_competitor,
)
from app.helpers.scoring import points_for
from app.helpers.leaderboard_cache import invalidate_leaderboard_cache

scores_bp = Blueprint("scores", __name__)

DEFAULT_LB_PER_PAGE = 10


def build_final_results_csv_rows(comp):
    """
    Fast final-results export that matches the intended singles scoring rule:

    - topped climbs only
    - fixed base_points from section_climb
    - no attempt penalty in score
    - top 8 topped climbs summed
    - tie-break by lowest attempts on those top 8
    - stable final tie-break by name asc

    Exports one row per competitor:
      category
      position
      total_competitors
      competitor_name
      competitor_email
      score
    """
    TOP_N = 8

    competitors = (
        db.session.query(
            Competitor.id,
            Competitor.name,
            Competitor.email,
        )
        .filter(Competitor.competition_id == comp.id)
        .all()
    )

    if not competitors:
        return []

    competitor_ids = [c.id for c in competitors]

    # Pull exact scoring config per saved score row
    score_rows = (
        db.session.query(
            Score.competitor_id,
            Score.climb_number,
            Score.attempts,
            Score.topped,
            Score.updated_at,
            SectionClimb.base_points,
        )
        .join(SectionClimb, SectionClimb.id == Score.section_climb_id)
        .join(Section, Section.id == SectionClimb.section_id)
        .filter(
            Score.competitor_id.in_(competitor_ids),
            Section.competition_id == comp.id,
        )
        .all()
    )

    by_competitor = defaultdict(list)
    for s in score_rows:
        by_competitor[s.competitor_id].append(s)

    rows = []
    for c in competitors:
        scores = by_competitor.get(c.id, [])

        topped_scored = []

        for s in scores:
            if not bool(s.topped):
                continue

            pts = int(s.base_points or 0)

            topped_scored.append({
                "climb_number": s.climb_number,
                "points": pts,
                "attempts": int(s.attempts or 0),
            })

        # Sort exactly like intended rule
        topped_scored.sort(
            key=lambda x: (-x["points"], x["attempts"], x.get("climb_number") or 0)
        )

        topN = topped_scored[:TOP_N]

        total_points = sum(x["points"] for x in topN)
        attempts_on_tops = sum(x["attempts"] for x in topN)

        rows.append({
            "competitor_id": c.id,
            "competitor_name": c.name or "",
            "competitor_email": c.email or "",
            "score": total_points,
            "attempts_on_tops": attempts_on_tops,
        })

    # Rank: score desc, attempts asc, name asc
    rows.sort(
        key=lambda r: (
            -int(r["score"]),
            int(r["attempts_on_tops"]),
            (r["competitor_name"] or "").lower(),
        )
    )

    total_competitors = len(rows)

    output_rows = []
    pos = 0
    prev_key = None

    for row in rows:
        k = (row["score"], row["attempts_on_tops"])
        if k != prev_key:
            pos += 1
        prev_key = k

        output_rows.append({
            "category": "All",
            "position": pos,
            "total_competitors": total_competitors,
            "competitor_name": row["competitor_name"],
            "competitor_email": row["competitor_email"],
            "score": row["score"],
        })

    return output_rows


@scores_bp.route("/my-scoring")
def my_scoring_redirect():
    viewer_id = session.get("competitor_id")
    if not viewer_id:
        return redirect("/")

    competitor = Competitor.query.get(viewer_id)
    if not competitor:
        session.pop("competitor_id", None)
        return redirect("/")

    if competitor.competition_id:
        comp = Competition.query.get(competitor.competition_id)
        if comp and comp.slug:
            session["active_comp_slug"] = comp.slug
            return redirect(f"/comp/{comp.slug}/competitor/{competitor.id}/sections")

    slug = (session.get("active_comp_slug") or "").strip()
    if slug:
        return redirect(f"/comp/{slug}/join")

    return redirect("/my-comps")


@scores_bp.route("/api/score", methods=["POST"])
def api_save_score():
    data = request.get_json(force=True, silent=True) or {}

    try:
        competitor_id = int(data.get("competitor_id", 0))
    except (TypeError, ValueError):
        return "Invalid competitor_id", 400

    if competitor_id <= 0:
        return "Invalid competitor_id", 400

    try:
        attempts = int(data.get("attempts", 1))
    except (TypeError, ValueError):
        attempts = 1

    topped = bool(data.get("topped", False))

    flashed_in = data.get("flashed", None)
    flashed = bool(flashed_in) if flashed_in is not None else False

    if attempts < 1:
        attempts = 1
    elif attempts > 50:
        attempts = 50

    if flashed:
        topped = True
        attempts = 1

    if not flashed and topped and attempts == 1:
        flashed = True

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

    if section_climb_id is None and (climb_number is None or climb_number <= 0):
        return "Missing section_climb_id or climb_number", 400

    viewer_id = session.get("competitor_id")
    is_admin = session.get("admin_ok", False)

    if (
        not viewer_id
        and not is_admin
        and current_app.debug
        and request.remote_addr in ("127.0.0.1", "::1")
    ):
        is_admin = True

    if viewer_id != competitor_id and not is_admin:
        return "Not allowed", 403

    comp_row = Competitor.query.get(competitor_id)
    if not comp_row:
        return "Competitor not found", 404

    if not comp_row.competition_id:
        return "Competitor not registered for a competition", 400

    current_comp = Competition.query.get(comp_row.competition_id)
    if not current_comp:
        return "Competition not found", 404

    if comp_is_finished(current_comp):
        return "Competition finished — scoring locked", 403

    sc = None

    if section_climb_id is not None:
        sc = SectionClimb.query.get(section_climb_id)
        if not sc:
            return "Unknown section_climb_id", 400

        sec = Section.query.get(sc.section_id) if sc.section_id else None
        if not sec or sec.competition_id != current_comp.id:
            return "section_climb_id not in this competition", 400

    else:
        matches = (
            SectionClimb.query.join(Section, Section.id == SectionClimb.section_id)
            .filter(
                SectionClimb.climb_number == climb_number,
                Section.competition_id == current_comp.id,
            )
            .all()
        )

        if not matches:
            return "Unknown climb number for this competition", 400

        if len(matches) > 1:
            return (
                "Ambiguous climb_number in this competition. "
                "Send section_climb_id instead.",
                400,
            )

        sc = matches[0]

    score = (
        Score.query.filter_by(competitor_id=competitor_id, section_climb_id=sc.id)
        .first()
    )

    if not score:
        score = Score(
            competitor_id=competitor_id,
            section_climb_id=sc.id,
            climb_number=sc.climb_number,
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


@scores_bp.route("/api/score/<int:competitor_id>")
def api_get_scores(competitor_id):
    competitor = Competitor.query.get_or_404(competitor_id)

    scores = (
        Score.query.filter_by(competitor_id=competitor_id)
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


@scores_bp.route("/api/leaderboard/details")
def leaderboard_details_api():
    comp = get_viewer_comp()

    if not comp:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "Pick a competition first to view the leaderboard.",
                    "climbs": [],
                }
            ),
            200,
        )

    is_admin = admin_can_manage_competition(comp)

    if not comp_is_live(comp) and not is_admin:
        session.pop("active_comp_slug", None)
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "That competition isn’t live right now — leaderboard is unavailable.",
                    "climbs": [],
                }
            ),
            200,
        )

    competitor_id = request.args.get("competitor_id", type=int)
    if not competitor_id:
        return jsonify({"ok": False, "error": "Missing competitor_id", "climbs": []}), 400

    climbs = get_top_climbs_for_competitor(
        competition_id=comp.id, competitor_id=competitor_id, limit=8
    )

    return jsonify({"ok": True, "climbs": climbs}), 200


def _paginate(rows, page, per_page):
    page = page if isinstance(page, int) and page > 0 else 1
    per_page = per_page if isinstance(per_page, int) and per_page > 0 else DEFAULT_LB_PER_PAGE
    total = len(rows)
    total_pages = max(1, (total + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages
    start = (page - 1) * per_page
    end = start + per_page
    return rows[start:end], page, per_page, total, total_pages


@scores_bp.route("/leaderboard")
def leaderboard_all():
    cid_raw = (request.args.get("cid") or "").strip()
    competitor = Competitor.query.get(int(cid_raw)) if cid_raw.isdigit() else None  # noqa: F841

    comp = get_viewer_comp()

    if not comp:
        flash("Pick a competition first to view the leaderboard.", "warning")
        return redirect("/my-comps")

    is_admin = admin_can_manage_competition(comp)

    if not comp_is_live(comp) and not is_admin:
        session.pop("active_comp_slug", None)
        flash("That competition isn’t live right now — leaderboard is unavailable.", "warning")
        return redirect("/my-comps")

    page = request.args.get("page", 1, type=int)
    per_page = DEFAULT_LB_PER_PAGE

    cat = "all"
    rows, category_label = build_leaderboard(cat, competition_id=comp.id)

    page_rows, page, per_page, total, total_pages = _paginate(rows, page, per_page)

    return render_template(
        "leaderboard.html",
        leaderboard=page_rows,
        category=category_label,
        current_competitor_id=session.get("competitor_id"),
        nav_active="leaderboard",
        comp=comp,
        comp_slug=comp.slug,
        page=page,
        per_page=per_page,
        total=total,
        total_pages=total_pages,
    )


@scores_bp.route("/leaderboard/<category>")
def leaderboard_by_category(category):
    cid_raw = (request.args.get("cid") or "").strip()
    competitor = Competitor.query.get(int(cid_raw)) if cid_raw.isdigit() else None  # noqa: F841

    comp = get_viewer_comp()

    if not comp:
        flash("Pick a competition first to view the leaderboard.", "warning")
        return redirect("/my-comps")

    is_admin = admin_can_manage_competition(comp)

    if not comp_is_live(comp) and not is_admin:
        session.pop("active_comp_slug", None)
        flash("That competition isn’t live right now — leaderboard is unavailable.", "warning")
        return redirect("/my-comps")

    page = request.args.get("page", 1, type=int)
    per_page = DEFAULT_LB_PER_PAGE

    cat = normalize_leaderboard_category(category)
    rows, category_label = build_leaderboard(cat, competition_id=comp.id)

    page_rows, page, per_page, total, total_pages = _paginate(rows, page, per_page)

    return render_template(
        "leaderboard.html",
        leaderboard=page_rows,
        category=category_label,
        current_competitor_id=session.get("competitor_id"),
        nav_active="leaderboard",
        comp=comp,
        comp_slug=comp.slug,
        page=page,
        per_page=per_page,
        total=total,
        total_pages=total_pages,
    )


@scores_bp.route("/leaderboard/comp/<int:comp_id>")
def leaderboard_for_comp_id(comp_id):
    comp = Competition.query.get_or_404(comp_id)

    is_admin = admin_can_manage_competition(comp)
    if not is_admin and not comp_is_live(comp):
        return render_template("leaderboard.html", comp=None, not_live=True)

    session["active_comp_slug"] = comp.slug

    admin_q = (request.args.get("admin") or "").strip()
    if is_admin:
        admin_q = "1"

    if admin_q == "1":
        return redirect("/leaderboard?admin=1")

    return redirect("/leaderboard")


@scores_bp.route("/api/leaderboard")
def api_leaderboard():
    raw_category = request.args.get("category")
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", DEFAULT_LB_PER_PAGE, type=int)

    comp = get_viewer_comp()
    if not comp:
        resp = make_response(jsonify({
            "category": "No competition selected",
            "rows": [],
            "req_id": None,
            "page": 1,
            "per_page": per_page,
            "total": 0,
            "total_pages": 1,
        }))
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        return resp

    is_admin = admin_can_manage_competition(comp)

    if not comp_is_live(comp) and not is_admin:
        session.pop("active_comp_slug", None)
        resp = make_response(jsonify({
            "category": "Competition not live",
            "rows": [],
            "req_id": None,
            "page": 1,
            "per_page": per_page,
            "total": 0,
            "total_pages": 1,
        }))
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        return resp

    try:
        cat = normalize_leaderboard_category(raw_category)
    except Exception:
        cat = "all"

    allowed = {"all", "male", "female", "inclusive", "doubles"}
    if cat not in allowed:
        cat = "all"

    if not page or page < 1:
        page = 1
    if not per_page or per_page < 1:
        per_page = DEFAULT_LB_PER_PAGE
    if per_page > 50:
        per_page = 50

    req_id = int(time.time() * 1000)

    try:
        current_app.logger.info(
            "LB API req_id=%s raw_category=%r normalized=%r comp_id=%s page=%s per_page=%s",
            req_id, raw_category, cat, comp.id, page, per_page
        )
    except Exception:
        pass

    rows, category_label = build_leaderboard(cat, competition_id=comp.id)
    page_rows, page, per_page, total, total_pages = _paginate(rows, page, per_page)

    resp = make_response(jsonify({
        "category": category_label,
        "rows": page_rows,
        "req_id": req_id,
        "cat_key": cat,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
    }))

    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@scores_bp.route("/admin/competition/<slug>/export-final-results.csv")
def export_final_results_csv(slug):
    comp = Competition.query.filter_by(slug=slug).first_or_404()

    if not admin_can_manage_competition(comp):
        abort(403)

    rows = build_final_results_csv_rows(comp)

    fieldnames = [
        "category",
        "position",
        "total_competitors",
        "competitor_name",
        "competitor_email",
        "score",
    ]

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()

    for row in rows:
        writer.writerow(row)

    csv_data = buffer.getvalue()
    buffer.close()

    response = make_response(csv_data)
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = (
        f'attachment; filename="{comp.slug}-final-results.csv"'
    )
    return response