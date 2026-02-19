from datetime import datetime
from app.extensions import db

class LoginCode(db.Model):
    __tablename__ = "login_code"

    id = db.Column(db.Integer, primary_key=True)

    competitor_id = db.Column(
        db.Integer,
        db.ForeignKey("competitor.id"),
        nullable=False,
    )

    account_id = db.Column(
        db.Integer,
        db.ForeignKey("account.id"),
        nullable=False,
        index=True,
    )

    code = db.Column(db.String(6), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used = db.Column(db.Boolean, default=False, nullable=False)

    competitor = db.relationship("Competitor")
    account = db.relationship("Account", back_populates="login_codes")
