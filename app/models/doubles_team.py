from datetime import datetime, timezone
from app.extensions import db

class DoublesTeam(db.Model):
    __tablename__ = "doubles_team"

    id = db.Column(db.Integer, primary_key=True)
    competition_id = db.Column(db.Integer, db.ForeignKey("competition.id", ondelete="CASCADE"), nullable=False)

    competitor_a_id = db.Column(db.Integer, db.ForeignKey("competitor.id", ondelete="CASCADE"), nullable=False)
    competitor_b_id = db.Column(db.Integer, db.ForeignKey("competitor.id", ondelete="CASCADE"), nullable=False)

    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

