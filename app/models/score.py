from datetime import datetime
from sqlalchemy import UniqueConstraint
from app.extensions import db

class Score(db.Model):
    __tablename__ = "scores"

    id = db.Column(db.Integer, primary_key=True)

    competitor_id = db.Column(
        db.Integer,
        db.ForeignKey("competitor.id"),
        nullable=False,
        index=True,
    )

    climb_number = db.Column(
        db.Integer,
        nullable=False,
        index=True,
    )

    attempts = db.Column(db.Integer, nullable=False, default=0)
    topped = db.Column(db.Boolean, nullable=False, default=False)

    section_climb_id = db.Column(
        db.Integer,
        db.ForeignKey("section_climb.id"),
        nullable=False,
        index=True,
    )

    flashed = db.Column(db.Boolean, nullable=False, default=False)

    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    __table_args__ = (
        UniqueConstraint(
            "competitor_id",
            "section_climb_id",
            name="uq_competitor_section_climb",
        ),
    )

    competitor = db.relationship(
        "Competitor",
        backref=db.backref("scores", lazy=True),
    )

    section_climb = db.relationship("SectionClimb")
