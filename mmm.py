"""Marketing Mix Model helpers: cleaning, adstock / Hill transforms, fitting, bootstrap, budget optimiser."""
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import differential_evolution, lsq_linear, minimize

DATA = Path(__file__).parent / "data"
CHANNELS = ["tv", "outdoor", "meta", "google_search", "youtube"]
LABELS = {"tv": "TV", "outdoor": "Outdoor", "meta": "Meta (FB/IG)", "google_search": "Google Search",
          "youtube": "YouTube"}
USD_AZN = 1.70
# bounds for the non-linear parameters: geometric decay and Hill half-saturation (in units of mean adstock)
DECAY_BOUNDS = {"tv": (0.0, 0.85), "outdoor": (0.0, 0.85), "meta": (0.0, 0.6),
                "google_search": (0.0, 0.4), "youtube": (0.0, 0.85)}
K_BOUNDS = (0.3, 3.0)
# weakly-informative priors (industry-typical carry-over; saturation centred on the average spend level)
DECAY_PRIOR = {"tv": (0.5, 0.15), "outdoor": (0.4, 0.15), "meta": (0.2, 0.15), "google_search": (0.05, 0.1),
               "youtube": (0.4, 0.15)}
LOGK_PRIOR = (0.0, 0.5)
NOISE_CV = 0.03  # assumed weekly noise (3% of revenue) that scales the likelihood against the priors


def load_raw():
    return pd.read_csv(DATA / "marketing_weekly.csv", parse_dates=["week_start"])


def clean(raw):
    df = raw.copy(); log = {}
    n = len(df); df = df.drop_duplicates(); log["Exact duplicate rows dropped"] = n - len(df)
    usd = df.meta_currency.eq("USD")
    log["Meta weeks billed in USD → converted at 1.70"] = int(usd.sum())
    df.loc[usd, "meta_spend"] = df.loc[usd, "meta_spend"] * USD_AZN
    df["meta_currency"] = "AZN"
    na = df.outdoor_spend.isna(); log["Missing outdoor invoices → filled from the monthly contract"] = int(na.sum())
    month = df.week_start.dt.to_period("M")
    df["outdoor_spend"] = df.groupby(month).outdoor_spend.transform(lambda s: s.fillna(s.median()))
    df = df.sort_values("week_start").reset_index(drop=True)
    return df, log


def adstock(x, decay):
    y = np.empty_like(x, dtype=float); c = 0.0
    for i, v in enumerate(x):
        c = v + decay * c; y[i] = c
    return y


def hill(a, K, s=1.0):
    return a ** s / (a ** s + K ** s)


def controls(df):
    t = np.arange(len(df)); woy = df.week_start.dt.isocalendar().week.to_numpy().astype(float)
    X = pd.DataFrame({
        "intercept": 1.0, "trend": t / 52,
        "sin1": np.sin(2 * np.pi * woy / 52), "cos1": np.cos(2 * np.pi * woy / 52),
        "sin2": np.sin(4 * np.pi * woy / 52), "cos2": np.cos(4 * np.pi * woy / 52),
        "promo_discount_pct": df.promo_discount_pct, "competitor_promo": df.competitor_promo,
        "novruz_week": df.novruz_week, "black_friday_week": df.black_friday_week, "new_year_weeks": df.new_year_weeks,
    }, index=df.index)
    return X


class MMM:
    """Revenue = controls·γ + Σ β_c · Hill(adstock(spend_c, θ_c) / scale_c, K_c), with β_c ≥ 0.

    Non-linear parameters (θ, K) are found with differential evolution on a MAP objective (Gaussian
    likelihood + weakly-informative priors on θ and log K); for each candidate the linear coefficients
    are solved exactly by bounded least squares (variable projection).
    """

    def _media(self, spend, params, scale=None):
        cols, sc = [], {}
        for j, ch in enumerate(CHANNELS):
            d, K = params[2 * j], params[2 * j + 1]
            a = adstock(spend[ch].to_numpy(float), d)
            sc[ch] = a.mean() if scale is None else scale[ch]
            cols.append(hill(a / sc[ch], K))
        return np.column_stack(cols), sc

    def _solve(self, Xc, M, y):
        A = np.hstack([Xc, M]); p = M.shape[1]
        lb = np.r_[np.full(Xc.shape[1], -np.inf), np.zeros(p)]
        res = lsq_linear(A, y, bounds=(lb, np.full(A.shape[1], np.inf)), method="bvls")
        return res.x

    def _loss(self, params, Xc, spend, y):
        M, _ = self._media(spend, params)
        coef = self._solve(Xc, M, y)
        r = y - np.hstack([Xc, M]) @ coef
        sigma = NOISE_CV * y.mean()
        pen = sum(((params[2 * j] - DECAY_PRIOR[ch][0]) / DECAY_PRIOR[ch][1]) ** 2
                  + ((np.log(params[2 * j + 1]) - LOGK_PRIOR[0]) / LOGK_PRIOR[1]) ** 2 for j, ch in enumerate(CHANNELS))
        return float(r @ r / sigma ** 2 + pen)

    def fit(self, df, seed=0, maxiter=120):
        self.Xcols = controls(df).columns
        Xc = controls(df).to_numpy(float); y = df.revenue_azn_k.to_numpy(float); spend = df[[f"{c}_spend" for c in CHANNELS]]
        spend.columns = CHANNELS
        bounds = []
        for ch in CHANNELS:
            bounds += [DECAY_BOUNDS[ch], K_BOUNDS]
        res = differential_evolution(self._loss, bounds, args=(Xc, spend, y), seed=seed, maxiter=maxiter,
                                     popsize=12, tol=1e-7, polish=True, updating="deferred", workers=1)
        self.params = res.x
        M, self.scale = self._media(spend, self.params)
        self.coef = self._solve(Xc, M, y)
        self.gamma = pd.Series(self.coef[:Xc.shape[1]], index=self.Xcols)
        self.beta = pd.Series(self.coef[Xc.shape[1]:], index=CHANNELS)
        self.decay = pd.Series(self.params[0::2], index=CHANNELS)
        self.K = pd.Series(self.params[1::2], index=CHANNELS)
        self.train_df = df
        return self

    def _spend(self, df):
        s = df[[f"{c}_spend" for c in CHANNELS]].copy(); s.columns = CHANNELS
        return s

    def contributions(self, df, spend=None):
        spend = self._spend(df) if spend is None else spend
        M, _ = self._media(spend, self.params, self.scale)
        out = pd.DataFrame(M * self.beta.values, columns=CHANNELS, index=df.index)
        out.insert(0, "base", controls(df).to_numpy(float) @ self.gamma.values)
        return out

    def predict(self, df, spend=None):
        return self.contributions(df, spend).sum(axis=1)

    # ---------- response curves & optimisation ----------
    def channel_revenue(self, ch, spend_series):
        a = adstock(np.asarray(spend_series, float), self.decay[ch]) / self.scale[ch]
        return self.beta[ch] * hill(a, self.K[ch])

    def period_response(self, df, multipliers):
        """Total media revenue over df's weeks when each channel's historic spend pattern is scaled."""
        sp = self._spend(df)
        return sum(self.channel_revenue(ch, sp[ch].to_numpy() * m).sum() for ch, m in zip(CHANNELS, multipliers))

    def marginal_roi(self, df, ch, step=0.01):
        sp = self._spend(df)[ch].to_numpy()
        base = self.channel_revenue(ch, sp).sum(); up = self.channel_revenue(ch, sp * (1 + step)).sum()
        return (up - base) / (sp.sum() * step)

    def optimise(self, df, lo=0.5, hi=1.5, total=None):
        sp = self._spend(df).sum().reindex(CHANNELS).to_numpy()
        total = sp.sum() if total is None else total
        cons = {"type": "eq", "fun": lambda m: (m * sp).sum() - total}
        bounds = [(lo, hi) if s > 0 else (1, 1) for s in sp]
        res = minimize(lambda m: -self.period_response(df, m), x0=np.full(len(CHANNELS), total / sp.sum()),
                       bounds=bounds, constraints=cons, method="SLSQP", options={"maxiter": 300, "ftol": 1e-9})
        return pd.Series(res.x, index=CHANNELS), -res.fun


def block_bootstrap_roi(model, df, n_boot=300, block=8, seed=1):
    """Moving-block bootstrap of residuals with the non-linear parameters held fixed → ROI intervals."""
    rng = np.random.default_rng(seed)
    Xc = controls(df).to_numpy(float); spend = model._spend(df)
    M, _ = model._media(spend, model.params, model.scale)
    fitted = np.hstack([Xc, M]) @ model.coef; resid = df.revenue_azn_k.to_numpy() - fitted
    n = len(resid); tot_spend = spend.sum().reindex(CHANNELS).to_numpy()
    rois = []
    for _ in range(n_boot):
        starts = rng.integers(0, n - block + 1, size=int(np.ceil(n / block)))
        r = np.concatenate([resid[s:s + block] for s in starts])[:n]
        coef = model._solve(Xc, M, fitted + r)
        beta = coef[Xc.shape[1]:]
        rois.append((M * beta).sum(axis=0) / tot_spend)
    return pd.DataFrame(rois, columns=CHANNELS)


def metrics(y, yhat):
    y, yhat = np.asarray(y), np.asarray(yhat)
    return {"MAPE": float(np.mean(np.abs(y - yhat) / y)), "R2": float(1 - ((y - yhat) ** 2).sum() / ((y - y.mean()) ** 2).sum()),
            "Bias": float((yhat.sum() - y.sum()) / y.sum())}
