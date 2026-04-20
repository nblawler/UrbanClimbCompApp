from datetime import datetime
from app.extensions import db

# Valid grading systems — kept as a module-level tuple so other parts of the
# codebase can import and reference it without repeating the string literals.
GRADING_SYSTEMS = ("colour", "v_grade", "numeric")

GRADING_SYSTEM_LABELS = {
    "colour":  "Colour",
    "v_grade": "V Grade",
    "numeric": "Numeric (1–9)",
}


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

    # How grades are expressed at this gym.
    # Set once by a gym admin via Gym Settings — does not change per competition.
    # nullable=True until the admin sets it (existing gyms populated via data migration).
    grading_system = db.Column(
        db.Enum(*GRADING_SYSTEMS, name="grading_system_enum", native_enum=False),
        nullable=True,
        default=None,
    )

    # Ordered list of grades for colour-graded gyms.
    # Each entry: {"label": "Blue", "colour": "#1e90ff"}
    # Ordered easiest → hardest. Used to populate grade dropdowns in the route setter.
    # Null for v_grade and numeric gyms (their lists are fixed and built into the UI).
    grade_list = db.Column(db.JSON, nullable=True, default=None)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    competitions = db.relationship(
        "Competition",
        back_populates="gym",
        lazy=True,
    )