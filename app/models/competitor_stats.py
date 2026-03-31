from datetime import datetime
from app.extensions import db


class CompetitorStats(db.Model):
    __tablename__ = "competitor_stats"

    id = db.Column(db.Integer, primary_key=True)

    account_id = db.Column(
        db.Integer,
        db.ForeignKey("account.id"),
        nullable=False,
        unique=True,
        index=True,
    )
    account = db.relationship("Account", backref=db.backref("stats", uselist=False))

    # Best finishing position ever (raw int, e.g. 1 = first place)
    best_place = db.Column(db.Integer, nullable=True)

    # Total competitions entered across all competitor rows for this account
    total_comps = db.Column(db.Integer, nullable=False, default=0)

    # Medal counts
    medals_gold = db.Column(db.Integer, nullable=False, default=0)      # 1st place finishes
    medals_silver = db.Column(db.Integer, nullable=False, default=0)    # 2nd place finishes
    medals_bronze = db.Column(db.Integer, nullable=False, default=0)    # 3rd place finishes
    medals_finalist = db.Column(db.Integer, nullable=False, default=0)  # Finalist (threshold TBD)

    # Milestone medals — stored as booleans since they're one-time unlocks
    milestone_10 = db.Column(db.Boolean, nullable=False, default=False)
    milestone_25 = db.Column(db.Boolean, nullable=False, default=False)
    milestone_50 = db.Column(db.Boolean, nullable=False, default=False)

    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    def __repr__(self):
        return f"<CompetitorStats account_id={self.account_id} best_place={self.best_place}>"