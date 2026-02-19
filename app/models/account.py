from datetime import datetime
from app.extensions import db

class Account(db.Model):
    __tablename__ = "account"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), nullable=False, unique=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    competitors = db.relationship("Competitor", back_populates="account")
    login_codes = db.relationship("LoginCode", back_populates="account")
    gym_admins = db.relationship("GymAdmin", back_populates="account")
