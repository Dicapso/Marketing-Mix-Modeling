"""Synthetic weekly marketing data for a Baku online electronics retailer (seed 42).

Revenue is generated from a known structural model (base + trend + seasonality + holidays
+ promotions + media with geometric adstock and Hill saturation + noise), so the MMM in the
notebook can be checked against the ground truth saved in data/ground_truth.json.
A few realistic data issues are injected on purpose (duplicate rows, a currency mix-up,
missing outdoor invoices).
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

rng = np.random.default_rng(42)
OUT = Path(__file__).parent
N = 156
weeks = pd.date_range("2023-10-16", periods=N, freq="W-MON")
t = np.arange(N)
woy = weeks.isocalendar().week.to_numpy().astype(float)

# ---------------- media spend (AZN thousand per week) ----------------
def flights(on_len, off_len, level, jitter, boost_months=()):
    x = np.zeros(N); i = int(rng.integers(0, 4))
    while i < N:
        L = int(rng.integers(*on_len))
        lvl = level * rng.uniform(0.8, 1.2)
        x[i:i + L] = lvl * rng.uniform(1 - jitter, 1 + jitter, size=len(x[i:i + L]))
        i += L + int(rng.integers(*off_len))
    m = weeks.month.to_numpy()
    for bm in boost_months:
        x[(m == bm) & (x > 0)] *= 1.35
    return x

tv = flights((3, 6), (2, 5), 70, 0.10, boost_months=(3, 11, 12))
months = weeks.to_period("M")
ooh_month = {p: (0.0 if rng.random() < 0.3 else rng.uniform(8, 30)) for p in months.unique()}
ooh = np.array([ooh_month[p] for p in months])
season = 1 + 0.18 * np.sin(2 * np.pi * (woy - 10) / 52) + 0.30 * np.isin(weeks.month, [11, 12])
meta = np.clip(26 * season * rng.uniform(0.55, 1.45, N) + rng.normal(0, 2, N), 8, None)
meta[60:64] = rng.uniform(0, 1.5, 4)          # 4-week "go-dark" incrementality test (Dec 2024)
meta[100:104] *= 2.0                          # 4-week heavy-up test (Sep 2025)
search = np.clip(30 * season ** 0.8 * rng.uniform(0.6, 1.4, N), 10, None)
youtube = np.where(rng.random(N) < 0.6, rng.uniform(8, 24, N), 0.0)
youtube[(t > 20) & (t < 28)] = 0  # one long pause

spend = {"tv": tv, "outdoor": ooh, "meta": meta, "google_search": search, "youtube": youtube}

# ---------------- true media model ----------------
TRUTH = {  # decay, K (half-saturation in units of mean adstock), shape s, beta (max weekly revenue, AZN k)
    "tv":            (0.60, 1.10, 1.6, 460),
    "outdoor":       (0.40, 0.90, 1.0, 60),
    "meta":          (0.25, 0.90, 1.0, 225),
    "google_search": (0.05, 0.45, 1.0, 170),
    "youtube":       (0.45, 2.20, 1.0, 265),
}

def adstock(x, d):
    y = np.zeros_like(x); c = 0.0
    for i, v in enumerate(x):
        c = v + d * c; y[i] = c
    return y

contrib = {}
for ch, (d, K, s, beta) in TRUTH.items():
    a = adstock(spend[ch], d); a = a / a.mean()
    contrib[ch] = beta * a ** s / (a ** s + K ** s)

# ---------------- base demand ----------------
def week_has(month, day):
    return np.array([any((w + pd.Timedelta(days=k)).month == month and (w + pd.Timedelta(days=k)).day == day
                         for k in range(7)) for w in weeks])

novruz = week_has(3, 20).astype(float)
black_friday = np.zeros(N)
for y in (2023, 2024, 2025):
    nov = pd.date_range(f"{y}-11-01", f"{y}-11-30", freq="D"); bf = nov[nov.weekday == 4][-1]
    black_friday[np.argmin(np.abs((weeks + pd.Timedelta(days=3)) - bf))] = 1
new_year = np.zeros(N)
for i, w in enumerate(weeks):
    if (w.month == 12 and w.day >= 17) or (w.month == 12 and w.day >= 10 and w.day < 17):
        new_year[i] = 1
promo_depth = np.where(rng.random(N) < 0.25, rng.choice([5, 10, 15, 20], N), 0).astype(float)
promo_depth[black_friday == 1] = 25
competitor_promo = (rng.random(N) < 0.15).astype(float)

base = (820 + 1.6 * t + 70 * np.sin(2 * np.pi * (woy - 10) / 52) + 35 * np.cos(4 * np.pi * woy / 52)
        + 140 * novruz + 420 * black_friday + 260 * new_year + 11 * promo_depth - 65 * competitor_promo)
media_total = sum(contrib.values())
revenue = (base + media_total) * rng.normal(1, 0.025, N)

# platform-reported (last-click / in-platform) attributed revenue: biased on purpose
meta_reported = contrib["meta"] * 1.7 * rng.normal(1, 0.08, N)
search_reported = (contrib["google_search"] + 0.35 * contrib["tv"] + 0.25 * contrib["youtube"] + 0.06 * base) \
                  * rng.normal(1, 0.06, N)
yt_reported = contrib["youtube"] * 0.35 * rng.normal(1, 0.15, N)

df = pd.DataFrame({
    "week_start": weeks.date, "revenue_azn_k": revenue.round(2),
    "orders": np.round(revenue * 1000 / rng.normal(310, 8, N)).astype(int),
    "tv_spend": tv.round(2), "outdoor_spend": ooh.round(2), "meta_spend": meta.round(2),
    "meta_currency": "AZN", "google_search_spend": search.round(2), "youtube_spend": youtube.round(2),
    "meta_reported_revenue": meta_reported.round(2), "google_reported_revenue": search_reported.round(2),
    "youtube_reported_revenue": yt_reported.round(2),
    "promo_discount_pct": promo_depth, "competitor_promo": competitor_promo.astype(int),
    "novruz_week": novruz.astype(int), "black_friday_week": black_friday.astype(int),
    "new_year_weeks": new_year.astype(int),
})

# ---------------- inject data issues ----------------
usd_rows = np.arange(40, 46)                               # Meta billing exported in USD for 6 weeks
df.loc[usd_rows, "meta_spend"] = (df.loc[usd_rows, "meta_spend"] / 1.70).round(2)
df.loc[usd_rows, "meta_currency"] = "USD"
miss = [70, 71, 118]                                       # outdoor invoices not uploaded
df.loc[miss, "outdoor_spend"] = np.nan
df = pd.concat([df, df.iloc[[95, 96]]]).sort_values("week_start", kind="stable").reset_index(drop=True)
df.to_csv(OUT / "marketing_weekly.csv", index=False)

# ---------------- ground truth ----------------
gt = {}
for ch in TRUTH:
    d, K, s, beta = TRUTH[ch]
    gt[ch] = {"decay": d, "half_saturation": K, "shape": s, "beta": beta,
              "total_spend": float(spend[ch].sum()), "total_contribution": float(contrib[ch].sum()),
              "roi": float(contrib[ch].sum() / spend[ch].sum())}
gt["_media_share_of_revenue"] = float(media_total.sum() / revenue.sum())
(OUT / "ground_truth.json").write_text(json.dumps(gt, indent=2))
print(df.shape)
print(json.dumps({k: round(v["roi"], 2) for k, v in gt.items() if not k.startswith("_")}),
      round(gt["_media_share_of_revenue"], 3))
print("avg weekly spend", {k: round(v.mean(), 1) for k, v in spend.items()}, "revenue", round(revenue.mean(), 1))
