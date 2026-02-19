from datetime import datetime
from sqlalchemy import UniqueConstraint
from app.extensions import db

class Competitor(db.Model):
    __tablename__ = "competitor"

    id = db.Column(db.Integer, primary_key=True)

    name = db.Column(db.String(120), nullable=False)
    gender = db.Column(db.String(20), nullable=False, default="Inclusive")
    email = db.Column(db.String(255), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    competition_id = db.Column(
        db.Integer,
        db.ForeignKey("competition.id"),
        nullable=True,
        index=True,
    )

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
        UniqueConstraint(
            "competition_id",
            "account_id",
            name="uq_competition_account",
        ),
    )
