from datetime import datetime, timezone
from app.extensions import db

class DoublesInvite(db.Model):
    __tablename__ = "doubles_invite"

    id = db.Column(db.Integer, primary_key=True)
    competition_id = db.Column(db.Integer, db.ForeignKey("competition.id", ondelete="CASCADE"), nullable=False)
    inviter_competitor_id = db.Column(db.Integer, db.ForeignKey("competitor.id", ondelete="CASCADE"), nullable=False)

    invitee_email = db.Column(db.Text, nullable=False)
    token_hash = db.Column(db.Text, nullable=False)

    status = db.Column(db.Text, nullable=False)  # 'pending','accepted','declined','expired','cancelled'
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    accepted_at = db.Column(db.DateTime(timezone=True), nullable=True)

