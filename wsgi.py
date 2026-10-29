"""
Estimo WSGI Entrypoint for Gunicorn and Azure App Service
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from estimo.app import app

if __name__ == "__main__":
    from estimo.config import PORT
    import os
    port = int(os.environ.get("PORT", str(PORT)))
    app.run(host="0.0.0.0", port=port)
