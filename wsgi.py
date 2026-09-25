"""WSGI entry point for Gunicorn/Render."""
from app import app

application = app
