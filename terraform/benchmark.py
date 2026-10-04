"""CPU LightGBM benchmark for README_aws.md section 4.4.

Run on the private EC2 node:
    python benchmark.py --data ~/ml-benchmark/creditcard.csv
"""

import argparse
import json
import platform
from pathlib import Path
from time import perf_counter

import lightgbm as lgb
import numpy as np
import pandas as pd
import sklearn
from sklearn.metrics import (
    accuracy_score, f1_score, precision_score, recall_score, roc_auc_score,
)
from sklearn.model_selection import train_test_split


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("creditcard.csv"))
    parser.add_argument("--output", type=Path, default=Path("benchmark_result.json"))
    parser.add_argument("--threads", type=int, default=2)
    args = parser.parse_args()
    if args.threads < 1:
        parser.error("--threads must be positive")
    data_path = args.data.expanduser()
    if not data_path.is_file():
        parser.error("Dataset not found: {}. Pass --data /path/to/creditcard.csv".format(data_path))

    start = perf_counter()
    frame = pd.read_csv(data_path)
    load_seconds = perf_counter() - start
    if "Class" not in frame or set(frame["Class"].unique()) != {0, 1}:
        parser.error("Dataset must contain a binary Class column with both 0 and 1")
    if frame.shape[1] < 2 or not all(pd.api.types.is_numeric_dtype(t) for t in frame.dtypes):
        parser.error("Dataset must contain numeric features")
    if not np.isfinite(frame.to_numpy()).all():
        parser.error("Dataset contains missing or infinite values")

    features = frame.drop(columns="Class")
    target = frame["Class"]
    # Keep the test set untouched by early stopping: 60% train, 20% validation, 20% test.
    try:
        x_trainval, x_test, y_trainval, y_test = train_test_split(
            features, target, test_size=0.2, stratify=target, random_state=42,
        )
        x_train, x_valid, y_train, y_valid = train_test_split(
            x_trainval, y_trainval, test_size=0.25, stratify=y_trainval, random_state=42,
        )
    except ValueError as exc:
        parser.error("Cannot create stratified splits: {}".format(exc))
    if any(y.nunique() != 2 for y in (y_train, y_valid, y_test)):
        parser.error("Each split needs both classes; provide more examples")
    if len(x_test) < 1000:
        parser.error("Test set needs at least 1000 rows for the inference benchmark")

    model = lgb.LGBMClassifier(
        objective="binary", metric="auc", n_estimators=1000,
        learning_rate=0.05, num_leaves=31, random_state=42,
        n_jobs=args.threads, verbosity=-1,
    )
    print("Rows: train={}, validation={}, test={}".format(len(x_train), len(x_valid), len(x_test)))
    start = perf_counter()
    model.fit(
        x_train, y_train, eval_set=[(x_valid, y_valid)], eval_metric="auc",
        callbacks=[lgb.early_stopping(50, first_metric_only=True, verbose=False)],
    )
    training_seconds = perf_counter() - start
    probabilities = model.predict_proba(x_test)[:, 1]
    predictions = (probabilities >= 0.5).astype(int)

    # Warm up; measure predict_proba only, excluding data selection and CSV I/O.
    single = x_test.iloc[:1]
    batch = x_test.iloc[:1000]
    model.predict_proba(single)
    model.predict_proba(batch)
    single_times = []
    for _ in range(100):
        start = perf_counter()
        model.predict_proba(single)
        single_times.append(perf_counter() - start)
    batch_times = []
    for _ in range(20):
        start = perf_counter()
        model.predict_proba(batch)
        batch_times.append(perf_counter() - start)

    results = {
        "dataset": str(data_path.resolve()),
        "rows": len(frame), "features": len(features.columns),
        "train_rows": len(x_train), "validation_rows": len(x_valid), "test_rows": len(x_test),
        "seed": 42, "threads": args.threads, "classification_threshold": 0.5,
        "load_time_seconds": load_seconds,
        "training_time_seconds": training_seconds,
        "best_iteration": int(model.best_iteration_),
        "auc_roc": float(roc_auc_score(y_test, probabilities)),
        "accuracy": float(accuracy_score(y_test, predictions)),
        "f1_score": float(f1_score(y_test, predictions, zero_division=0)),
        "precision": float(precision_score(y_test, predictions, zero_division=0)),
        "recall": float(recall_score(y_test, predictions, zero_division=0)),
        "inference_latency_ms": float(np.mean(single_times) * 1000),
        "inference_latency_p95_ms": float(np.percentile(single_times, 95) * 1000),
        "inference_batch_size": 1000,
        "inference_batch_time_seconds": float(np.mean(batch_times)),
        "inference_throughput_rows_per_second": float(1000 / np.mean(batch_times)),
        "latency_repetitions": 100, "throughput_repetitions": 20,
        "environment": {
            "platform": platform.platform(), "python": platform.python_version(),
            "lightgbm": lgb.__version__, "scikit_learn": sklearn.__version__,
            "pandas": pd.__version__, "numpy": np.__version__,
        },
    }
    output = args.output.expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(results, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps(results, indent=2, allow_nan=False))
    print("Saved results to {}".format(output.resolve()))


if __name__ == "__main__":
    main()
