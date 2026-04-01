"""Train logistic regression on matchup features; save with joblib."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import joblib
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, brier_score_loss, log_loss
from sklearn.model_selection import train_test_split

from mma_model.predict.dataset import FEATURE_NAMES, build_training_arrays


def train_and_save(
    session: Any,
    model_path: Path,
    *,
    min_prior_fights: int = 1,
    last_n: int = 5,
    test_size: float = 0.25,
    random_state: int = 42,
) -> dict[str, float | int | str]:
    X, y, fight_ids = build_training_arrays(
        session, min_prior_fights=min_prior_fights, last_n=last_n
    )
    n = X.shape[0]
    if n < 8:
        raise ValueError(
            f"Need at least 8 labeled fights with stats; got {n}. Run sync with fight details."
        )

    strat = y if len(np.unique(y)) > 1 else None
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=test_size, random_state=random_state, stratify=strat
    )
    model = LogisticRegression(max_iter=500)
    model.fit(X_train, y_train)
    proba = model.predict_proba(X_test)[:, 1]
    acc = float(accuracy_score(y_test, model.predict(X_test)))
    ll = float(log_loss(y_test, proba))
    br = float(brier_score_loss(y_test, proba))

    model_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "model": model,
        "feature_names": list(FEATURE_NAMES),
        "n_samples": n,
        "fight_ids": fight_ids,
    }
    joblib.dump(payload, model_path)

    return {
        "n_samples": n,
        "accuracy_holdout": acc,
        "log_loss_holdout": ll,
        "brier_holdout": br,
        "model_path": str(model_path.resolve()),
    }


def load_model(model_path: Path):
    payload = joblib.load(model_path)
    return payload["model"], list(payload["feature_names"])


def predict_fight_a_win_prob(session: Any, fight_id: str, model_path: Path, *, last_n: int = 5) -> float:
    from mma_model.db.models import Fight
    from mma_model.composites.rolling import rolling_profile_before_fight
    from mma_model.features.matchup import matchup_features

    model, _names = load_model(model_path)
    fight = session.get(Fight, fight_id)
    if fight is None:
        raise ValueError(f"Unknown fight_id: {fight_id}")
    ra = rolling_profile_before_fight(session, fight.fighter_a_id, fight_id, last_n=last_n)
    rb = rolling_profile_before_fight(session, fight.fighter_b_id, fight_id, last_n=last_n)
    if ra is None or rb is None:
        raise ValueError("Could not build rolling profiles (missing event date?)")
    m = matchup_features(ra, rb)
    X = np.array(
        [[m.diff_sig_pm, m.diff_acc, m.diff_td_15, m.diff_sub_15, m.diff_ctrl_pm]],
        dtype=np.float64,
    )
    return float(model.predict_proba(X)[0, 1])
