from sqlalchemy import UniqueConstraint
from app.extensions import db

CLIMB_STYLES = ("balance", "power", "coordination")

CLIMB_STYLE_LABELS = {
    "balance":      "Balance",
    "power":        "Power",
    "coordination": "Coordination",
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

    # Competition that this climb belongs to — needed so climb numbers
    # are unique per competition (not just per section or per gym).
    competition_id = db.Column(
        db.Integer,
        db.ForeignKey("competition.id"),
        nullable=True,
        index=True,
    )

    climb_number = db.Column(db.Integer, nullable=False, index=True)

    colour     = db.Column(db.String(80),  nullable=True)
    grade      = db.Column(db.String(20),  nullable=True)
    styles     = db.Column(db.JSON,        nullable=True, default=None)

    base_points          = db.Column(db.Integer, nullable=True)
    penalty_per_attempt  = db.Column(db.Integer, nullable=True)
    attempt_cap          = db.Column(db.Integer, nullable=True)

    # Map position — null until the route setter places it on the map
    x_percent = db.Column(db.Float, nullable=True)
    y_percent = db.Column(db.Float, nullable=True)

    section = db.relationship("Section", backref=db.backref("climbs", lazy=True))

    __table_args__ = (
        # Climb numbers must be unique within a competition
        UniqueConstraint("competition_id", "climb_number", name="uq_comp_climb_number"),
    )