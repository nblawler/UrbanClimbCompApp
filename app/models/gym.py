from datetime import datetime
from app.extensions import db

class Gym(db.Model):
    __tablename__ = "gym"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    slug = db.Column(db.String(160), nullable=False, unique=True)
    map_image_path = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    competitions = db.relationship(
        "Competition",
        back_populates="gym",
        lazy=True,
    )
