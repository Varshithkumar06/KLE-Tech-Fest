from __future__ import annotations

import hashlib
import json
import math
import sys
import types
from functools import lru_cache
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd

from .config import ROOT_DIR, MODELS_DIR

TARGET = "Real_Effort_Person_Hours"
HOURS_PER_PERSON_MONTH = 152
DEFAULT_DELIVERY_MONTHS = 6


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _install_pickle_compatibility() -> None:
    """Provide the small class definition referenced by the saved joblib file."""
    if "ml.pipeline" in sys.modules:
        return
    package = types.ModuleType("ml")
    package.__path__ = []  # mark it as a package for pickle imports
    pipeline = types.ModuleType("ml.pipeline")

    class FullEstimationPipeline:
        def __init__(self, preprocessor: Any, estimator: Any, use_log_target: bool = True) -> None:
            self.preprocessor = preprocessor
            self.estimator = estimator
            self.use_log_target = use_log_target

        def predict(self, X: pd.DataFrame) -> np.ndarray:
            raw = self.estimator.predict(self.preprocessor.transform(X))
            return np.maximum(np.expm1(raw), 0.0) if self.use_log_target else np.maximum(raw, 0.0)

    FullEstimationPipeline.__module__ = "ml.pipeline"
    pipeline.FullEstimationPipeline = FullEstimationPipeline
    package.pipeline = pipeline
    sys.modules["ml"] = package
    sys.modules["ml.pipeline"] = pipeline


def _find_path(rel_path: str) -> Path:
    """Find file in ml/ or fallback to legacy Models/."""
    p_ml = ROOT_DIR / "ml" / rel_path
    if p_ml.exists():
        return p_ml
    p_old = ROOT_DIR / "Models" / rel_path
    return p_old


@lru_cache(maxsize=1)
def _model_details() -> tuple[Any, dict[str, Any], dict[str, Any]]:
    registry_path = MODELS_DIR / "registry.json"
    if not registry_path.exists():
        registry_path = ROOT_DIR / "Models" / "models" / "registry.json"
    
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    version = registry["active_models"].get(TARGET)
    if not version:
        raise RuntimeError(f"No active local model is registered for {TARGET}.")
    entry = registry["models"][f"{TARGET}@{version}"]
    
    model_path = _find_path(entry["model_file_path"])
    if not model_path.exists() or _sha256(model_path) != entry["sha256"]:
        raise RuntimeError("The local model checksum did not match the registry; loading was blocked.")
        
    schema_path = _find_path(entry["feature_schema_file_path"])
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    _install_pickle_compatibility()
    return joblib.load(model_path), entry, schema


def predict_ucp_effort(raw_features: dict[str, Any], team_size: int = 1) -> dict[str, Any]:
    """Return a transparently labelled local-model estimate in person-hours."""
    try:
        model, entry, schema = _model_details()
        features: dict[str, Any] = {}
        missing: list[str] = []
        for name, data_type in schema["features"].items():
            value = raw_features.get(name)
            if value is None or value == "":
                missing.append(name)
                continue
            features[name] = value

        required_numeric = schema.get("numeric_features", [])
        missing_numeric = [name for name in required_numeric if name in missing]
        if missing_numeric:
            raise ValueError("Gemini did not extract required UCP values: " + ", ".join(missing_numeric))
        for name in schema.get("categorical_features", []):
            features.setdefault(name, "Unknown")

        # A row of zeros is a Gemini placeholder, not a meaningful project scope.
        if all(float(features[name]) == 0 for name in required_numeric):
            raise ValueError("The project brief does not contain enough scope information for a local-model prediction.")

        hours = float(model.predict(pd.DataFrame([features]))[0])
        metrics = entry.get("metrics", {})
        mae = float(metrics.get("MAE", 0.0))
        delivery_months = DEFAULT_DELIVERY_MONTHS
        recommended_engineers = max(1, math.ceil(hours / (HOURS_PER_PERSON_MONTH * delivery_months)))
        held_out_accuracy = max(0.0, (1.0 - float(metrics.get("MMRE", 0.0))) * 100)
        return {
            "available": True,
            "label": "Predicted value by locally trained model",
            "target": TARGET,
            "unit": "person-hours",
            "hours_low": round(max(0.0, hours - mae)),
            "hours_likely": round(hours),
            "hours_high": round(hours + mae),
            "months_likely": delivery_months,
            "recommended_engineers": recommended_engineers,
            "delivery_months_assumed": delivery_months,
            "accuracy_percent": round(held_out_accuracy, 1),
            "accuracy_basis": "Approximate held-out accuracy calculated as 100% − MMRE; regression has no single classification accuracy.",
            "model": entry["algorithm"],
            "version": entry["version"],
            "validation_metrics": metrics,
            "input_features": features,
            "note": ("Engineer count is a staffing recommendation derived from predicted effort and a "
                     f"{delivery_months}-month delivery target. Range uses held-out MAE; it is not a guarantee."),
        }
    except Exception as exc:
        return {
            "available": False,
            "label": "Local model unavailable",
            "note": str(exc),
        }
