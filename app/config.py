import os

class Config:
    raw_db_url = os.getenv("DATABASE_URL")

    if raw_db_url:
        if raw_db_url.startswith("postgres://"):
            raw_db_url = raw_db_url.replace("postgres://", "postgresql://", 1)
        SQLALCHEMY_DATABASE_URI = raw_db_url
    else:
        SQLALCHEMY_DATABASE_URI = "sqlite:///scoring.db"

    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SECRET_KEY = os.getenv("SECRET_KEY", "change-me-dev-secret")

RESEND_API_KEY = os.getenv("RESEND_API_KEY", None)
RESEND_FROM_EMAIL = os.getenv("RESEND_FROM_EMAIL", None)
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "letmein123")
