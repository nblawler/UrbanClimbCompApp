from datetime import datetime
from app.extensions import db

GRADING_SYSTEMS = ("colour", "v_grade", "numeric")

GRADING_SYSTEM_LABELS = {
    "colour":  "Colour",
    "v_grade": "V Grade",
    "numeric": "Numeric (1–9)",
}


class Gym(db.Model):
    __tablename__ = "gym"

    id = db.Column(db.Integer, primary_key=True)

    name = db.Column(db.String(160), nullable=False)
    slug = db.Column(db.String(160), nullable=False, unique=True)
    map_image_path = db.Column(db.String(255), nullable=True)

    # How grades are expressed at this gym.
    # Set once by a gym admin via Gym Settings — does not change per competition.
    grading_system = db.Column(
        db.Enum(*GRADING_SYSTEMS, name="grading_system_enum", native_enum=False),
        nullable=True,
        default=None,
    )

    # Ordered list of grade colours for colour-graded gyms.
    # Each entry: {"label": "Blue", "colour": "#1e90ff"}
    # Ordered easiest → hardest.
    # Null for v_grade and numeric gyms (their lists are fixed).
    grade_list = db.Column(db.JSON, nullable=True, default=None)

    # Ordered list of hold colours used at this gym.
    # Each entry: {"label": "Red", "colour": "#ff0000"}
    # Always gym-defined regardless of grading system —
    # hold colour is for physical identification, independent of grade.
    hold_colour_list = db.Column(db.JSON, nullable=True, default=None)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    competitions = db.relationship(
        "Competition",
        back_populates="gym",
        lazy=True,
    )