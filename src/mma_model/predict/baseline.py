"""Baseline win-probability model (logistic regression when enough rows exist)."""

from __future__ import annotations

import numpy as np
from sklearn.linear_model import LogisticRegression


def train_logistic(X: np.ndarray, y: np.ndarray) -> LogisticRegression:
    m = LogisticRegression(max_iter=200)
    m.fit(X, y)
    return m
