from .auth import auth_bp
from .competitions import comp_bp
from .login import login_bp
from .competitors import competitors_bp
from .admin import admin_bp
from .api import api_bp

def register_blueprints(app):
    app.register_blueprint(auth_bp)
    app.register_blueprint(comp_bp)
    app.register_blueprint(login_bp)
    app.register_blueprint(competitors_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(api_bp)