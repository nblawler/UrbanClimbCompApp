from sqlalchemy import UniqueConstraint
from app.extensions import db

class Section(db.Model):
    __tablename__ = "section"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    slug = db.Column(db.String(120), nullable=False)

    start_climb = db.Column(db.Integer, nullable=False, default=0)
    end_climb = db.Column(db.Integer, nullable=False, default=0)

    gym_id = db.Column(
        db.Integer,
        db.ForeignKey("gym.id"),
        nullable=True,
        index=True,
    )

    competition_id = db.Column(
        db.Integer,
        db.ForeignKey("competition.id"),
        nullable=True,
        index=True,
    )

    boundary_points_json = db.Column(db.Text, nullable=True)

    gym = db.relationship("Gym")
    competition = db.relationship("Competition", back_populates="sections")

    __table_args__ = (
        UniqueConstraint("competition_id", "name", name="uq_section_comp_name"),
        UniqueConstraint("competition_id", "slug", name="uq_section_comp_slug"),
    )
