from .auth import auth_bp
from .competitions import comp_bp
from .login import login_bp
from .competitors import competitors_bp

def register_blueprints(app):
    app.register_blueprint(auth_bp)
    app.register_blueprint(comp_bp)
    app.register_blueprint(login_bp)
    app.register_blueprint(competitors_bp)
