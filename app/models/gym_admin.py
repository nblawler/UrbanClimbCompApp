from datetime import datetime
from sqlalchemy import UniqueConstraint
from app.extensions import db

class GymAdmin(db.Model):
    __tablename__ = "gym_admin"

    id = db.Column(db.Integer, primary_key=True)

    # Legacy for now (existing DB column). Keep it so old data loads.
    competitor_id = db.Column(
        db.Integer,
        db.ForeignKey("competitor.id"),
        nullable=False,
        index=True,
    )

    # NEW: stable identity
    account_id = db.Column(
        db.Integer,
        db.ForeignKey("account.id"),
        nullable=False,
        index=True,
    )

    gym_id = db.Column(
        db.Integer,
        db.ForeignKey("gym.id"),
        nullable=False,
        index=True,
    )

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    account = db.relationship("Account", back_populates="gym_admins")

    __table_args__ = (
        UniqueConstraint("account_id", "gym_id", name="uq_gym_admin_account_gym"),
    )
