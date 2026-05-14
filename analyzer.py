"""
TikTok product viability analyzer.

Consumes a scraper output JSON (results/viral_*.json) and produces:
  - results/analysis_<target>_<ts>.json   per-video enriched metrics
  - results/verdicts_<target>_<ts>.csv    per-cluster business verdict
  - results/report_<target>_<ts>.md       human-readable top picks with reasoning

Usage:
    python analyzer.py results/viral_skincare_20260514_120000.json
    python analyzer.py                # auto-uses most-recent results/viral_*.json
"""

import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

OUT_DIR = Path("results")


# ---------------------------------------------------------------------------
# Lexicons - tunable
# ---------------------------------------------------------------------------

POSITIVE_PHRASES = [
    # 3-point: confirmed purchase / strong endorsement
    ("ordered mine", 3), ("just ordered", 3), ("already ordered", 3),
    ("bought it", 3), ("just bought", 3), ("purchased", 3),
    ("love mine", 3), ("worth every penny", 3), ("best purchase", 3),
    ("life changing", 3), ("life-changing", 3), ("game changer", 3),
    ("game-changer", 3), ("actually works", 3), ("works so well", 3),
    ("totally worth", 3),
    # 2-point: strong positive
    ("obsessed", 2), ("must have", 2), ("must-have", 2),
    ("highly recommend", 2), ("love this", 2), ("love it", 2),
    ("amazing", 2), ("incredible", 2), ("so good", 2),
    ("i need this", 2), ("i need it", 2), ("i want this", 2),
    ("looks amazing", 2), ("can't wait", 2), ("cant wait", 2),
    ("worth it", 2),
    # 1-point: mild positive
    ("cute", 1), ("pretty", 1), ("nice", 1), ("looks good", 1),
    ("want it", 1), ("looks cool", 1),
]

NEGATIVE_PHRASES = [
    # 3-point: refund-level
    ("returned mine", 3), ("returned it", 3), ("got refunded", 3),
    ("broke after", 3), ("stopped working", 3), ("doesn't work", 3),
    ("does not work", 3), ("scam", 3), ("ripoff", 3), ("rip off", 3),
    ("waste of money", 3), ("don't buy", 3), ("do not buy", 3),
    ("wouldn't recommend", 3), ("would not recommend", 3),
    # 2-point: quality concerns
    ("disappointed", 2), ("disappointing", 2), ("cheap quality", 2),
    ("feels cheap", 2), ("looks cheap", 2), ("not worth", 2),
    ("regret buying", 2), ("regretted", 2), ("trash", 2), ("garbage", 2),
    ("horrible", 2), ("terrible quality", 2),
    # 1-point: mild negative
    ("meh", 1), ("overhyped", 1), ("overrated", 1), ("not great", 1),
    ("expected better", 1), ("not impressed", 1),
]

SKEPTICAL_PHRASES = [
    # 2-point: explicit doubt about authenticity
    ("is this an ad", 2), ("is this sponsored", 2), ("paid promotion", 2),
    ("fake views", 2), ("bot views", 2), ("is this real", 2),
    ("is this fake", 2), ("smells like an ad", 2),
    # 1-point: mild skepticism
    ("ad?", 1), ("sponsored?", 1), ("does this actually work", 1),
    ("does it actually work", 1),
]

# Genuine specific questions = real buyer research, not just hype
SPECIFIC_QUESTIONS = [
    "does it work on", "does it work for", "is it safe for",
    "how long does", "can you use it", "would this work",
    "does it come in", "what size", "does it ship",
]

# Category lexicon - first match wins by total keyword hits in cluster text
CATEGORIES = {
    "beauty": [
        "skincare", "serum", "makeup", "mascara", "foundation", "lipstick",
        "blush", "concealer", "moisturizer", "sunscreen", "spf", "perfume",
        "fragrance", "lotion", "cleanser", "retinol", "hyaluronic",
        "niacinamide", "exfoliant", "primer", "lipgloss", "lip gloss",
        "eyeshadow", "eyeliner", "hair mask", "hair oil",
    ],
    "gadget": [
        "gadget", "tech", "smart ", "wireless", "bluetooth", "charger",
        "cable", "led ", "lamp", "camera", "earbuds", "headphones",
        "speaker", "smartwatch", "tracker", "drone",
    ],
    "kitchen": [
        "kitchen", "mug", "tumbler", "blender", "mixer", "knife", "cookware",
        "pan", "pot", "spatula", "cutting board", "coffee", "espresso",
        "snack", "tea",
    ],
    "apparel": [
        "dress", "shirt", "tee ", "pants", "jeans", "jacket", "coat",
        "sneakers", "shoes", "boots", "hat", "bag", "purse", "jewelry",
        "ring", "necklace", "earring", "leggings", "hoodie", "sweater",
    ],
    "fitness": [
        "workout", "gym", "fitness", "protein", "supplement", "yoga",
        "pilates", "dumbbell", "resistance band", "running",
    ],
    "home": [
        "home decor", "bedroom", "bathroom", "decor", "candle", "rug",
        "pillow", "blanket", "sheet", "curtain", "vase", "wall art",
        "organizer", "container",
    ],
    "pet": [
        " dog ", " cat ", "puppy", "kitten", " pet ", "leash", "harness",
    ],
}

PRICE_RE = re.compile(r"\$\s?(\d{1,4})(?:\.\d{1,2})?")
UNDER_RE = re.compile(r"under\s*\$?\s*(\d{1,4})", re.I)
PRICE_WORD_RE = re.compile(r"(\d{1,4})\s*(?:dollars|bucks)", re.I)

PREMIUM_HINTS = ["luxury", "designer", "high end", "high-end", "premium", "splurge"]
CHEAP_HINTS = ["dupe", "affordable", "budget", "broke girl", "deal", "under $", "only $"]

HARD_TO_SHIP = {"apparel"}            # size / fit / return issues
BEST_MARGIN = {"beauty", "gadget", "kitchen", "home"}  # cheap to source, high markup
RESTRICTED = {"supplement"}           # ingestible = legal/regulatory risk

# Constants duplicated from scraper.py (kept here to avoid scraper's import-time
# side effects from flag parsing). Keep in sync if scraper's lexicon changes.
PRODUCT_HASHTAGS_PTS = {
    "tiktokmademebuyit": 4, "tiktokshop": 4, "amazonfinds": 4,
    "amazonmusthaves": 4, "founditonamazon": 4,
    "productreview": 2, "musthaves": 2, "shophaul": 2, "unboxing": 2,
    "shoppinghaul": 2, "affordablefinds": 2, "targetfinds": 2,
    "shopwithme": 2, "amazondeal": 2, "amazondeals": 2,
}
SHOP_DOMAINS = ["amazon.", "shopify", "shopee", "tiktok.com/t/",
                "linktr.ee", "beacons.ai", "stan.store", "allmylinks.com"]
CLUSTER_KEYWORDS = {
    "tiktokmademebuyit", "tiktokshop", "amazon find",
    "available on amazon", "found on amazon",
    "it's on amazon", "its on amazon",
    "use code", "discount code", "promo code", "coupon code",
}


# ---------------------------------------------------------------------------
# Comment scoring
# ---------------------------------------------------------------------------

def _best_match(text_lower, phrases):
    best = 0
    for phrase, weight in phrases:
        if phrase in text_lower and weight > best:
            best = weight
    return best


def analyze_comments(comments):
    if not comments:
        return {
            "positive_score": 0, "negative_score": 0, "skeptical_score": 0,
            "specific_questions": 0, "net_tone": 0, "comment_count": 0,
            "positive_samples": [], "negative_samples": [],
        }
    pos = neg = skep = qs = 0
    pos_samples, neg_samples = [], []
    counted = 0
    for c in comments:
        c = (c or "").strip()
        if not c:
            continue
        counted += 1
        cl = c.lower()
        p = _best_match(cl, POSITIVE_PHRASES)
        n = _best_match(cl, NEGATIVE_PHRASES)
        s = _best_match(cl, SKEPTICAL_PHRASES)
        pos += p
        neg += n
        skep += s
        if any(q in cl for q in SPECIFIC_QUESTIONS):
            qs += 1
        if p and len(pos_samples) < 3:
            pos_samples.append(c[:120])
        if n and len(neg_samples) < 3:
            neg_samples.append(c[:120])
    return {
        "positive_score": pos, "negative_score": neg, "skeptical_score": skep,
        "specific_questions": qs, "net_tone": pos - neg - skep,
        "comment_count": counted,
        "positive_samples": pos_samples, "negative_samples": neg_samples,
    }


def parse_comments_raw(row):
    raw = row.get("comments_raw", "")
    if isinstance(raw, list):
        return [c for c in raw if c]
    if isinstance(raw, str) and raw:
        return [c.strip() for c in raw.split("|||") if c.strip()]
    # fall back to intent samples if no full comments were saved
    samples = row.get("comment_intent_samples", "") or ""
    return [c.strip() for c in samples.split("||") if c.strip()]


# ---------------------------------------------------------------------------
# Per-video enrichment
# ---------------------------------------------------------------------------

def _pct(num, den):
    return round(num / den * 100, 3) if den > 0 else 0.0


def enrich_video(row):
    views = row.get("views", 0) or 0
    likes = row.get("likes", 0) or 0
    n_comments = row.get("comments", 0) or 0
    shares = row.get("shares", 0) or 0
    saves = row.get("saves", 0) or 0
    followers = row.get("author_followers", 0) or 0

    engagement_pct = _pct(likes + n_comments * 2 + shares * 3 + saves * 4, views)

    caption_l = (row.get("caption", "") or "").lower()
    hashtags_l = (row.get("hashtags", "") or "").lower()
    ad_markers = ["#ad", "#sponsored", "#paidpartnership", "#gifted",
                  "paid partnership", "sponsored by"]
    paid_flag = any(m in caption_l or m in hashtags_l for m in ad_markers)
    low_eng_warning = views >= 500_000 and engagement_pct < 1.5

    organic_mult = round(views / max(followers, 1), 2) if followers else 0
    if followers and followers < 200_000 and views >= followers * 3:
        credibility = "micro_organic"
    elif followers and followers > 5_000_000:
        credibility = "mega_likely_paid"
    elif followers and views >= followers * 5:
        credibility = "organic_viral"
    elif followers == 0:
        credibility = "unknown"
    else:
        credibility = "normal"

    sentiment = analyze_comments(parse_comments_raw(row))

    return {
        **row,
        "like_ratio_pct": _pct(likes, views),
        "save_ratio_pct": _pct(saves, views),
        "share_ratio_pct": _pct(shares, views),
        "comment_ratio_pct": _pct(n_comments, views),
        "engagement_pct": engagement_pct,
        "paid_content_flag": paid_flag,
        "low_engagement_warning": low_eng_warning,
        "creator_credibility": credibility,
        "organic_multiplier": organic_mult,
        "sentiment_positive": sentiment["positive_score"],
        "sentiment_negative": sentiment["negative_score"],
        "sentiment_skeptical": sentiment["skeptical_score"],
        "sentiment_specific_questions": sentiment["specific_questions"],
        "sentiment_net": sentiment["net_tone"],
        "sentiment_pos_samples": " || ".join(sentiment["positive_samples"]),
        "sentiment_neg_samples": " || ".join(sentiment["negative_samples"]),
        "comments_scraped_n": sentiment["comment_count"],
    }


# ---------------------------------------------------------------------------
# Cluster grouping
# ---------------------------------------------------------------------------

def canonicalize_link(link):
    try:
        u = urlparse(link)
        host = u.netloc.lower().lstrip("www.")
        return f"{host}{u.path.rstrip('/')}"
    except Exception:
        return link.lower()


def cluster_videos(rows):
    """Group videos by shared product signal. Same logic as scraper but on enriched rows."""
    clusters = {}

    def add(key, row):
        c = clusters.setdefault(key, {"signal": key, "videos": [], "creators": set()})
        c["videos"].append(row)
        c["creators"].add(row.get("author_username", ""))

    for r in rows:
        for tag in (r.get("hashtags", "") or "").split(","):
            t = tag.strip().lower()
            if t and PRODUCT_HASHTAGS_PTS.get(t, 0) >= 3:
                add(f"#{t}", r)
        for link in (r.get("links", "") or "").split(","):
            link = link.strip()
            if not link:
                continue
            host = urlparse(link).netloc.lower()
            if any(s in host for s in SHOP_DOMAINS):
                add(canonicalize_link(link), r)
        cap = (r.get("caption", "") or "").lower()
        for kw in CLUSTER_KEYWORDS:
            if kw in cap:
                add(f"kw:{kw}", r)

    out = []
    for c in clusters.values():
        creators = {u for u in c["creators"] if u}
        if len(creators) >= 2 and len(c["videos"]) >= 2:
            c["creators"] = creators
            out.append(c)
    return out


# ---------------------------------------------------------------------------
# Cluster-level business logic
# ---------------------------------------------------------------------------

def guess_category(text):
    text = text.lower()
    scores = {cat: sum(1 for kw in kws if kw in text)
              for cat, kws in CATEGORIES.items()}
    scores = {k: v for k, v in scores.items() if v > 0}
    if not scores:
        return "uncategorized"
    return max(scores, key=scores.get)


def guess_price_tier(text):
    text_l = text.lower()
    prices = []
    for rx in (PRICE_RE, UNDER_RE, PRICE_WORD_RE):
        for m in rx.finditer(text):
            try:
                prices.append(int(m.group(1)))
            except (ValueError, IndexError):
                pass
    if prices:
        prices.sort()
        median = prices[len(prices) // 2]
        if median <= 15:
            return "under_15", f"~${median}"
        if median <= 50:
            return "15_50", f"~${median}"
        if median <= 100:
            return "50_100", f"~${median}"
        return "100_plus", f"~${median}"
    if any(h in text_l for h in CHEAP_HINTS):
        return "under_15", "cheap_language"
    if any(h in text_l for h in PREMIUM_HINTS):
        return "100_plus", "premium_language"
    return "unknown", ""


def trajectory(ages):
    valid = [a for a in ages if isinstance(a, (int, float)) and a >= 0]
    if not valid:
        return "unknown"
    valid.sort()
    p50 = valid[len(valid) // 2]
    if p50 < 5:
        return "accelerating"
    if p50 < 14:
        return "fresh"
    if p50 < 30:
        return "mature"
    if p50 < 60:
        return "cooling"
    return "stale"


def saturation(n_creators):
    if n_creators >= 30:
        return "saturated"
    if n_creators >= 16:
        return "late"
    if n_creators >= 5:
        return "sweet_spot"
    return "early"


def score_cluster(cluster):
    """Return (final_score_0_to_100, verdict, reasons[], concerns[])."""
    videos = cluster["videos"]
    n_videos = len(videos)
    n_creators = len(cluster["creators"])

    total_views = sum(v.get("views", 0) for v in videos)
    total_likes = sum(v.get("likes", 0) for v in videos)
    total_shares = sum(v.get("shares", 0) for v in videos)
    total_saves = sum(v.get("saves", 0) for v in videos)
    total_buy_intent = sum(v.get("comment_intent_score", 0) for v in videos)
    total_pos = sum(v.get("sentiment_positive", 0) for v in videos)
    total_neg = sum(v.get("sentiment_negative", 0) for v in videos)
    total_skep = sum(v.get("sentiment_skeptical", 0) for v in videos)
    total_questions = sum(v.get("sentiment_specific_questions", 0) for v in videos)
    avg_engagement = (sum(v.get("engagement_pct", 0) for v in videos) / n_videos)
    avg_save_ratio = sum(v.get("save_ratio_pct", 0) for v in videos) / n_videos
    avg_velocity = sum(v.get("velocity_views_per_day", 0) for v in videos) / n_videos
    paid_count = sum(1 for v in videos if v.get("paid_content_flag"))
    micro_organic = sum(1 for v in videos if v.get("creator_credibility") == "micro_organic")
    low_eng_count = sum(1 for v in videos if v.get("low_engagement_warning"))

    sat = saturation(n_creators)
    traj = trajectory([v.get("age_days") for v in videos])
    cluster_text = " ".join((v.get("caption", "") or "") for v in videos)
    category = guess_category(cluster_text)
    price_tier, price_evidence = guess_price_tier(cluster_text)

    # ----- Scoring components (0-100 total) -----
    # creator diversity (0-25): sweet spot 5-15 creators is best
    if 5 <= n_creators <= 15:
        creator_pts = 25
    elif n_creators <= 4:
        creator_pts = 8 + n_creators * 2
    elif n_creators <= 25:
        creator_pts = 22 - (n_creators - 15)
    else:
        creator_pts = max(5, 15 - (n_creators - 25) // 2)

    # engagement quality (0-25): organic content runs 3-8%; >=5% is excellent
    eng_pts = min(25, avg_engagement * 5)

    # demand signal (0-25): positive sentiment + buy-intent, minus negative/skeptical
    raw_demand = total_buy_intent * 2 + total_pos + total_questions - total_neg * 2 - total_skep
    demand_pts = max(0, min(25, raw_demand))

    # freshness (0-15)
    freshness_pts = {"accelerating": 15, "fresh": 12, "mature": 7,
                     "cooling": 3, "stale": 0, "unknown": 5}.get(traj, 5)

    # sellability bonus (0-10): margin-friendly categories, micro-creator presence
    sellability_pts = 0
    if category in BEST_MARGIN:
        sellability_pts += 4
    if category in HARD_TO_SHIP:
        sellability_pts -= 3
    if price_tier in ("under_15", "15_50"):
        sellability_pts += 3
    elif price_tier == "100_plus":
        sellability_pts -= 2
    if micro_organic >= 2:
        sellability_pts += 3
    sellability_pts = max(-5, min(10, sellability_pts))

    raw_score = creator_pts + eng_pts + demand_pts + freshness_pts + sellability_pts
    final_score = max(0, min(100, round(raw_score, 1)))

    # ----- Verdict overrides -----
    overrides = []
    if sat == "saturated":
        verdict = "SATURATED"
        overrides.append("30+ creators promoting = late to the trend")
    elif total_neg > total_pos and total_neg >= 3:
        verdict = "AVOID"
        overrides.append(f"More negative comments ({total_neg}) than positive ({total_pos})")
    elif paid_count / max(n_videos, 1) >= 0.5:
        # majority of videos are explicit #ad - not organic
        verdict = "WATCH"
        overrides.append(f"{paid_count}/{n_videos} videos are paid - not organic demand")
    elif final_score >= 75:
        verdict = "STRONG SELL"
    elif final_score >= 55:
        verdict = "SELL"
    elif final_score >= 35:
        verdict = "WATCH"
    else:
        verdict = "SKIP"

    # ----- Reasoning -----
    reasons, concerns = [], []
    if 5 <= n_creators <= 15:
        reasons.append(f"{n_creators} unrelated creators = real organic spread")
    if traj == "accelerating":
        reasons.append("trend is accelerating (median age <5 days)")
    elif traj == "fresh":
        reasons.append("trend is fresh (median age <2 weeks)")
    if avg_engagement >= 5:
        reasons.append(f"strong engagement ({avg_engagement:.1f}%) - organic-quality interest")
    if avg_save_ratio >= 0.5:
        reasons.append(f"high save rate ({avg_save_ratio:.2f}%) - viewers bookmarking to buy")
    if total_buy_intent >= 5:
        reasons.append(f"{total_buy_intent} explicit buy-intent comments ('where to buy', etc.)")
    if total_pos >= 5 and total_neg == 0:
        reasons.append(f"positive sentiment ({total_pos}) with no negative comments")
    if micro_organic >= 2:
        reasons.append(f"{micro_organic} micro-creators with organic reach (high credibility)")
    if category in BEST_MARGIN:
        reasons.append(f"category '{category}' has good margins and is easy to ship")
    if price_tier in ("under_15", "15_50") and price_evidence:
        reasons.append(f"price point {price_evidence} is in the dropship sweet spot")

    if sat == "late":
        concerns.append(f"{n_creators} creators promoting - getting saturated")
    if traj == "cooling":
        concerns.append("trend is cooling (most videos >30 days old)")
    elif traj == "stale":
        concerns.append("trend is stale (median age >60 days)")
    if total_neg >= 2:
        concerns.append(f"{total_neg} negative comments mentioning returns / broken / cheap quality")
    if total_skep >= 2:
        concerns.append(f"{total_skep} skeptical comments - viewers calling it an ad")
    if paid_count > 0:
        concerns.append(f"{paid_count}/{n_videos} videos are paid partnerships - reduces organic signal")
    if low_eng_count > 0:
        concerns.append(f"{low_eng_count} videos with suspicious low engagement (possible bot/ad-boost)")
    if avg_engagement < 2:
        concerns.append(f"average engagement only {avg_engagement:.1f}% - weak organic interest")
    if category in HARD_TO_SHIP:
        concerns.append(f"category '{category}' has high return/sizing issues - harder to ship profitably")
    if price_tier == "100_plus":
        concerns.append(f"price point {price_evidence or 'premium'} = lower volume, harder margin")

    return {
        "score": final_score,
        "verdict": verdict,
        "reasons": reasons[:5],
        "concerns": concerns[:5],
        "overrides": overrides,
        "category": category,
        "price_tier": price_tier,
        "price_evidence": price_evidence,
        "saturation": sat,
        "trajectory": traj,
        "n_creators": n_creators,
        "n_videos": n_videos,
        "total_views": total_views,
        "total_likes": total_likes,
        "total_shares": total_shares,
        "total_saves": total_saves,
        "total_buy_intent": total_buy_intent,
        "total_positive_sentiment": total_pos,
        "total_negative_sentiment": total_neg,
        "total_skeptical": total_skep,
        "total_specific_questions": total_questions,
        "avg_engagement_pct": round(avg_engagement, 3),
        "avg_save_ratio_pct": round(avg_save_ratio, 3),
        "avg_velocity_per_day": round(avg_velocity, 1),
        "paid_video_count": paid_count,
        "micro_organic_count": micro_organic,
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_verdicts_csv(scored_clusters, path):
    if not scored_clusters:
        return
    sample = scored_clusters[0]
    base_keys = [
        "signal", "verdict", "score", "category", "price_tier", "price_evidence",
        "saturation", "trajectory", "n_creators", "n_videos",
        "total_views", "total_likes", "total_shares", "total_saves",
        "total_buy_intent", "total_positive_sentiment", "total_negative_sentiment",
        "total_skeptical", "total_specific_questions",
        "avg_engagement_pct", "avg_save_ratio_pct", "avg_velocity_per_day",
        "paid_video_count", "micro_organic_count",
        "reasons", "concerns", "example_urls",
    ]
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=base_keys)
        w.writeheader()
        for c in scored_clusters:
            row = {k: c.get(k, "") for k in base_keys if k not in
                   ("reasons", "concerns", "example_urls")}
            row["reasons"] = " | ".join(c.get("reasons", []))
            row["concerns"] = " | ".join(c.get("concerns", []))
            row["example_urls"] = " | ".join(c.get("example_urls", []))
            w.writerow(row)


def write_report_md(scored_clusters, path, target):
    lines = [
        f"# Product Verdict Report — {target}",
        f"_Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}_",
        "",
        f"**{len(scored_clusters)} clusters analyzed.** Verdicts:",
        "",
    ]
    by_verdict = {}
    for c in scored_clusters:
        by_verdict.setdefault(c["verdict"], []).append(c)
    order = ["STRONG SELL", "SELL", "WATCH", "SATURATED", "SKIP", "AVOID"]
    for v in order:
        if v in by_verdict:
            lines.append(f"- **{v}**: {len(by_verdict[v])}")
    lines.append("")

    for v in order:
        if v not in by_verdict:
            continue
        lines.append(f"## {v}")
        lines.append("")
        for c in by_verdict[v]:
            lines.append(f"### {c['signal']} — score {c['score']}")
            lines.append("")
            lines.append(
                f"`{c['category']}` · price `{c['price_tier']}` "
                f"{c['price_evidence']} · {c['n_creators']} creators · "
                f"{c['n_videos']} videos · {c['total_views']:,} views · "
                f"`{c['saturation']}` · `{c['trajectory']}`"
            )
            lines.append("")
            if c.get("overrides"):
                lines.append("**Override:** " + "; ".join(c["overrides"]))
                lines.append("")
            if c["reasons"]:
                lines.append("**Why it could sell:**")
                for r in c["reasons"]:
                    lines.append(f"- {r}")
                lines.append("")
            if c["concerns"]:
                lines.append("**Concerns:**")
                for r in c["concerns"]:
                    lines.append(f"- {r}")
                lines.append("")
            if c.get("example_urls"):
                lines.append("**Examples:**")
                for u in c["example_urls"]:
                    lines.append(f"- {u}")
                lines.append("")
        lines.append("")
    Path(path).write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def find_latest_json():
    candidates = sorted(OUT_DIR.glob("viral_*.json"), key=lambda p: p.stat().st_mtime,
                        reverse=True)
    if not candidates:
        candidates = sorted(OUT_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime,
                            reverse=True)
    return candidates[0] if candidates else None


def main():
    if len(sys.argv) > 1:
        json_path = Path(sys.argv[1])
    else:
        json_path = find_latest_json()
        if not json_path:
            print("Usage: python analyzer.py <results/viral_*.json>")
            print("(or run the scraper first to produce a JSON file)")
            sys.exit(1)
        print(f"[+] No path given, using latest: {json_path}")

    if not json_path.exists():
        print(f"[!] File not found: {json_path}")
        sys.exit(1)

    rows = json.loads(json_path.read_text(encoding="utf-8"))
    print(f"[+] Loaded {len(rows)} videos from {json_path.name}")

    enriched = [enrich_video(r) for r in rows]

    target_match = re.search(r"(?:viral|hashtag|profile|video)_([^_]+)_\d{8}",
                             json_path.stem)
    target = target_match.group(1) if target_match else json_path.stem
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    analysis_path = OUT_DIR / f"analysis_{target}_{ts}.json"
    analysis_path.write_text(json.dumps(enriched, indent=2, ensure_ascii=False),
                             encoding="utf-8")
    print(f"[OK] Per-video enrichment -> {analysis_path}")

    clusters = cluster_videos(enriched)
    if not clusters:
        print("[!] No cross-creator clusters found (need >=2 creators sharing a signal).")
        return

    scored = []
    for c in clusters:
        s = score_cluster(c)
        s["signal"] = c["signal"]
        s["example_urls"] = [v.get("url", "") for v in c["videos"][:3]]
        scored.append(s)
    scored.sort(key=lambda x: x["score"], reverse=True)

    verdicts_path = OUT_DIR / f"verdicts_{target}_{ts}.csv"
    report_path = OUT_DIR / f"report_{target}_{ts}.md"
    write_verdicts_csv(scored, verdicts_path)
    write_report_md(scored, report_path, target)
    print(f"[OK] Cluster verdicts -> {verdicts_path}")
    print(f"[OK] Human report   -> {report_path}")
    print("\nTop 5 candidates:")
    for c in scored[:5]:
        print(f"  [{c['verdict']:<12}] {c['score']:>5.1f}  {c['signal']:<40}  "
              f"{c['category']:<14} {c['n_creators']}c/{c['n_videos']}v "
              f"{c['saturation']}/{c['trajectory']}")


if __name__ == "__main__":
    main()
