import os
from pathlib import Path

# Paths
ROOT_DIR = Path(__file__).resolve().parent.parent
ESTIMO_DIR = ROOT_DIR / "estimo"
APP_DIR = ESTIMO_DIR
DATA_DIR = ESTIMO_DIR / "data"
ML_DIR = ROOT_DIR / "ml"
MODELS_DIR = ML_DIR / "models"
CACHE_DIR = ROOT_DIR / ".image_cache"
CACHE_DIR.mkdir(exist_ok=True)

# Load root .env if present
_env_file = ROOT_DIR / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

# Gemini Settings
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()

# NVIDIA Settings
def get_nvidia_api_key() -> str:
    # 1. Environment variable
    env_key = os.environ.get("NVIDIA_API_KEY", "").strip()
    if env_key and env_key.startswith("nvapi-"):
        return env_key

    # 2. Key files in estimo/key.txt or Nvidea/key.txt
    candidates = [
        ESTIMO_DIR / "key.txt",
        ROOT_DIR / "Nvidea" / "key.txt",
    ]
    for p in candidates:
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("nvapi-"):
                    return line
    return ""

NEMOTRON_MODEL = os.environ.get("NEMOTRON_MODEL", "nvidia/nemotron-3-ultra-550b-a55b").strip()
PORT = int(os.environ.get("PORT", "5002"))
