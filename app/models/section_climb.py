from sqlalchemy import UniqueConstraint
from app.extensions import db

CLIMB_STYLES = ("balance", "power", "coordination")

CLIMB_STYLE_LABELS = {
    "balance":       "Balance",
    "power":         "Power",
    "coordination":  "Coordination",
}


class SectionClimb(db.Model):
    __tablename__ = "section_climb"

    id = db.Column(db.Integer, primary_key=True)

    section_id = db.Column(
        db.Integer,
        db.ForeignKey("section.id"),
        nullable=False,
        index=True,
    )

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
        index=True,
    )

    # Physical hold colour on the wall — purely for identification.
    # Separate from grade; a red hold climb can be any grade.
    colour = db.Column(db.String(80), nullable=True)

    # Difficulty grade — interpreted via gym.grading_system.
    # Examples: "Blue" (colour gym), "V4" (v_grade gym), "6" (numeric gym).
    grade = db.Column(db.String(20), nullable=True)

    # Movement styles of the climb — stored as a JSON array since a climb can
    # have multiple styles e.g. ["power", "coordination"].
    # Valid values per entry: "balance", "power", "coordination"
    # nullable in DB so migration is safe; UI enforces at least one selection.
    styles = db.Column(db.JSON, nullable=True, default=None)

    # Per-climb scoring config (admin editable)
    base_points = db.Column(db.Integer, nullable=True)
    penalty_per_attempt = db.Column(db.Integer, nullable=True)
    attempt_cap = db.Column(db.Integer, nullable=True)

    # Where this climb sits on the map (% of width/height)
    x_percent = db.Column(db.Float, nullable=True)
    y_percent = db.Column(db.Float, nullable=True)

    section = db.relationship("Section", backref=db.backref("climbs", lazy=True))

    __table_args__ = (
        UniqueConstraint("section_id", "climb_number", name="uq_section_climb"),
    )