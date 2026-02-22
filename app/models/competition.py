from datetime import datetime
from app.extensions import db

class Competition(db.Model):
    __tablename__ = "competition"

    id = db.Column(db.Integer, primary_key=True)

    # Public-facing name, e.g. "UC Collingwood Boulder Blitz"
    name = db.Column(db.String(160), nullable=False)

    # Optional extra context (legacy free-text)
    gym_name = db.Column(db.String(160), nullable=True)

    # NEW: formal gym relationship
    gym_id = db.Column(
        db.Integer,
        db.ForeignKey("gym.id"),
        nullable=False,
        index=True,
    )
    gym = db.relationship(
        "Gym",
        back_populates="competitions",
    )

    # For pretty URLs later, e.g. "uc-collingwood-boulder-blitz"
    slug = db.Column(db.String(160), nullable=False, unique=True)

    # When the comp runs (optional for now)
    start_at = db.Column(db.DateTime, nullable=True)
    end_at = db.Column(db.DateTime, nullable=True)

    # Let you “archive” comps without deleting them
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    # Relationships
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
