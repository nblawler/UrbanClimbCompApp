from app import create_app
from app.extensions import db
from app.routes import register_blueprints
from dotenv import load_dotenv

load_dotenv()

api = create_app()

# Register all Blueprints (auth, competitions, etc.)
register_blueprints(api)

def init_db():
    """Ensure DB tables exist."""
    db.create_all()

# Run DB bootstrap once at startup
with api.app_context():
    init_db()

if __name__ == "__main__":
    api.run(debug=True)
