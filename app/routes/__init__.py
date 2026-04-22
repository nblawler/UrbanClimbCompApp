from .index import index_bp
from .admin import admin_bp
from .auth import auth_bp
from .competitions import competitions_bp
from .competitors import competitors_bp
from .scores import scores_bp
from .climbs import climbs_bp
from app.routes.gym_settings import gym_settings_bp
from app.routes.climb_entry import climb_entry_bp

def register_blueprints(app):
    app.register_blueprint(index_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(competitions_bp)
    app.register_blueprint(competitors_bp)
    app.register_blueprint(scores_bp)
    app.register_blueprint(climbs_bp)
    app.register_blueprint(gym_settings_bp)
    app.register_blueprint(climb_entry_bp)