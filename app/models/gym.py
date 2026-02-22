from datetime import datetime
from app.extensions import db

class Gym(db.Model):
    __tablename__ = "gym"

    id = db.Column(db.Integer, primary_key=True)

    # Public name of the gym, e.g. "UC Collingwood"
    name = db.Column(db.String(160), nullable=False)

    # For pretty URLs and also for mapping to a static map image if you want
    slug = db.Column(db.String(160), nullable=False, unique=True)

    # Path or URL to the map image for this gym
    # Example: "/static/maps/collingwood-map.png"
    map_image_path = db.Column(db.String(255), nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    competitions = db.relationship(
        "Competition",
        back_populates="gym",
        lazy=True,
    )
