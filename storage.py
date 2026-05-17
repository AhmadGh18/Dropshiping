"""
SQLite-backed history store for analyzer runs.

Single local file at results/history.db. No server, no API keys, no accounts.

Each analyzer run saves one snapshot (per-video + per-cluster rows) and queries
prior snapshots to compute momentum/trend features for each cluster.
"""

import sqlite3
import time
from pathlib import Path

try:
    from rapidfuzz import fuzz
    FUZZY_OK = True
except ImportError:
    FUZZY_OK = False

DB_PATH = Path("results/history.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_ts      INTEGER NOT NULL,
    target      TEXT,
    mode        TEXT,
    n_videos    INTEGER,
    n_clusters  INTEGER
);
CREATE INDEX IF NOT EXISTS ix_snap_ts ON snapshots(run_ts);

CREATE TABLE IF NOT EXISTS video_obs (
    video_id     TEXT NOT NULL,
    snapshot_id  INTEGER NOT NULL,
    author       TEXT,
    views        INTEGER,
    likes        INTEGER,
    comments     INTEGER,
    shares       INTEGER,
    saves        INTEGER,
    age_days     REAL,
    PRIMARY KEY (video_id, snapshot_id),
    FOREIGN KEY (snapshot_id) REFERENCES snapshots(snapshot_id)
);
CREATE INDEX IF NOT EXISTS ix_vid_video ON video_obs(video_id);

CREATE TABLE IF NOT EXISTS product_obs (
    product_key   TEXT NOT NULL,
    snapshot_id   INTEGER NOT NULL,
    tier          TEXT,
    n_creators    INTEGER,
    n_videos      INTEGER,
    total_views   INTEGER,
    total_saves   INTEGER,
    buy_intent    INTEGER,
    pos_sentiment INTEGER,
    neg_sentiment INTEGER,
    score         REAL,
    verdict       TEXT,
    PRIMARY KEY (product_key, snapshot_id),
    FOREIGN KEY (snapshot_id) REFERENCES snapshots(snapshot_id)
);
CREATE INDEX IF NOT EXISTS ix_prod_key ON product_obs(product_key);
"""


# Multiplier applied to the base score after momentum classification.
# 'compounding' is the strongest buy signal; 'declining' the strongest sell.
MOMENTUM_MULTIPLIER = {
    "first_seen":         0.95,   # mild uncertainty discount (no history)
    "early_acceleration": 1.25,   # new and growing fast - get in
    "compounding":        1.40,   # multi-dimensional growth - confirmed trend
    "expanding":          1.10,   # slow steady growth
    "mature_steady":      0.90,   # flat - possible evergreen
    "declining":          0.50,   # falling - skip
    "noisy":              1.00,   # not enough signal to adjust
}


def connect(db_path=DB_PATH):
    db_path = Path(db_path)
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)
    return conn


def save_snapshot(conn, target, mode, enriched_videos, scored_clusters, run_ts=None):
    """Persist one analyzer run. Returns the new snapshot_id."""
    run_ts = run_ts or int(time.time())
    cur = conn.execute(
        "INSERT INTO snapshots (run_ts, target, mode, n_videos, n_clusters) "
        "VALUES (?,?,?,?,?)",
        (run_ts, target, mode, len(enriched_videos), len(scored_clusters)),
    )
    sid = cur.lastrowid

    video_rows = []
    for v in enriched_videos:
        vid = v.get("video_id") or ""
        if not vid:
            continue
        age = v.get("age_days")
        age = age if isinstance(age, (int, float)) else None
        video_rows.append((
            vid, sid,
            v.get("author_username", ""),
            v.get("views", 0), v.get("likes", 0),
            v.get("comments", 0), v.get("shares", 0),
            v.get("saves", 0),
            age,
        ))
    if video_rows:
        conn.executemany(
            "INSERT OR REPLACE INTO video_obs "
            "(video_id, snapshot_id, author, views, likes, comments, shares, saves, age_days) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            video_rows,
        )

    cluster_rows = []
    for c in scored_clusters:
        # Prefer the historical key if we matched against an existing one
        key = c.get("historical_product_key") or c.get("signal", "")
        cluster_rows.append((
            key, sid,
            c.get("tier", ""),
            c.get("n_creators", 0), c.get("n_videos", 0),
            c.get("total_views", 0), c.get("total_saves", 0),
            c.get("total_buy_intent", 0),
            c.get("total_positive_sentiment", 0),
            c.get("total_negative_sentiment", 0),
            c.get("score", 0), c.get("verdict", ""),
        ))
    if cluster_rows:
        conn.executemany(
            "INSERT OR REPLACE INTO product_obs "
            "(product_key, snapshot_id, tier, n_creators, n_videos, total_views, "
            " total_saves, buy_intent, pos_sentiment, neg_sentiment, score, verdict) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            cluster_rows,
        )

    conn.commit()
    return sid


def find_matched_product_key(new_key, conn, threshold=88):
    """Map a new cluster signal to an existing historical product_key via fuzzy
    matching. Lets 'stanley tumbler' (today) find 'stanleys tumbler' (last week)
    even when the entity-extractor produced slightly different strings.

    Returns the existing key if a fuzzy match exists, else new_key unchanged."""
    if not FUZZY_OK:
        return new_key
    rows = conn.execute("SELECT DISTINCT product_key FROM product_obs").fetchall()
    best_score = 0
    best_key = new_key
    for (k,) in rows:
        if not k:
            continue
        if k == new_key:
            return k
        s = fuzz.token_set_ratio(new_key, k)
        if s >= threshold and s > best_score:
            best_score = s
            best_key = k
    return best_key


def momentum_features(product_key, conn, now_ts=None):
    """Compute trend stage and growth rates for a product, using PRIOR snapshots
    only (the current run should NOT be in the DB yet when this is called).

    Returns a dict with keys:
        stage:              one of MOMENTUM_MULTIPLIER keys
        days_observed:      days since first prior observation
        days_since_last:    days since most recent prior observation
        n_prior_snapshots:  how many prior runs we have data from
        view_wow_pct:       % growth in views vs prior window  (None if N/A)
        creator_wow_pct:    % growth in creators
        intent_wow_pct:     % growth in buy-intent
        score_delta:        absolute change in analyzer score
    """
    now_ts = now_ts or int(time.time())

    rows = conn.execute("""
        SELECT s.run_ts, p.total_views, p.n_creators, p.score, p.buy_intent
          FROM product_obs p
          JOIN snapshots s ON p.snapshot_id = s.snapshot_id
         WHERE p.product_key = ?
      ORDER BY s.run_ts DESC LIMIT 30
    """, (product_key,)).fetchall()

    empty = {
        "stage": "first_seen", "days_observed": 0,
        "days_since_last": None, "n_prior_snapshots": 0,
        "view_wow_pct": None, "creator_wow_pct": None,
        "intent_wow_pct": None, "score_delta": None,
    }

    n = len(rows)
    if n == 0:
        return empty

    most_recent_ts = rows[0][0]
    oldest_ts = rows[-1][0]
    days_observed = (now_ts - oldest_ts) / 86400
    days_since_last = (now_ts - most_recent_ts) / 86400

    # If last observation is too old, treat as first-seen again
    if days_since_last > 21:
        result = dict(empty)
        result["days_observed"] = round(days_observed, 1)
        result["n_prior_snapshots"] = n
        result["days_since_last"] = round(days_since_last, 1)
        return result

    # With one prior observation we can show deltas but not growth rate trends
    if n == 1:
        return {
            "stage": "noisy",  # need >=2 prior points for direction
            "days_observed": round(days_observed, 1),
            "days_since_last": round(days_since_last, 1),
            "n_prior_snapshots": 1,
            "view_wow_pct": None, "creator_wow_pct": None,
            "intent_wow_pct": None, "score_delta": None,
        }

    # Now-window: most recent observation
    recent_views, recent_creators, recent_score, recent_intent = rows[0][1:]

    # Prior-window: average of older observations within the last 30 days
    prior = [r for r in rows[1:] if (now_ts - r[0]) / 86400 <= 30]
    if not prior:
        prior = rows[1:2]
    prior_views = sum(r[1] for r in prior) / len(prior)
    prior_creators = sum(r[2] for r in prior) / len(prior)
    prior_score = sum(r[3] for r in prior) / len(prior)
    prior_intent = sum(r[4] for r in prior) / len(prior)

    def growth(now, was):
        if was <= 0:
            return None
        return (now - was) / was

    view_g = growth(recent_views, prior_views)
    creator_g = growth(recent_creators, prior_creators)
    intent_g = growth(recent_intent, prior_intent)
    score_d = recent_score - prior_score

    def gt(x, t):
        return x is not None and x > t

    def lt(x, t):
        return x is not None and x < t

    # Classification (cascaded; earlier rules win)
    if days_observed < 7 and gt(view_g, 0.3):
        stage = "early_acceleration"
    elif gt(view_g, 0.3) and (gt(intent_g, 0.3) or gt(creator_g, 0.3)):
        stage = "compounding"
    elif gt(view_g, 0.05) and (gt(creator_g, 0) or gt(intent_g, 0)):
        stage = "expanding"
    elif lt(view_g, -0.2) and (lt(intent_g, 0) or score_d < -10):
        stage = "declining"
    elif view_g is not None and abs(view_g) < 0.1 and days_observed > 14:
        stage = "mature_steady"
    else:
        stage = "noisy"

    def pct(x):
        return None if x is None else round(x * 100, 1)

    return {
        "stage": stage,
        "days_observed": round(days_observed, 1),
        "days_since_last": round(days_since_last, 1),
        "n_prior_snapshots": n,
        "view_wow_pct": pct(view_g),
        "creator_wow_pct": pct(creator_g),
        "intent_wow_pct": pct(intent_g),
        "score_delta": round(score_d, 1),
    }
