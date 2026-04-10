from datetime import datetime, timezone
from app.extensions import db

class DoublesLeaderboard(db.Model):
    __tablename__ = "doubles_leaderboard"

    id = db.Column(db.Integer, primary_key=True)

    team_id = db.Column(
        db.Integer,
        db.ForeignKey("doubles_team.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    competition_id = db.Column(
        db.Integer,
        db.ForeignKey("competition.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    total_points     = db.Column(db.Integer, nullable=False, default=0)
    attempts_on_tops = db.Column(db.Integer, nullable=False, default=0)

    # Cached member details for instant expand — no extra queries needed
    a_id     = db.Column(db.Integer, nullable=False)
    a_name   = db.Column(db.String(255), nullable=False, default="")
    a_climbs = db.Column(db.JSON, nullable=False, default=list)

    b_id     = db.Column(db.Integer, nullable=False)
    b_name   = db.Column(db.String(255), nullable=False, default="")
    b_climbs = db.Column(db.JSON, nullable=False, default=list)

    last_update = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        db.UniqueConstraint(
            "team_id",
            "competition_id",
            name="doubles_leaderboard_team_competition",
        ),
    )