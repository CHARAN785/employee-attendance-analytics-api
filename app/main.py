"""Expose the backend API at the assignment-required ``app.main:app`` path."""

from backend.app.main import app

__all__ = ["app"]
