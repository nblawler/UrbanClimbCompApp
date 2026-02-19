from datetime import datetime
from app.extensions import db

class Competition(db.Model):
    __tablename__ = "competition"

    id = db.Column(db.Integer, primary_key=True)

    name = db.Column(db.String(160), nullable=False)
    gym_name = db.Column(db.String(160), nullable=True)

    gym_id = db.Column(
        db.Integer,
        db.ForeignKey("gym.id"),
        nullable=False,
        index=True,
    )

    slug = db.Column(db.String(160), nullable=False, unique=True)

    start_at = db.Column(db.DateTime, nullable=True)
    end_at = db.Column(db.DateTime, nullable=True)

    is_active = db.Column(db.Boolean, nullable=False, default=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    gym = db.relationship("Gym", back_populates="competitions")

    sections = db.relationship(
        "Section",
        back_populates="competition",
        lazy=True,
    )

    competitors = db.relationship(
        "Competitor",
        back_populates="competition",
        lazy=True,
    )
