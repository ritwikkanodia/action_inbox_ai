"""Deny-by-default cloud factory, deliberately separate from the SQLite app."""
from pathlib import Path
from flask import Flask
from cloud.config import DEFAULTS
from cloud.http import register_routes


def create_app(db, principal=None, *, testing=False):
    if principal is not None and testing is not True:
        raise ValueError('production_authentication_not_integrated')
    root = Path(__file__).resolve().parents[1]
    app = Flask(__name__, template_folder=str(root / 'templates'), static_folder=str(root / 'static'))
    app.config.update(TESTING=testing is True, MAX_CONTENT_LENGTH=DEFAULTS.body_bytes)
    register_routes(app, db, principal)
    return app
