"""
Estimo Compatibility Shim: pipeline_app/app.py
=============================================
This file ensures backwards compatibility for existing workflows and scripts
that reference 'pipeline_app/app.py'. All execution is forwarded to the
modular 'estimo' package.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from estimo.app import (
    app,
    run_gemini_agent,
    run_hardware_agent,
    run_software_agent,
    run_final_prediction,
    predict_ucp_effort,
)
from estimo.config import PORT

if __name__ == "__main__":
    port = int(os.environ.get("PORT", str(PORT)))
    print(f"Starting Estimo via pipeline_app shim on http://127.0.0.1:{port} ...")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True, use_reloader=False)
