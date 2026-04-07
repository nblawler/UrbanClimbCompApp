from datetime import datetime
from app.extensions import db

class Leaderboard(db.Model):
    __tablename__ = "leaderboard"

    id = db.Column(db.Integer, primary_key=True)

    competitor_id = db.Column(
        db.Integer,
        db.ForeignKey("competitor.id"),
        nullable=False,
        index=True,
    )

    competition_id = db.Column(
        db.Integer,
        db.ForeignKey("competition.id"),
        nullable=False,
        index=True,
    )

    total_points = db.Column(db.Integer, nullable=False, default=0)
    attempts_on_tops = db.Column(db.Integer, nullable=False, default=0)
    tops = db.Column(db.Integer, nullable=False, default=0)

    last_update = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint(
            "competitor_id",
            "competition_id",
            name="leaderboard_competitor_competition",
        ),
    )