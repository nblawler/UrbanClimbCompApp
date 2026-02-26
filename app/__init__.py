from flask import Flask
from .config import Config
from .extensions import db
from app.helpers.time import utc_to_melbourne


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)

    app.jinja_env.globals["utc_to_melbourne"] = utc_to_melbourne

    # Jinja filter used by templates: {{ dt|melb_dt('%d %b %Y...') }}
    @app.template_filter("melb_dt")
    def melb_dt(dt, fmt: str = "%d %b %Y, %I:%M %p") -> str:
        dt_melb = utc_to_melbourne(dt)
        return dt_melb.strftime(fmt) if dt_melb else ""

    return app
