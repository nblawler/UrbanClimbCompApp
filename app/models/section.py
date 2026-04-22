from sqlalchemy import UniqueConstraint
from app.extensions import db


class Section(db.Model):
    __tablename__ = "section"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    slug = db.Column(db.String(120), nullable=False)

    start_climb = db.Column(db.Integer, nullable=False, default=0)
    end_climb   = db.Column(db.Integer, nullable=False, default=0)

    # Gym-level — sections belong to a gym, not a competition
    gym_id = db.Column(db.Integer, db.ForeignKey("gym.id"), nullable=False, index=True)
    gym    = db.relationship("Gym", backref=db.backref("sections", lazy=True))

    # Soft reference — kept for historical queries but no longer drives uniqueness
    competition_id = db.Column(
        db.Integer,
        db.ForeignKey("competition.id"),
        nullable=True,
        index=True,
    )
    competition = db.relationship("Competition", back_populates="sections")

    # Polygon boundary — [{"x": 12.34, "y": 56.78}, ...]  x/y = % of image size
    boundary_points_json = db.Column(db.Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("gym_id", "name", name="uq_section_gym_name"),
        UniqueConstraint("gym_id", "slug", name="uq_section_gym_slug"),
    )