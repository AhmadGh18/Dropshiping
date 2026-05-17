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
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

# Optional dependencies - analyzer degrades gracefully if missing.
try:
    from rapidfuzz import fuzz
    FUZZY_OK = True
except ImportError:
    FUZZY_OK = False

try:
    import wordninja
    WORDNINJA_OK = True
except ImportError:
    WORDNINJA_OK = False

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

SPECIFIC_QUESTIONS = [
    "does it work on", "does it work for", "is it safe for",
    "how long does", "can you use it", "would this work",
    "does it come in", "what size", "does it ship",
]

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

# Anchored price detection: require a price-context anchor word before the $X.
# Bare "$5 off" -> caught by DISCOUNT_RE first and excluded.
DISCOUNT_RE = re.compile(r"\$\s?(\d{1,4})\s*(?:off|discount|coupon)", re.I)
PRICE_ANCHORED_RE = re.compile(
    r"(?:for|only|just|is|=|@|costs?|priced at|retails (?:for|at))"
    r"\s*\$?\s*(\d{1,4})(?:\.\d{1,2})?",
    re.I,
)
PRICE_PLAIN_RE = re.compile(r"\$\s?(\d{1,4})(?:\.\d{1,2})?")
UNDER_RE = re.compile(r"under\s*\$?\s*(\d{1,4})", re.I)
PRICE_WORD_RE = re.compile(r"(\d{1,4})\s*(?:dollars|bucks)\b", re.I)

PREMIUM_HINTS = ["luxury", "designer", "high end", "high-end", "premium", "splurge"]
CHEAP_HINTS = ["dupe", "affordable", "budget", "broke girl", "deal", "under $", "only $"]

HARD_TO_SHIP = {"apparel"}
BEST_MARGIN = {"beauty", "gadget", "kitchen", "home"}

# ---------------------------------------------------------------------------
# Logistics rules - keyword penalties / bonuses applied to the cluster's text.
# Each entry is (compiled regex, points, reason). Points sum into sellability.
# ---------------------------------------------------------------------------

LOGISTICS_PENALTIES = [
    (re.compile(r"\b(king|queen|weighted|oversized|huge|giant|jumbo)\b", re.I),
     -5, "bulky -> high shipping cost"),
    (re.compile(r"\b(glass|ceramic|mirror|porcelain|crystal)\b", re.I),
     -4, "fragile -> breakage / returns"),
    (re.compile(r"\b(battery|rechargeable|lithium|powerbank|power\s+bank)\b", re.I),
     -3, "lithium -> shipping restrictions"),
    (re.compile(r"\b(perfume|fragrance|cologne|aerosol|hairspray|hair\s+spray)\b", re.I),
     -4, "flammable -> hazmat fees"),
    (re.compile(r"\b(supplement|vitamin|cbd|melatonin|gummy|tincture|probiotic)\b", re.I),
     -8, "ingestible -> FDA / platform restrictions"),
    (re.compile(
        r"\b(pokemon|disney|marvel|nike|adidas|stanley\b|apple\b|sanrio|lego|"
        r"harry\s+potter|nintendo|sony|samsung|gucci|prada|chanel|hermes)\b", re.I),
     -10, "trademarked brand -> listing removal / lawsuit risk"),
    (re.compile(r"\b(baby|toddler|infant|crib|car\s+seat|pacifier)\b", re.I),
     -5, "children's product -> CPSC safety compliance required"),
    (re.compile(r"\b(laser|knife|sword|airsoft|taser|firearm|weapon)\b", re.I),
     -8, "weapon-adjacent -> platform restrictions"),
    (re.compile(r"\b(size\s+[xsml]+|fits\s+sizes|true\s+to\s+size|"
                r"runs\s+small|runs\s+large|wear\s+a\s+size)\b", re.I),
     -3, "sized apparel -> high return rate from fit"),
]

LOGISTICS_BONUSES = [
    (re.compile(r"\b(refill|refillable|cartridge|consumable|monthly|subscription)\b", re.I),
     +5, "consumable -> repeat customers"),
    (re.compile(r"\b(travel|portable|compact|mini|pocket|foldable)\b", re.I),
     +3, "compact -> cheap shipping"),
    (re.compile(r"\b(silicone|nylon|aluminum|stainless)\b", re.I),
     +1, "durable material -> low returns"),
    (re.compile(r"\b(unisex|one\s+size|adjustable)\b", re.I),
     +2, "no sizing returns"),
]

# Rough industry-typical retail margins by category (post-COGS, pre-ad-spend)
BASE_MARGIN = {
    "beauty": 0.65, "gadget": 0.55, "home": 0.55, "kitchen": 0.50,
    "pet": 0.55, "fitness": 0.45, "apparel": 0.40, "uncategorized": 0.40,
}
# Price tier acts as a margin multiplier - the $15-50 sweet spot is best
PRICE_MARGIN_ADJ = {
    "under_15": 1.00, "15_50": 1.15, "50_100": 1.00,
    "100_plus": 0.80, "unknown": 0.95,
}
# Below this, ad spend kills the business
MIN_VIABLE_MARGIN = 0.25

# Word-boundary ad detection (does NOT match #advertisement)
AD_RE = re.compile(
    r"(?:^|[\s#])(?:ad|sponsored|paidpartnership|gifted|paid\s+partnership|"
    r"sponsored\s+by)(?:[\s#:!.?,]|$)",
    re.I,
)

# ---------------------------------------------------------------------------
# Clustering constants - tight-scoped to avoid generic-CTA cluster pollution
# ---------------------------------------------------------------------------

SHOP_DOMAINS = ["amazon.", "shopify", "shopee", "tiktok.com/t/",
                "linktr.ee", "beacons.ai", "stan.store", "allmylinks.com"]

# Hashtags that lump unrelated products under one bucket - excluded from
# entity/tag clustering, only used for paid-content detection above.
MEGA_CTA_TAGS = {
    "tiktokmademebuyit", "tiktokshop", "amazonfinds", "amazonmusthaves",
    "founditonamazon", "fyp", "fy", "foryou", "foryoupage", "viral",
    "musthaves", "shophaul", "shoppinghaul", "haul", "ad", "sponsored",
    "paidpartnership", "gifted", "ootd", "review", "unboxing",
    "productreview", "shopwithme", "affordablefinds", "targetfinds",
    "amazondeal", "amazondeals",
}

# Specific product-hashtag whitelist (carries enough product specificity to
# cluster on, even though it doesn't decompose to noun tokens).
SPECIFIC_HASHTAGS = set()  # could be populated from prior runs / Amazon BSR

# Product nouns used by entity extraction and hashtag decomposition.
PRODUCT_NOUNS = {
    # beauty
    "serum", "cream", "mask", "lipstick", "gloss", "balm", "lotion",
    "cleanser", "moisturizer", "sunscreen", "spf", "perfume", "primer",
    "concealer", "foundation", "blush", "mascara", "eyeliner", "eyeshadow",
    "fragrance",
    # gadget
    "charger", "cable", "earbuds", "headphones", "lamp", "light", "speaker",
    "tracker", "watch", "smartwatch", "camera", "drone",
    # kitchen
    "tumbler", "mug", "cup", "blender", "knife", "pan", "kettle", "mixer",
    "spatula", "container", "organizer", "kettle",
    # home
    "candle", "diffuser", "rug", "pillow", "blanket", "curtain", "vase",
    # apparel
    "dress", "shirt", "tee", "jacket", "sneakers", "boots", "bag", "purse",
    "leggings", "hoodie", "sweater",
    # fitness
    "weights", "dumbbell", "band", "mat",
    # pet
    "leash", "harness", "collar", "toy",
}

# Patterns for entity extraction
CAPS_NOUN_PATTERN = (
    r"\b([A-Z][\w'&-]+(?:\s+[A-Z][\w'&-]+){0,2})\s+("
    + "|".join(re.escape(n) for n in PRODUCT_NOUNS)
    + r")\b"
)
CAPS_NOUN_RE = re.compile(CAPS_NOUN_PATTERN)
HASHTAG_RE_LOCAL = re.compile(r"#(\w+)")
QUOTED_RE = re.compile(r"['\"]([^'\"]{3,40})['\"]")


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
    samples = row.get("comment_intent_samples", "") or ""
    return [c.strip() for c in samples.split("||") if c.strip()]


# ---------------------------------------------------------------------------
# Product entity extraction (rule-based NER)
# ---------------------------------------------------------------------------

def normalize_phrase(phrase):
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", phrase.lower())).strip()


def split_hashtag(tag):
    """Split a concatenated hashtag ('stanleycup' -> ['stanley', 'cup']).
    Falls back to single-token list if wordninja isn't installed."""
    if not WORDNINJA_OK:
        return [tag]
    try:
        return wordninja.split(tag)
    except Exception:
        return [tag]


def extract_product_phrases(row):
    """Return a set of candidate product entity strings from one video.

    Strategy:
      1. 'Brand Noun' capitalized n-grams: 'Stanley tumbler', 'Drunk Elephant serum'
      2. Hashtag decomposition: '#stanleycup' -> 'stanley cup' (must contain a noun)
      3. Quoted strings adjacent to product nouns
    """
    caption = row.get("caption", "") or ""
    out = set()

    for m in CAPS_NOUN_RE.finditer(caption):
        out.add(normalize_phrase(f"{m.group(1)} {m.group(2)}"))

    for tag in HASHTAG_RE_LOCAL.findall(caption):
        tl = tag.lower()
        if tl in MEGA_CTA_TAGS:
            continue
        tokens = [t.lower() for t in split_hashtag(tl)]
        if 2 <= len(tokens) <= 4 and any(t in PRODUCT_NOUNS for t in tokens):
            out.add(normalize_phrase(" ".join(tokens)))

    caption_l = caption.lower()
    if any(n in caption_l for n in PRODUCT_NOUNS):
        for q in QUOTED_RE.findall(caption):
            ql = q.lower()
            if any(n in ql for n in PRODUCT_NOUNS) and len(q.split()) <= 5:
                out.add(normalize_phrase(q))

    # Filter very short or pure-noun phrases (e.g. just 'serum' isn't a product)
    out = {p for p in out if len(p.split()) >= 2 and len(p) <= 60}
    return out


def cross_validated_in_comments(phrase, comments_text_lower):
    """Cluster cross-validation: the product phrase is mentioned in the
    cluster's viewer comments (not just creator captions)."""
    if not comments_text_lower:
        return False
    tokens = phrase.split()
    return all(tok in comments_text_lower for tok in tokens)


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

    caption = row.get("caption", "") or ""
    hashtags_str = " ".join(
        f"#{t.strip()}" for t in (row.get("hashtags", "") or "").split(",") if t.strip()
    )
    paid_flag = bool(AD_RE.search(caption)) or bool(AD_RE.search(hashtags_str))
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
# Three-tier clustering
# ---------------------------------------------------------------------------

def canonicalize_link(link):
    try:
        u = urlparse(link)
        host = u.netloc.lower().lstrip("www.")
        return f"{host}{u.path.rstrip('/')}"
    except Exception:
        return link.lower()


def fuzzy_merge(buckets, threshold=88):
    """Merge near-duplicate keys: 'stanley tumbler' + 'stanleys tumbler' -> one bucket.
    No-op if rapidfuzz unavailable or only one bucket."""
    if not FUZZY_OK or len(buckets) <= 1:
        return dict(buckets)
    keys = sorted(buckets.keys(), key=len)
    canonical = {}
    merged = defaultdict(list)
    for k in keys:
        target = None
        for existing in set(canonical.values()):
            if fuzz.token_set_ratio(k, existing) >= threshold:
                target = existing
                break
        if target is None:
            target = k
        canonical[k] = target
        merged[target].extend(buckets[k])
    return dict(merged)


def cluster_videos(rows):
    """Three-tier clustering. Returns list of cluster dicts:
       {signal, tier, videos, creators, cross_validated}

    Tiers (strongest to weakest):
      'link'   - shared shop link (one URL = one product, almost certain)
      'entity' - shared extracted product phrase ('stanley tumbler')
      'tag'    - shared product-specific hashtag (not mega-CTA)
    """
    by_link = defaultdict(list)
    by_entity = defaultdict(list)
    by_tag = defaultdict(list)

    for r in rows:
        # Tier 1: canonicalized shop links
        for link in (r.get("links") or "").split(","):
            link = link.strip()
            if not link:
                continue
            host = urlparse(link).netloc.lower()
            if any(s in host for s in SHOP_DOMAINS):
                by_link[canonicalize_link(link)].append(r)

        # Tier 2: extracted product entities
        for ent in extract_product_phrases(r):
            by_entity[ent].append(r)

        # Tier 3: product-specific hashtags (NOT mega-CTAs)
        for tag in (r.get("hashtags") or "").split(","):
            t = tag.strip().lower()
            if not t or t in MEGA_CTA_TAGS or len(t) < 6:
                continue
            tokens = split_hashtag(t)
            if (t in SPECIFIC_HASHTAGS
                or (2 <= len(tokens) <= 4 and any(n in PRODUCT_NOUNS for n in tokens))):
                by_tag[t].append(r)

    by_entity = fuzzy_merge(by_entity, threshold=88)

    out = []

    def build(key, videos, tier):
        creators = {v.get("author_username", "") for v in videos} - {""}
        if len(creators) < 2 or len(videos) < 2:
            return None
        cross_val = False
        if tier == "entity":
            for v in videos:
                comments_text = " ".join(parse_comments_raw(v)).lower()
                if cross_validated_in_comments(key, comments_text):
                    cross_val = True
                    break
        return {
            "signal": key, "tier": tier,
            "videos": videos, "creators": creators,
            "cross_validated": cross_val,
        }

    for key, vids in by_link.items():
        c = build(key, vids, "link")
        if c:
            out.append(c)
    for key, vids in by_entity.items():
        c = build(key, vids, "entity")
        if c:
            out.append(c)
    for key, vids in by_tag.items():
        c = build(f"#{key}", vids, "tag")
        if c:
            out.append(c)

    return out


# ---------------------------------------------------------------------------
# Cluster-level helpers
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
    """Anchored price detection. Excludes 'off / discount / coupon' amounts and
    prefers the maximum extracted price (product price, not the discount)."""
    text_l = text.lower()

    # Collect discount amounts so we can exclude them from plain-$ fallback
    discount_amounts = set()
    for m in DISCOUNT_RE.finditer(text):
        try:
            discount_amounts.add(int(m.group(1)))
        except (ValueError, IndexError):
            pass

    prices = []
    # High-confidence: price context anchor word in front
    for m in PRICE_ANCHORED_RE.finditer(text):
        try:
            prices.append(int(m.group(1)))
        except (ValueError, IndexError):
            pass
    # Lower-confidence fallbacks only when nothing anchored
    if not prices:
        for m in PRICE_PLAIN_RE.finditer(text):
            try:
                val = int(m.group(1))
                if val not in discount_amounts:
                    prices.append(val)
            except (ValueError, IndexError):
                pass
        for m in UNDER_RE.finditer(text):
            try:
                prices.append(int(m.group(1)))
            except (ValueError, IndexError):
                pass
        for m in PRICE_WORD_RE.finditer(text):
            try:
                prices.append(int(m.group(1)))
            except (ValueError, IndexError):
                pass

    if prices:
        # Use the maximum: a video saying "$5 off the $30 serum" should report $30
        product_price = max(prices)
        if product_price <= 15:
            return "under_15", f"~${product_price}"
        if product_price <= 50:
            return "15_50", f"~${product_price}"
        if product_price <= 100:
            return "50_100", f"~${product_price}"
        return "100_plus", f"~${product_price}"
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


def logistics_check(text):
    """Scan the cluster's combined text for shipping/legal/business red and
    green flags. Returns (total_pts, penalty_reasons, bonus_reasons).
    Each rule is applied at most once even if multiple matches."""
    pts = 0
    penalty_reasons, bonus_reasons = [], []
    for rx, p, reason in LOGISTICS_PENALTIES:
        if rx.search(text):
            pts += p
            penalty_reasons.append(f"{p:+d} {reason}")
    for rx, b, reason in LOGISTICS_BONUSES:
        if rx.search(text):
            pts += b
            bonus_reasons.append(f"{b:+d} {reason}")
    return pts, penalty_reasons, bonus_reasons


def estimate_margin(category, price_tier, logistics_pts):
    """Estimate net retail margin (0-1). Combines category baseline, price-tier
    multiplier, and logistics adjustment (each -1 logistics pt ~= -1% margin)."""
    base = BASE_MARGIN.get(category, 0.40)
    adj  = PRICE_MARGIN_ADJ.get(price_tier, 0.95)
    logistics_delta = max(-0.20, min(0.10, logistics_pts * 0.01))
    margin = base * adj + logistics_delta
    return max(0.05, min(0.75, margin))


def margin_confidence(category, price_tier):
    """How much trust to put in the margin estimate."""
    if category == "uncategorized" and price_tier == "unknown":
        return "low"
    if category != "uncategorized" and price_tier != "unknown":
        return "high"
    return "medium"


def score_cluster(cluster):
    """Return scoring dict with score, verdict, reasons, concerns."""
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
    avg_engagement = sum(v.get("engagement_pct", 0) for v in videos) / n_videos
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

    tier = cluster.get("tier", "tag")
    cross_validated = cluster.get("cross_validated", False)

    # Logistics scan + margin estimate
    logistics_pts, logistics_penalties, logistics_bonuses = logistics_check(cluster_text)
    est_margin = estimate_margin(category, price_tier, logistics_pts)
    margin_conf = margin_confidence(category, price_tier)

    # ----- Score components (0-100) -----
    if 5 <= n_creators <= 15:
        creator_pts = 25
    elif n_creators <= 4:
        creator_pts = 8 + n_creators * 2
    elif n_creators <= 25:
        creator_pts = 22 - (n_creators - 15)
    else:
        creator_pts = max(5, 15 - (n_creators - 25) // 2)

    eng_pts = min(25, avg_engagement * 5)

    raw_demand = (total_buy_intent * 2 + total_pos + total_questions
                  - total_neg * 2 - total_skep)
    demand_pts = max(0, min(25, raw_demand))

    # Fix: 'unknown' trajectory should not be rewarded
    freshness_pts = {"accelerating": 15, "fresh": 12, "mature": 7,
                     "cooling": 3, "stale": 0, "unknown": 0}.get(traj, 0)

    # Sellability bonus / penalty
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
    # Fix: penalize suspicious low-engagement videos (-2 each)
    sellability_pts -= 2 * low_eng_count
    # Cluster-tier confidence bonus: link > entity > tag
    sellability_pts += {"link": 5, "entity": 3, "tag": 0}.get(tier, 0)
    if cross_validated:
        sellability_pts += 3
    # Logistics flags (trademarks, fragile, hazmat, consumables, etc.)
    sellability_pts += logistics_pts
    sellability_pts = max(-15, min(20, sellability_pts))

    raw_score = creator_pts + eng_pts + demand_pts + freshness_pts + sellability_pts
    final_score = max(0, min(100, round(raw_score, 1)))

    # ----- Verdict (with overrides) -----
    overrides = []
    if sat == "saturated":
        verdict = "SATURATED"
        overrides.append("30+ creators promoting = late to the trend")
    elif total_neg > total_pos and total_neg >= 3:
        verdict = "AVOID"
        overrides.append(f"More negative comments ({total_neg}) than positive ({total_pos})")
    elif paid_count / max(n_videos, 1) >= 0.5:
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

    # Margin gate: a product with thin margins can't survive ad spend.
    # Only apply when we have enough signal for a reliable margin estimate.
    if margin_conf != "low" and not overrides:
        if verdict == "STRONG SELL" and est_margin < 0.45:
            verdict = "SELL"
            overrides.append(
                f"margin only {est_margin:.0%} - too thin for STRONG SELL"
            )
        elif verdict in ("SELL", "WATCH") and est_margin < MIN_VIABLE_MARGIN:
            verdict = "SKIP"
            overrides.append(
                f"margin {est_margin:.0%} below {MIN_VIABLE_MARGIN:.0%} viability"
                " - ads will eat the profit"
            )

    # ----- Reasoning -----
    reasons, concerns = [], []
    if tier == "link":
        reasons.append("clustered on shared shop link - same product confirmed")
    elif tier == "entity":
        reasons.append("clustered on extracted product entity (multiple creators name same product)")
    if cross_validated:
        reasons.append("product name appears in viewer comments (cross-validated)")
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
    if margin_conf in ("high", "medium") and est_margin >= 0.50:
        reasons.append(f"estimated margin {est_margin:.0%} - healthy ad-spend headroom")
    for br in logistics_bonuses[:3]:
        reasons.append(br)

    if tier == "tag":
        concerns.append("clustered only on hashtag - product identity not confirmed (could be mixed products)")
    if sat == "late":
        concerns.append(f"{n_creators} creators promoting - getting saturated")
    if traj == "cooling":
        concerns.append("trend is cooling (most videos >30 days old)")
    elif traj == "stale":
        concerns.append("trend is stale (median age >60 days)")
    elif traj == "unknown":
        concerns.append("no age data for videos - can't judge freshness")
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
    if margin_conf in ("high", "medium") and est_margin < MIN_VIABLE_MARGIN:
        concerns.append(
            f"estimated margin {est_margin:.0%} below {MIN_VIABLE_MARGIN:.0%} - "
            "ad-spend will eat profit"
        )
    for pr in logistics_penalties[:4]:
        concerns.append(pr)

    # Trademark risk is the single most severe flag - call it out explicitly
    has_trademark_risk = any("trademark" in r for r in logistics_penalties)

    return {
        "score": final_score,
        "verdict": verdict,
        "tier": tier,
        "cross_validated": cross_validated,
        "reasons": reasons[:6],
        "concerns": concerns[:6],
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
        "low_engagement_count": low_eng_count,
        "estimated_margin_pct": round(est_margin * 100, 1),
        "margin_confidence": margin_conf,
        "logistics_pts": logistics_pts,
        "logistics_flags": " | ".join(logistics_penalties + logistics_bonuses),
        "trademark_risk": has_trademark_risk,
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_verdicts_csv(scored_clusters, path):
    if not scored_clusters:
        return
    base_keys = [
        "signal", "tier", "cross_validated", "verdict", "score",
        "category", "price_tier", "price_evidence",
        "estimated_margin_pct", "margin_confidence",
        "logistics_pts", "trademark_risk", "logistics_flags",
        "saturation", "trajectory", "n_creators", "n_videos",
        "total_views", "total_likes", "total_shares", "total_saves",
        "total_buy_intent", "total_positive_sentiment", "total_negative_sentiment",
        "total_skeptical", "total_specific_questions",
        "avg_engagement_pct", "avg_save_ratio_pct", "avg_velocity_per_day",
        "paid_video_count", "micro_organic_count", "low_engagement_count",
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

    # Cluster-tier breakdown
    by_tier = {}
    for c in scored_clusters:
        by_tier.setdefault(c.get("tier", "tag"), []).append(c)
    if by_tier:
        lines.append("**Cluster tiers:**")
        for t in ("link", "entity", "tag"):
            if t in by_tier:
                lines.append(
                    f"- `{t}`: {len(by_tier[t])} clusters"
                    f"{' (strongest evidence)' if t == 'link' else ''}"
                    f"{' (mid-confidence)' if t == 'entity' else ''}"
                    f"{' (low-confidence, hashtag-only)' if t == 'tag' else ''}"
                )
        lines.append("")

    for v in order:
        if v not in by_verdict:
            continue
        lines.append(f"## {v}")
        lines.append("")
        for c in by_verdict[v]:
            tier_badge = f"`{c.get('tier','tag')}`"
            cv_badge = " · ✓cross-validated" if c.get("cross_validated") else ""
            lines.append(f"### {c['signal']} — score {c['score']} ({tier_badge}{cv_badge})")
            lines.append("")
            tm = " · ⚠ trademark risk" if c.get("trademark_risk") else ""
            lines.append(
                f"`{c['category']}` · price `{c['price_tier']}` "
                f"{c['price_evidence']} · margin "
                f"~{c.get('estimated_margin_pct', 0)}% "
                f"({c.get('margin_confidence', '?')}) · "
                f"{c['n_creators']} creators · "
                f"{c['n_videos']} videos · {c['total_views']:,} views · "
                f"`{c['saturation']}` · `{c['trajectory']}`{tm}"
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

    if not FUZZY_OK:
        print("[!] rapidfuzz not installed - fuzzy entity merging disabled "
              "('stanley tumbler' and 'stanleys tumbler' will stay separate)")
    if not WORDNINJA_OK:
        print("[!] wordninja not installed - concatenated hashtags ('#stanleycup') "
              "won't be decomposed")

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
    by_tier = {}
    for c in scored:
        by_tier[c["tier"]] = by_tier.get(c["tier"], 0) + 1
    tier_summary = ", ".join(f"{n} {t}" for t, n in by_tier.items())
    print(f"[+] {len(scored)} clusters by tier: {tier_summary}")
    print("\nTop 5 candidates:")
    for c in scored[:5]:
        cv = "✓" if c["cross_validated"] else " "
        tm = " ⚠TM" if c.get("trademark_risk") else "    "
        margin = c.get("estimated_margin_pct", 0)
        print(f"  [{c['verdict']:<12}] {c['score']:>5.1f} {cv}{tm} {c['tier']:<6} "
              f"m{margin:>4.0f}% {c['signal']:<30} {c['category']:<12} "
              f"{c['n_creators']}c/{c['n_videos']}v "
              f"{c['saturation']}/{c['trajectory']}")


if __name__ == "__main__":
    main()
