"""Example plugin: gradient-boosted trees (scikit-learn) with walk-forward training.

Install scikit-learn into the same environment as the app:  pip install scikit-learn
Copy this file into your plugins folder (shown on Strategies -> Plugins), then press
"Reload plugins". It appears as "ML: gradient boosting (sklearn)".
"""

import pandas as pd

from alpaca_manager.strategies import ModelStrategy, Param, default_features


class SklearnBoosting(ModelStrategy):
    key = "ml_sklearn_boosting"
    name = "ML: gradient boosting (sklearn)"
    description = ("Histogram gradient-boosted trees predicting each symbol's return over the next "
                   "few bars. Retrains walk-forward; holds the best positive forecasts.")
    params = {
        **ModelStrategy.params,
        "max_depth": Param(3, min=1, max=12, help="Tree depth"),
        "learning_rate": Param(0.05, min=0.001, max=1.0, step=0.01, help="Boosting learning rate"),
        "max_iter": Param(150, min=10, max=2000, help="Boosting rounds"),
    }

    def make_model(self):
        # Imported here so the app still starts if scikit-learn isn't installed;
        # the error then shows only when this strategy is used.
        from sklearn.ensemble import HistGradientBoostingRegressor

        return HistGradientBoostingRegressor(
            max_depth=self.p["max_depth"], learning_rate=self.p["learning_rate"],
            max_iter=self.p["max_iter"], random_state=0)

    def features(self, df: pd.DataFrame) -> pd.DataFrame:
        # Start from the built-in features and add your own. Every column must use
        # only the current bar and earlier ones (rolling / shift(+n) / pct_change).
        f = default_features(df)
        f["ret_60"] = df["close"].pct_change(60)
        f["dist_high_20"] = df["close"] / df["high"].rolling(20).max() - 1
        return f
