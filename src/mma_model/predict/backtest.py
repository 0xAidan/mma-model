"""Walk-forward backtest: train only on past fights, predict the next (no lookahead)."""

from __future__ import annotations

from typing import Any

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, brier_score_loss, log_loss
from sqlalchemy.orm import Session

from mma_model.db.models import Fight
from mma_model.predict.dataset import (
    build_training_arrays,
    build_training_arrays_for_fight_ids,
    feature_row_and_label_for_fight,
)


def walk_forward_backtest(
    session: Session,
    *,
    min_train_fights: int = 30,
    min_prior_fights: int = 1,
    last_n: int = 5,
    max_predictions: int | None = None,
    random_state: int = 42,
) -> dict[str, Any]:
    """
    Chronological expanding window: for each fight i >= min_train_fights, fit on fights 0..i-1,
    predict fight i. Features use only stats from bouts strictly before that fight (rolling_profile).
    """
    _, _, ordered_ids = build_training_arrays(
        session, min_prior_fights=min_prior_fights, last_n=last_n
    )
    n = len(ordered_ids)
    if n <= min_train_fights:
        raise ValueError(
            f"Need more eligible fights than min_train_fights={min_train_fights}; "
            f"got {n} chronological samples. Increase sync or lower min_prior_fights."
        )

    per_fight: list[dict[str, Any]] = []
    y_true: list[int] = []
    y_prob: list[float] = []

    for i in range(min_train_fights, n):
        train_ids = ordered_ids[:i]
        test_id = ordered_ids[i]
        X_train, y_train = build_training_arrays_for_fight_ids(
            session, train_ids, min_prior_fights=min_prior_fights, last_n=last_n
        )
        if X_train.shape[0] < 4:
            continue
        if len(np.unique(y_train)) < 2:
            continue

        test_fight = session.get(Fight, test_id)
        if test_fight is None:
            continue
        test_got = feature_row_and_label_for_fight(
            session, test_fight, min_prior_fights=min_prior_fights, last_n=last_n
        )
        if test_got is None:
            continue
        x_test, y_test = test_got

        model = LogisticRegression(max_iter=500, random_state=random_state)
        model.fit(X_train, y_train)
        p = float(model.predict_proba(x_test.reshape(1, -1))[0, 1])

        per_fight.append(
            {
                "fight_id": test_id,
                "y_fighter_a_won": int(y_test),
                "p_fighter_a": p,
            }
        )
        y_true.append(int(y_test))
        y_prob.append(p)

        if max_predictions is not None and len(per_fight) >= max_predictions:
            break

    if not y_true:
        raise ValueError("No walk-forward predictions produced (check class balance / min_train).")

    yt = np.asarray(y_true, dtype=np.int64)
    yp = np.asarray(y_prob, dtype=np.float64)
    y_hat = (yp >= 0.5).astype(np.int64)

    return {
        "method": "walk_forward_expanding",
        "min_train_fights": min_train_fights,
        "n_predictions": len(per_fight),
        "accuracy": float(accuracy_score(yt, y_hat)),
        "log_loss": float(log_loss(yt, yp)),
        "brier": float(brier_score_loss(yt, yp)),
        "predictions": per_fight,
    }
