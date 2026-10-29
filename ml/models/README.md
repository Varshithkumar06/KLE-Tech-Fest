# Models Directory

Approved and experimental models are serialized to disk under:
`models/<target>/<version>/`

### Structure of an Approved Model Bundle:
- `model.joblib`: Complete fitted scikit-learn / XGBoost pipeline (preprocessor + estimator)
- `preprocessing_report.json`: Encoding parameters, imputation values, scaler params
- `metadata.json`: Model architecture, hyper-parameters, training timestamp, runtime environment
- `evaluation.json`: Held-out validation metrics (MAE, RMSE, MMRE, MdMRE, PRED(25), Bias)
- `feature_schema.json`: Expected input features with types and units
- `dataset_manifest.json`: Reference to the training dataset manifest and raw file SHA-256

### Security Rule (R6):
- All models are verified against SHA-256 checksums recorded in `models/registry.json`.
- Arbitrary pickle/joblib files outside this signed structure are NEVER loaded.
- Initial state is EMPTY.
