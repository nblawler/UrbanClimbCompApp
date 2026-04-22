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
import zipfile
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
from app.helpers.new_leaderboard import refresh_leaderboard_row, refresh_doubles_leaderboard_row

scores_bp = Blueprint("scores", __name__)

DEFAULT_LB_PER_PAGE = 10


def _normalize_competitor_category(value):
    s = (value or "").strip().lower()
    s = s.replace("_", " ").replace("-", " ")

    if s in {"male", "man", "men", "m"}:
        return "male"
    if s in {"female", "woman", "women", "f"}:
        return "female"
    if s in {
        "inclusive",
        "gender inclusive",
        "genderinclusive",
        "open",
        "non binary",
        "non-binary",
        "nb",
    }:
        return "inclusive"
    if s in {"doubles", "double"}:
        return "doubles"

    return s


def _get_competitor_category_key(competitor):
    for attr in ("category", "registration_category", "division", "gender"):
        if hasattr(competitor, attr):
            raw = getattr(competitor, attr)
            if raw:
                return _normalize_competitor_category(raw)
    return ""


def _category_label(category):
    if category == "all":
        return "All"
    if category == "male":
        return "Male"
    if category == "female":
        return "Female"
    if category == "inclusive":
        return "Inclusive"
    if category == "doubles":
        return "Doubles"
    return category.title()


def _load_competitor_hero(cid_raw, competition_id):
    from app.models import Competitor, Leaderboard

    competitor = None
    total_points = None
    position = None

    cid = None
    if cid_raw:
        try:
            cid = int(cid_raw)
        except (TypeError, ValueError):
            pass

    if not cid:
        return None, None, None

    competitor = Competitor.query.get(cid)
    if not competitor or competitor.competition_id != competition_id:
        return None, None, None

    lb_row = Leaderboard.query.filter_by(
        competitor_id=cid,
        competition_id=competition_id,
    ).first()

    total_points = lb_row.total_points if lb_row else 0

    if lb_row:
        position = (
            Leaderboard.query
            .filter(
                Leaderboard.competition_id == competition_id,
                db.or_(
                    Leaderboard.total_points > lb_row.total_points,
                    db.and_(
                        Leaderboard.total_points == lb_row.total_points,
                        Leaderboard.attempts_on_tops < lb_row.attempts_on_tops,
                    ),
                ),
            )
            .count()
        ) + 1

    return competitor, total_points, position


def build_final_results_rows_all(comp):
    TOP_N = 8

    competitors = (
        db.session.query(Competitor)
        .filter(Competitor.competition_id == comp.id)
        .all()
    )

    if not competitors:
        return []

    competitor_ids = [c.id for c in competitors]

    score_rows = (
        db.session.query(
            Score.competitor_id,
            Score.climb_number,
            Score.attempts,
            Score.topped,
            Score.section_climb_id,
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

        topped_scored.sort(
            key=lambda x: (-x["points"], x["attempts"], x.get("climb_number") or 0)
        )

        topN = topped_scored[:TOP_N]

        total_points = sum(x["points"] for x in topN)
        attempts_on_tops = sum(x["attempts"] for x in topN)

        rows.append({
            "competitor_id": c.id,
            "competitor_name": c.name or "",
            "category_key": _get_competitor_category_key(c),
            "score": total_points,
            "attempts_on_tops": attempts_on_tops,
        })

    rows.sort(
        key=lambda r: (
            -int(r["score"]),
            int(r["attempts_on_tops"]),
            (r["competitor_name"] or "").lower(),
        )
    )

    return rows


def build_final_results_csv_rows_for_category(comp, category):
    all_rows = build_final_results_rows_all(comp)

    if category == "all":
        filtered = all_rows
    else:
        filtered = [r for r in all_rows if r.get("category_key") == category]

    total_competitors = len(filtered)
    output_rows = []

    pos = 0
    prev_key = None

    for row in filtered:
        k = (row["score"], row["attempts_on_tops"])
        if k != prev_key:
            pos += 1
        prev_key = k

        output_rows.append({
            "category": _category_label(category),
            "position": pos,
            "total_competitors": total_competitors,
            "competitor_name": row["competitor_name"],
            "score": row["score"],
        })

    return output_rows


def build_export_rows_from_leaderboard(comp, category):
    result, category_label = build_leaderboard(category, competition_id=comp.id)

    # Doubles returns a list of dicts already
    if category == "doubles":
        rows = result or []
        total_competitors = len(rows)
        return [
            {
                "category":          category_label,
                "position":          row.get("position", ""),
                "total_competitors": total_competitors,
                "team_name":         row.get("name", ""),
                "score":             row.get("total_points", 0),
            }
            for row in rows
        ]

    # Singles returns a query — execute it and build row dicts
    all_records = result.all() if result is not None else []
    total_competitors = len(all_records)

    output_rows = []
    current_position = 0
    previous_rank_values = None

    for leaderboard_record, competitor in all_records:
        total_points = int(leaderboard_record.total_points or 0)
        attempts_on_tops = int(leaderboard_record.attempts_on_tops or 0)

        current_rank_values = (total_points, attempts_on_tops)
        if current_rank_values != previous_rank_values:
            current_position += 1
        previous_rank_values = current_rank_values

        output_rows.append({
            "category":          category_label,
            "position":          current_position,
            "total_competitors": total_competitors,
            "competitor_name":   competitor.name,
            "score":             total_points,
        })

    return output_rows


def _rows_to_csv_string(rows, category="singles"):
    if category == "doubles":
        fieldnames = [
            "category",
            "position",
            "total_competitors",
            "team_name",
            "score",
        ]
    else:
        fieldnames = [
            "category",
            "position",
            "total_competitors",
            "competitor_name",
            "score",
        ]

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()

    for row in rows:
        writer.writerow(row)

    csv_data = buffer.getvalue()
    buffer.close()
    return csv_data


def _paginate(query, page, per_page):
    page = page if isinstance(page, int) and page > 0 else 1
    per_page = per_page if isinstance(per_page, int) and per_page > 0 else DEFAULT_LB_PER_PAGE

    total = query.count()
    total_pages = max(1, (total + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages

    rows = query.offset((page - 1) * per_page).limit(per_page).all()
    return rows, page, per_page, total, total_pages


def _paginate_list(rows, page, per_page):
    page = page if isinstance(page, int) and page > 0 else 1
    per_page = per_page if isinstance(per_page, int) and per_page > 0 else DEFAULT_LB_PER_PAGE
    total = len(rows)
    total_pages = max(1, (total + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages
    start = (page - 1) * per_page
    end = start + per_page
    return rows[start:end], page, per_page, total, total_pages


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
        Score.query.filter_by(
            competitor_id=competitor_id,
            section_climb_id=sc.id,
        ).first()
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

    refresh_leaderboard_row(
        competitor_id=competitor_id,
        competition_id=current_comp.id,
        top_n=8,
    )

    refresh_doubles_leaderboard_row(
        competitor_id=competitor_id,
        competition_id=current_comp.id,
        top_n=8,
    )

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
    comp_id = competitor.competition_id

    rows = (
        db.session.query(
            Score.climb_number,
            Score.section_climb_id,
            Score.attempts,
            Score.topped,
            Score.flashed,
            SectionClimb.base_points,
        )
        .join(SectionClimb, SectionClimb.id == Score.section_climb_id)
        .join(Section, Section.id == SectionClimb.section_id)
        .filter(
            Score.competitor_id == competitor_id,
            Section.competition_id == comp_id,
        )
        .order_by(Score.climb_number.asc())
        .all()
    )

    out = []
    for r in rows:
        pts = int(r.base_points or 0) if r.topped else 0
        out.append({
            "climb_number": r.climb_number,
            "section_climb_id": r.section_climb_id,
            "attempts": r.attempts,
            "topped": r.topped,
            "flashed": r.flashed or False,
            "points": pts,
        })

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
                    "error": "That competition isn't live right now — leaderboard is unavailable.",
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


@scores_bp.route("/leaderboard")
def leaderboard_all():
    comp = get_viewer_comp()

    if not comp:
        flash("Pick a competition first to view the leaderboard.", "warning")
        return redirect("/my-comps")

    is_admin = admin_can_manage_competition(comp)

    if not comp_is_live(comp) and not is_admin:
        session.pop("active_comp_slug", None)
        flash("That competition isn't live right now — leaderboard is unavailable.", "warning")
        return redirect("/my-comps")

    cid_raw = (request.args.get("cid") or str(session.get("competitor_id") or "")).strip()
    competitor, total_points, position = _load_competitor_hero(cid_raw, comp.id)

    page = request.args.get("page", 1, type=int)
    per_page = DEFAULT_LB_PER_PAGE

    # Initial render — JS immediately takes over, so just use _paginate_list
    query, category_label = build_leaderboard("all", competition_id=comp.id)
    raw_rows, page, per_page, total, total_pages = _paginate(query, page, per_page)

    page_rows = []
    current_position = (page - 1) * per_page
    previous_rank_values = None
    for leaderboard_record, competitor in raw_rows:
        total_points = int(leaderboard_record.total_points or 0)
        attempts_on_tops = int(leaderboard_record.attempts_on_tops or 0)
        current_rank_values = (total_points, attempts_on_tops)
        if current_rank_values != previous_rank_values:
            current_position += 1
        previous_rank_values = current_rank_values
        page_rows.append({
            "competitor_id":    competitor.id,
            "name":             competitor.name,
            "gender":           competitor.gender,
            "tops":             int(leaderboard_record.tops or 0),
            "attempts_on_tops": attempts_on_tops,
            "total_points":     total_points,
            "last_update":      leaderboard_record.last_update,
            "position":         current_position,
        })

    return render_template(
        "leaderboard.html",
        leaderboard=page_rows,
        category=category_label,
        current_competitor_id=session.get("competitor_id"),
        competitor=competitor,
        total_points=total_points,
        position=position,
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
    comp = get_viewer_comp()

    if not comp:
        flash("Pick a competition first to view the leaderboard.", "warning")
        return redirect("/my-comps")

    is_admin = admin_can_manage_competition(comp)

    if not comp_is_live(comp) and not is_admin:
        session.pop("active_comp_slug", None)
        flash("That competition isn't live right now — leaderboard is unavailable.", "warning")
        return redirect("/my-comps")

    cid_raw = (request.args.get("cid") or str(session.get("competitor_id") or "")).strip()
    competitor, total_points, position = _load_competitor_hero(cid_raw, comp.id)

    page = request.args.get("page", 1, type=int)
    per_page = DEFAULT_LB_PER_PAGE

    cat = normalize_leaderboard_category(category)

    if cat == "doubles":
        rows, category_label = build_leaderboard(cat, competition_id=comp.id)
        page_rows, page, per_page, total, total_pages = _paginate_list(rows, page, per_page)
    else:
        query, category_label = build_leaderboard(cat, competition_id=comp.id)
        raw_rows, page, per_page, total, total_pages = _paginate(query, page, per_page)

        page_rows = []
        current_position = (page - 1) * per_page
        previous_rank_values = None
        for leaderboard_record, competitor in raw_rows:
            total_points = int(leaderboard_record.total_points or 0)
            attempts_on_tops = int(leaderboard_record.attempts_on_tops or 0)
            current_rank_values = (total_points, attempts_on_tops)
            if current_rank_values != previous_rank_values:
                current_position += 1
            previous_rank_values = current_rank_values
            page_rows.append({
                "competitor_id":    competitor.id,
                "name":             competitor.name,
                "gender":           competitor.gender,
                "tops":             int(leaderboard_record.tops or 0),
                "attempts_on_tops": attempts_on_tops,
                "total_points":     total_points,
                "last_update":      leaderboard_record.last_update,
                "position":         current_position,
            })

    return render_template(
        "leaderboard.html",
        leaderboard=page_rows,
        category=category_label,
        current_competitor_id=session.get("competitor_id"),
        competitor=competitor,
        total_points=total_points,
        position=position,
        nav_active="leaderboard",
        comp=comp,
        comp_slug=comp.slug,
        page=page,
        per_page=per_page,
        total=total,
        total_pages=total_pages,
    )


@scores_bp.route("/leaderboard/comp/<int:comp_id>")
def leaderboard_for_comp(comp_id):
    comp = Competition.query.get_or_404(comp_id)

    from_route_setter = (request.args.get("from") or "").strip() == "route_setter"

    # Store comp name so the leaderboard page can display it
    session["leaderboard_comp_name"] = comp.name

    if admin_can_manage_competition(comp):
        session["admin_comp_id"] = comp.id
        session["active_comp_slug"] = comp.slug

        target = "/leaderboard?admin=1"
        if from_route_setter:
            target += "&from=route_setter"
        return redirect(target)

    session["active_comp_slug"] = comp.slug

    target = "/leaderboard"
    if from_route_setter:
        target += "?from=route_setter"
    return redirect(target)


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

    # Doubles uses a precomputed list — paginate in Python
    if cat == "doubles":
        all_rows, category_label = build_leaderboard(cat, competition_id=comp.id)
        page_rows, page, per_page, total, total_pages = _paginate_list(all_rows, page, per_page)

    # Singles/filtered — paginate in SQL, only fetch what we need
    else:
        query, category_label = build_leaderboard(cat, competition_id=comp.id)
        raw_rows, page, per_page, total, total_pages = _paginate(query, page, per_page)

        page_rows = []
        current_position = (page - 1) * per_page
        previous_rank_values = None

        for leaderboard_record, competitor in raw_rows:
            total_points = int(leaderboard_record.total_points or 0)
            attempts_on_tops = int(leaderboard_record.attempts_on_tops or 0)

            current_rank_values = (total_points, attempts_on_tops)
            if current_rank_values != previous_rank_values:
                current_position += 1
            previous_rank_values = current_rank_values

            page_rows.append({
                "competitor_id":    competitor.id,
                "name":             competitor.name,
                "gender":           competitor.gender,
                "tops":             int(leaderboard_record.tops or 0),
                "attempts_on_tops": attempts_on_tops,
                "total_points":     total_points,
                "last_update":      leaderboard_record.last_update,
                "position":         current_position,
            })

    resp = make_response(jsonify({
        "category": category_label,
        "rows":     page_rows,
        "req_id":   req_id,
        "cat_key":  cat,
        "page":     page,
        "per_page": per_page,
        "total":    total,
        "total_pages": total_pages,
    }))

    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@scores_bp.route("/admin/competition/<slug>/export-final-results.zip")
def export_final_results_zip(slug):
    comp = Competition.query.filter_by(slug=slug).first_or_404()

    if not admin_can_manage_competition(comp):
        abort(403)

    categories = ["all", "male", "female", "inclusive", "doubles"]

    zip_buffer = io.BytesIO()

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for category in categories:
            rows = build_export_rows_from_leaderboard(comp, category)
            csv_data = _rows_to_csv_string(rows, category)
            filename = f"{comp.slug}-{category}-results.csv"
            zf.writestr(filename, csv_data)

    zip_buffer.seek(0)

    response = make_response(zip_buffer.getvalue())
    response.headers["Content-Type"] = "application/zip"
    response.headers["Content-Disposition"] = (
        f'attachment; filename="{comp.slug}-final-results-by-category.zip"'
    )
    return response