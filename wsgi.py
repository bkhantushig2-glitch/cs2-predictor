"""WSGI entry point for production servers (gunicorn, Render, Railway).

Adds the project root to sys.path so `webapp.app` can resolve sibling modules
(`advanced_predictor`, `prop_odds`, etc.) the same way `python3 webapp/app.py`
would locally.

Start command:
    gunicorn --bind 0.0.0.0:$PORT wsgi:app
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, 'webapp'))

from webapp.app import app  # noqa: E402

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
