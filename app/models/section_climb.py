from sqlalchemy import UniqueConstraint
from app.extensions import db

class SectionClimb(db.Model):
    __tablename__ = "section_climb"

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
