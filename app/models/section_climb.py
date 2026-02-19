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

    gym_id = db.Column(
        db.Integer,
        db.ForeignKey("gym.id"),
        nullable=True,
        index=True,
    )

    climb_number = db.Column(
        db.Integer,
        nullable=False,
        index=True,
    )

    colour = db.Column(db.String(80), nullable=True)

    base_points = db.Column(db.Integer, nullable=True)
    penalty_per_attempt = db.Column(db.Integer, nullable=True)
    attempt_cap = db.Column(db.Integer, nullable=True)

    x_percent = db.Column(db.Float, nullable=True)
    y_percent = db.Column(db.Float, nullable=True)

    section = db.relationship(
        "Section",
        backref=db.backref("climbs", lazy=True),
    )

    gym = db.relationship("Gym")

    __table_args__ = (
        UniqueConstraint(
            "section_id",
            "climb_number",
            name="uq_section_climb",
        ),
    )
