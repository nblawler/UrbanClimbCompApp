from datetime import datetime
from sqlalchemy import UniqueConstraint
from app.extensions import db

class Competitor(db.Model):
    __tablename__ = "competitor"

    id = db.Column(db.Integer, primary_key=True)

    name = db.Column(db.String(120), nullable=False)
    gender = db.Column(db.String(20), nullable=False, default="Inclusive")

    # Keep for now (legacy) — but your logic should treat Account.email as source of truth.
    email = db.Column(db.String(255), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    competition_id = db.Column(
        db.Integer,
        db.ForeignKey("competition.id"),
        nullable=True,
        index=True,
    )

    # NEW: the stable user identity
    account_id = db.Column(
        db.Integer,
        db.ForeignKey("account.id"),
        nullable=False,
        index=True,
    )

    account = db.relationship("Account", back_populates="competitors")

    competition = db.relationship(
        "Competition",
        back_populates="competitors",
    )

    __table_args__ = (
        # NEW uniqueness: one competitor per comp per account
        UniqueConstraint("competition_id", "account_id", name="uq_competition_account"),
    )
