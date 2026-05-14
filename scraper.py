"""
Beginner-friendly TikTok scraper.

Usage:
    python scraper.py video <url>
    python scraper.py hashtag <tag>            e.g. skincare
    python scraper.py profile <username>       e.g. mrbeast (no @)
    python scraper.py viral <tag-or-@user>     full validation pipeline (recommended)

Flags (any position):
    --headless          run browser invisibly (TikTok blocks more often this way)
    --debug             save screenshot + page HTML to results/ for inspection
    --days N            only keep videos posted in the last N days (default 30, 0 = off)
    --comments          (viral mode) also scrape comments and score buy-intent
    --comments-top N    scrape comments only for top N candidate videos (default 15)

Outputs:
    results/<mode>_<target>_<timestamp>.json     all videos
    results/<mode>_<target>_<timestamp>.csv      same, flat
    results/winners_<target>_<timestamp>.csv     (viral mode) clustered winning products
"""

import asyncio
import csv
import json
import math
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

OUT_DIR = Path("results")
OUT_DIR.mkdir(exist_ok=True)

# List of (keyword, score) — higher score = stronger purchase signal.
# Score tiers: 4 = explicit commercial, 2 = moderate, 1 = weak/contextual
PRODUCT_KEYWORDS = [
    # 4-point: explicit purchase / commercial intent
    ("buy now", 4), ("shop now", 4), ("order now", 4), ("get yours", 4),
    ("add to cart", 4), ("use code", 4), ("discount code", 4),
    ("promo code", 4), ("coupon code", 4), ("link in bio", 4),
    ("shop link", 4), ("check link", 4), ("in my bio", 4), ("bio link", 4),
    ("tiktok shop", 4), ("tiktokshop", 4), ("tiktokmademebuyit", 4),
    ("amazon find", 4), ("available on amazon", 4), ("found on amazon", 4),
    ("it's on amazon", 4), ("its on amazon", 4),
    ("paid partnership", 4), ("gifted", 4), ("sponsored", 4),
    # 2-point: moderate commercial signals
    ("shop my", 2), ("shop the", 2), ("available at", 2),
    ("swipe up", 2), ("check my bio", 2), ("save this", 2),
    ("on sale", 2), ("free shipping", 2), ("limited time", 2),
    ("collab", 2), ("ambassador", 2), ("affiliate", 2),
    ("amazon", 2), ("shopify", 2),
    # 1-point: weak / contextual signals
    ("discount", 1), ("promo", 1), ("sale", 1), ("haul", 1),
    ("unboxing", 1), ("review", 1), ("must have", 1), ("must-have", 1),
    ("game changer", 1), ("obsessed", 1), ("affordable", 1),
]

# Dict of hashtag → score (4 = strong commercial, 2 = moderate, 1 = weak)
PRODUCT_HASHTAGS = {
    "tiktokmademebuyit": 4, "tiktokshop": 4, "amazonfinds": 4,
    "amazonmusthaves": 4, "founditonamazon": 4, "ad": 4, "sponsored": 4,
    "paidpartnership": 4, "gifted": 4,
    "productreview": 2, "musthaves": 2, "shophaul": 2, "unboxing": 2,
    "shoppinghaul": 2, "affordablefinds": 2, "targetfinds": 2,
    "shopwithme": 2, "amazondeal": 2, "amazondeals": 2,
    "haul": 1, "review": 1, "ootd": 1,
}

# Specific-enough keywords to be meaningful as cluster keys (exclude generic CTAs)
CLUSTER_KEYWORDS = {
    kw for kw, pts in PRODUCT_KEYWORDS
    if pts >= 4 and kw not in {
        "link in bio", "in my bio", "bio link", "check link", "shop link",
        "shop now", "buy now", "order now", "add to cart",
        "gifted", "sponsored", "paid partnership",
    }
}

# Shop link domains used for product detection and clustering
SHOP_DOMAINS = ["amazon.", "shopify", "shopee", "tiktok.com/t/",
                 "linktr.ee", "beacons.ai", "stan.store", "allmylinks.com"]

URL_RE = re.compile(r"https?://[^\s)]+", re.I)
HASHTAG_RE = re.compile(r"#(\w+)", re.U)


def pop_flag(name):
    if name in sys.argv:
        sys.argv.remove(name)
        return True
    return False


def pop_flag_value(name, default):
    if name in sys.argv:
        i = sys.argv.index(name)
        try:
            val = sys.argv[i + 1]
            del sys.argv[i:i + 2]
            return val
        except IndexError:
            del sys.argv[i:]
    return default


HEADLESS = pop_flag("--headless")
DEBUG = pop_flag("--debug")
SCRAPE_COMMENTS = pop_flag("--comments")
DAYS_FILTER = int(pop_flag_value("--days", "30"))
COMMENTS_TOP = int(pop_flag_value("--comments-top", "15"))


# (phrase, weight) — higher weight = stronger buyer intent signal
BUY_INTENT_PHRASES = [
    # 3-point: explicit purchase readiness
    ("where to buy", 3), ("where can i buy", 3), ("how do i buy", 3),
    ("drop the link", 3), ("send link", 3), ("link please", 3),
    ("just ordered", 3), ("already ordered", 3), ("i need this in my life", 3),
    ("need the link", 3), ("need a link", 3),
    # 2-point: strong curiosity / purchase intent
    ("link?", 2), ("drop a link", 2), ("where did you get", 2),
    ("where can i", 2), ("where to get", 2), ("what's it called", 2),
    ("what is this called", 2), ("i need this", 2), ("i need it", 2),
    ("i'm buying", 2), ("im buying", 2), ("bought it", 2),
    ("name?", 2), ("brand?", 2), ("what brand", 2), ("code?", 2),
    ("discount code", 2), ("drop name", 2), ("@ me", 2), ("tag me", 2),
    # 1-point: soft intent / social proof
    ("i want this", 1), ("i want it", 1), ("ordered", 1),
    ("got it", 1), ("obsessed", 1), ("what's the brand", 1),
    ("send me", 1), ("what is this", 1), ("what's this", 1),
]


def detect_product(caption, hashtags, links):
    cap = (caption or "").lower()
    score, matched = 0, []
    for kw, pts in PRODUCT_KEYWORDS:
        if kw in cap:
            score += pts
            matched.append(kw)
    for tag in hashtags:
        pts = PRODUCT_HASHTAGS.get(tag.lower(), 0)
        if pts:
            score += pts
            matched.append("#" + tag)
    for link in links:
        host = urlparse(link).netloc.lower()
        if any(s in host for s in SHOP_DOMAINS):
            score += 5
            matched.append(host)
    # bonus when 2+ independent signal types agree (cross-validation)
    signal_types = sum([
        any(kw in cap for kw, pts in PRODUCT_KEYWORDS if pts >= 4),
        any(PRODUCT_HASHTAGS.get(t.lower(), 0) >= 3 for t in hashtags),
        bool(links),
    ])
    if signal_types >= 2:
        score += 3
    return min(score, 30), matched  # cap to prevent runaway accumulation


def viral_score(views, likes, comments, shares, saves=0):
    if views <= 0:
        return 0.0
    # saves are the strongest signal (people bookmark to buy later)
    engagement = (likes + comments * 2 + shares * 3 + saves * 4) / max(views, 1)
    # log-scale view contribution so a 10M-view video doesn't drown a
    # 500k-view high-engagement video — log10(10M)=7, normalises to ~1.43
    view_component = math.log10(max(views, 1)) / 7.0
    return round(engagement * 100 + view_component * 10, 2)


def days_since(epoch_seconds):
    if not epoch_seconds:
        return None
    try:
        ts = int(epoch_seconds)
    except (TypeError, ValueError):
        return None
    age = (datetime.now().timestamp() - ts) / 86400.0
    return round(age, 1) if age >= 0 else None


def velocity(views, age_days):
    if not age_days or age_days <= 0 or views <= 0:
        return 0.0
    return round(views / age_days, 1)


def item_to_row(item, source_url=""):
    stats = item.get("stats", {}) or {}
    author = item.get("author", {}) or {}
    author_stats = item.get("authorStats", {}) or {}
    caption = item.get("desc", "") or ""
    hashtags = HASHTAG_RE.findall(caption)
    links = URL_RE.findall(caption)
    p_score, p_match = detect_product(caption, hashtags, links)
    vid = item.get("id", "")
    user = author.get("uniqueId", "")
    full_url = source_url or (f"https://www.tiktok.com/@{user}/video/{vid}" if user and vid else "")
    views = stats.get("playCount", 0)
    likes = stats.get("diggCount", 0)
    comments = stats.get("commentCount", 0)
    shares = stats.get("shareCount", 0)
    saves = stats.get("collectCount", 0) or stats.get("saveCount", 0)
    create_time = item.get("createTime", "")
    age = days_since(create_time)
    return {
        "url": full_url,
        "video_id": vid,
        "caption": caption,
        "create_time": create_time,
        "age_days": age if age is not None else "",
        "author_username": user,
        "author_nickname": author.get("nickname", ""),
        "author_followers": author_stats.get("followerCount", 0),
        "views": views,
        "likes": likes,
        "comments": comments,
        "shares": shares,
        "saves": saves,
        "velocity_views_per_day": velocity(views, age) if age else 0.0,
        "duration": (item.get("video") or {}).get("duration", 0),
        "music": (item.get("music") or {}).get("title", ""),
        "hashtags": ",".join(hashtags),
        "links": ",".join(links),
        "product_score": p_score,
        "product_signals": ",".join(p_match),
        "viral_score": viral_score(views, likes, comments, shares, saves),
        "comment_intent_score": 0,
        "comment_intent_samples": "",
        "comments_raw": "",
    }


PROFILE_DIR = Path(".browser_profile").resolve()
PROFILE_DIR.mkdir(exist_ok=True)


async def new_context(p):
    """Persistent browser profile so cookies + captcha solutions survive between runs."""
    ctx = await p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=HEADLESS,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        viewport={"width": 1366, "height": 900},
        locale="en-US",
        timezone_id="America/New_York",
    )
    await ctx.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )
    return ctx


async def dismiss_overlays(page):
    for sel in [
        'button:has-text("Accept all")',
        'button:has-text("Allow all")',
        'button:has-text("Reject all")',
        'button:has-text("Not now")',
        'button:has-text("Continue as guest")',
        'div[role="dialog"] button[aria-label="Close"]',
        'button[aria-label="Close"]',
    ]:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=500):
                await btn.click(timeout=1500)
                await page.wait_for_timeout(500)
        except Exception:
            pass


async def save_debug(page, label):
    ts = datetime.now().strftime("%H%M%S")
    safe = re.sub(r"[^\w\-]+", "_", label)[:40]
    png = OUT_DIR / f"debug_{safe}_{ts}.png"
    html = OUT_DIR / f"debug_{safe}_{ts}.html"
    try:
        await page.screenshot(path=str(png), full_page=True)
        content = await page.content()
        html.write_text(content, encoding="utf-8")
        print(f"[debug] saved {png.name} and {html.name}")
    except Exception as e:
        print(f"[debug] failed: {e}")


async def extract_inline_data(page):
    return await page.evaluate(
        """() => {
            const el = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__');
            return el ? el.textContent : null;
        }"""
    )


async def extract_video_meta(page, url):
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(2000)
    await dismiss_overlays(page)
    try:
        await page.wait_for_selector('script#__UNIVERSAL_DATA_FOR_REHYDRATION__', timeout=15000)
    except Exception:
        pass

    data = await extract_inline_data(page)
    if data:
        try:
            j = json.loads(data)
            scope = j.get("__DEFAULT_SCOPE__", {})
            detail = scope.get("webapp.video-detail") or {}
            item = detail.get("itemInfo", {}).get("itemStruct") or {}
            if item:
                return item_to_row(item, source_url=url)
        except json.JSONDecodeError:
            pass

    if DEBUG:
        await save_debug(page, "video_" + (urlparse(url).path or "x"))

    caption_loc = page.locator('h1[data-e2e="browse-video-desc"], h1[data-e2e="video-desc"]').first
    caption = ""
    try:
        caption = (await caption_loc.text_content(timeout=2000)) or ""
    except Exception:
        pass
    hashtags = HASHTAG_RE.findall(caption)
    links = URL_RE.findall(caption)
    p_score, p_match = detect_product(caption, hashtags, links)
    return {
        "url": url, "video_id": "", "caption": caption,
        "create_time": "", "age_days": "", "author_username": "", "author_nickname": "",
        "author_followers": 0, "views": 0, "likes": 0, "comments": 0, "shares": 0,
        "velocity_views_per_day": 0.0,
        "duration": 0, "music": "", "hashtags": ",".join(hashtags),
        "links": ",".join(links), "product_score": p_score,
        "product_signals": ",".join(p_match), "viral_score": 0.0,
        "comment_intent_score": 0, "comment_intent_samples": "",
    }


async def scrape_comments_for_video(page, url, max_comments=50):
    """Visit a video page, intercept comment API calls, return list of comment texts."""
    comments = []

    async def on_response(resp):
        if "/api/comment/list" not in resp.url:
            return
        try:
            body = await resp.json()
        except Exception:
            return
        for c in (body.get("comments") or []):
            txt = (c.get("text") or "").strip()
            if txt:
                comments.append(txt)

    handler = lambda r: asyncio.create_task(on_response(r))
    page.on("response", handler)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3000)
        await dismiss_overlays(page)
        for _ in range(6):
            if len(comments) >= max_comments:
                break
            await page.mouse.wheel(0, 4000)
            await page.wait_for_timeout(1500)
    finally:
        page.remove_listener("response", handler)
    return comments[:max_comments]


def score_comments(comments):
    """Score comment list for buy intent. Each comment contributes at most the
    weight of its highest-matching phrase (no double-counting per comment)."""
    if not comments:
        return 0, []
    score = 0
    samples = []
    for c in comments:
        cl = c.lower()
        best_weight = 0
        for phrase, weight in BUY_INTENT_PHRASES:
            if phrase in cl:
                best_weight = max(best_weight, weight)
        if best_weight:
            score += best_weight
            if len(samples) < 5:
                samples.append(c[:120])
    return score, samples


def find_items_in_json(obj, found):
    """Recursively walk a JSON blob and collect any video item-like dicts."""
    if isinstance(obj, dict):
        if "id" in obj and "desc" in obj and "stats" in obj and "author" in obj:
            vid = obj.get("id")
            if vid and vid not in found:
                found[vid] = obj
        for v in obj.values():
            find_items_in_json(v, found)
    elif isinstance(obj, list):
        for v in obj:
            find_items_in_json(v, found)


async def scrape_listing(target_url, max_videos):
    """Open the listing page, intercept TikTok's JSON API responses, and harvest items."""
    found = {}

    async with async_playwright() as p:
        ctx = await new_context(p)
        page = await ctx.new_page()

        async def on_response(resp):
            url = resp.url
            if "tiktok.com" not in url:
                return
            if not any(k in url for k in [
                "/api/challenge/item_list", "/api/post/item_list",
                "/api/recommend/item_list", "/api/search/item",
                "/api/related/item_list",
            ]):
                return
            try:
                body = await resp.json()
            except Exception:
                return
            before = len(found)
            find_items_in_json(body, found)
            gained = len(found) - before
            if gained:
                print(f"[net] +{gained} items from {url.split('?')[0].split('/')[-1]} (total {len(found)})")

        page.on("response", lambda r: asyncio.create_task(on_response(r)))

        try:
            await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)
            await dismiss_overlays(page)

            data = await extract_inline_data(page)
            if data:
                try:
                    find_items_in_json(json.loads(data), found)
                    if found:
                        print(f"[+] {len(found)} items from inline page data")
                except json.JSONDecodeError:
                    pass

            captcha_warned = False
            stable = 0
            last = -1
            max_wait_seconds = 180 if not HEADLESS else 30
            elapsed = 0
            tick = 2.2

            while len(found) < max_videos and elapsed < max_wait_seconds:
                if not found and not captcha_warned and elapsed >= 6:
                    print("[!] No items yet. If you see 'Something went wrong' or a captcha,")
                    print("    solve it / hit Refresh in the open browser. The script will keep waiting.")
                    captcha_warned = True

                await page.mouse.wheel(0, 6000)
                await page.wait_for_timeout(int(tick * 1000))
                elapsed += tick

                if len(found) == last:
                    stable += 1
                else:
                    stable = 0
                    last = len(found)

                if found and stable >= 6:
                    break

            if not found and DEBUG:
                await save_debug(page, "listing")

        finally:
            await ctx.close()

    items = list(found.values())[:max_videos]
    return [item_to_row(it) for it in items]


async def scrape_video(url):
    async with async_playwright() as p:
        ctx = await new_context(p)
        page = await ctx.new_page()
        try:
            return [await extract_video_meta(page, url)]
        finally:
            await ctx.close()


def save(results, mode, target):
    if not results:
        print("[!] No results to save.")
        return
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r"[^\w\-]+", "_", target)[:60]
    base = OUT_DIR / f"{mode}_{safe}_{ts}"

    with open(base.with_suffix(".json"), "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    keys = sorted({k for r in results for k in r.keys()})
    with open(base.with_suffix(".csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in results:
            w.writerow(r)

    print(f"[OK] Saved {len(results)} rows -> {base}.json / .csv")


def filter_viral_products(results, min_views=30_000, min_product_score=4,
                          max_age_days=0, min_velocity=800):
    """Keep videos that are both product-relevant and demonstrably viral.

    A video passes if it has a strong product signal AND either:
      - enough raw views (established viral), OR
      - high views/day velocity (early viral that hasn't peaked yet).
    """
    out = []
    for r in results:
        if r.get("product_score", 0) < min_product_score:
            continue
        views = r.get("views", 0)
        vel = r.get("velocity_views_per_day", 0)
        # pass if high absolute views OR fast-growing (catch early viral)
        if views < min_views and vel < min_velocity:
            continue
        if max_age_days > 0:
            age = r.get("age_days", "")
            if not isinstance(age, (int, float)) or age > max_age_days:
                continue
        out.append(r)
    return out


def canonicalize_link(link):
    """Strip query params and trailing path noise so the same shop link clusters together."""
    try:
        u = urlparse(link)
        host = u.netloc.lower().lstrip("www.")
        path = u.path.rstrip("/")
        return f"{host}{path}"
    except Exception:
        return link.lower()


def cluster_signals(rows):
    """Group videos by shared product signal. A signal = specific product hashtag,
    shop link, or high-specificity keyword.  Generic CTAs like 'link in bio' are
    excluded as cluster keys — they would mix unrelated products together."""
    clusters = {}

    def add(key, row):
        c = clusters.setdefault(key, {
            "signal": key, "videos": [], "creators": set(),
            "total_views": 0, "total_likes": 0, "total_comments": 0,
            "total_shares": 0, "total_intent": 0,
            "total_velocity": 0.0, "total_product_score": 0,
        })
        c["videos"].append(row)
        c["creators"].add(row.get("author_username", ""))
        c["total_views"] += row.get("views", 0)
        c["total_likes"] += row.get("likes", 0)
        c["total_comments"] += row.get("comments", 0)
        c["total_shares"] += row.get("shares", 0)
        c["total_intent"] += row.get("comment_intent_score", 0)
        c["total_velocity"] += row.get("velocity_views_per_day", 0)
        c["total_product_score"] += row.get("product_score", 0)

    for r in rows:
        # only cluster on hashtags with score >= 3 (strong commercial signal)
        for tag in (r.get("hashtags", "") or "").split(","):
            t = tag.strip().lower()
            if t and PRODUCT_HASHTAGS.get(t, 0) >= 3:
                add(f"#{t}", r)
        # cluster on shop links (most specific product identifier)
        for link in (r.get("links", "") or "").split(","):
            link = link.strip()
            if not link:
                continue
            host = urlparse(link).netloc.lower()
            if any(s in host for s in SHOP_DOMAINS):
                add(canonicalize_link(link), r)
        # cluster only on specific high-value keywords, not generic CTAs
        cap = (r.get("caption", "") or "").lower()
        for kw, pts in PRODUCT_KEYWORDS:
            if pts >= 4 and kw in cap and kw in CLUSTER_KEYWORDS:
                add(f"kw:{kw}", r)

    out = []
    for key, c in clusters.items():
        n_creators = len({u for u in c["creators"] if u})
        n_videos = len(c["videos"])
        # require at least 2 different creators AND 2 videos for a real trend
        if n_creators < 2 or n_videos < 2:
            continue
        avg_velocity = c["total_velocity"] / n_videos
        total_engagement = (c["total_likes"] + c["total_comments"] * 2
                            + c["total_shares"] * 3)
        avg_engagement_pct = total_engagement / max(c["total_views"], 1) * 100
        avg_product_score = c["total_product_score"] / n_videos
        win_score = round(
            n_creators * 15              # creator diversity = most reliable trend signal
            + n_videos * 3               # video volume (more creators making it = real)
            + (c["total_views"] / 50_000)  # total reach
            + avg_velocity * 0.05        # currently trending (views/day)
            + avg_engagement_pct * 2     # quality engagement (not just passive views)
            + avg_product_score * 2      # strength of product signals
            + c["total_intent"] * 8,     # buyers asking where-to-buy in comments
            2,
        )
        out.append({
            "signal": key,
            "unique_creators": n_creators,
            "videos": n_videos,
            "total_views": c["total_views"],
            "total_likes": c["total_likes"],
            "total_comments": c["total_comments"],
            "total_shares": c["total_shares"],
            "avg_velocity_per_day": round(avg_velocity, 1),
            "avg_engagement_pct": round(avg_engagement_pct, 3),
            "avg_product_score": round(avg_product_score, 1),
            "comment_intent_total": c["total_intent"],
            "win_score": win_score,
            "example_video_urls": " | ".join(v.get("url", "") for v in c["videos"][:3]),
            "example_captions": " || ".join(
                (v.get("caption", "") or "")[:100] for v in c["videos"][:3]
            ),
        })
    out.sort(key=lambda x: x["win_score"], reverse=True)
    return out


def save_winners(clusters, target):
    if not clusters:
        print("[!] No cross-creator product clusters found (try a broader hashtag or more videos).")
        return
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r"[^\w\-]+", "_", target)[:60]
    path = OUT_DIR / f"winners_{safe}_{ts}.csv"
    keys = list(clusters[0].keys())
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for c in clusters:
            w.writerow(c)
    print(f"[OK] Saved {len(clusters)} winning-product clusters -> {path}")
    print("\nTop candidates:")
    for c in clusters[:5]:
        print(f"  {c['win_score']:>8.1f}  {c['signal']:<40}  "
              f"{c['unique_creators']} creators, {c['total_views']:,} views, "
              f"vel={c['avg_velocity_per_day']}/day, eng={c['avg_engagement_pct']}%, "
              f"intent={c['comment_intent_total']}")


async def enrich_with_comments(rows, top_n):
    """Visit the top-N videos, scrape comments, attach intent score in-place."""
    if not rows:
        return
    targets = rows[:top_n]
    print(f"[+] Scraping comments for top {len(targets)} videos (this is the slow part)...")
    async with async_playwright() as p:
        ctx = await new_context(p)
        page = await ctx.new_page()
        try:
            for i, r in enumerate(targets, 1):
                url = r.get("url", "")
                if not url:
                    continue
                try:
                    comments = await scrape_comments_for_video(page, url, max_comments=50)
                    score, samples = score_comments(comments)
                    r["comment_intent_score"] = score
                    r["comment_intent_samples"] = " || ".join(samples)
                    r["comments_raw"] = " ||| ".join(comments)
                    print(f"  ({i}/{len(targets)}) intent={score} | {len(comments)} comments | {url}")
                except Exception as e:
                    print(f"  ({i}/{len(targets)}) failed: {e}")
        finally:
            await ctx.close()


def usage():
    print(__doc__)
    sys.exit(1)


async def main():
    if len(sys.argv) < 3:
        usage()
    mode, target = sys.argv[1].lower(), sys.argv[2]
    max_videos = int(sys.argv[3]) if len(sys.argv) > 3 else 30

    if mode == "video":
        results = await scrape_video(target)
    elif mode == "hashtag":
        url = f"https://www.tiktok.com/tag/{target.lstrip('#')}"
        results = await scrape_listing(url, max_videos)
    elif mode == "profile":
        url = f"https://www.tiktok.com/@{target.lstrip('@')}"
        results = await scrape_listing(url, max_videos)
    elif mode == "viral":
        if target.startswith("@"):
            url = f"https://www.tiktok.com/{target}"
        else:
            url = f"https://www.tiktok.com/tag/{target.lstrip('#')}"
        all_results = await scrape_listing(url, max_videos)
        results = filter_viral_products(all_results, max_age_days=DAYS_FILTER)
        results.sort(
            key=lambda r: (
                r.get("product_score", 0) * 3
                + r.get("velocity_views_per_day", 0) / 500
                + r.get("viral_score", 0)
            ),
            reverse=True,
        )
        print(f"[+] {len(results)} viral product videos out of {len(all_results)} "
              f"(filter: product>={4}, views>={30_000} OR velocity>={800}/day"
              + (f", age<={DAYS_FILTER}d)" if DAYS_FILTER else ")"))

        if SCRAPE_COMMENTS:
            await enrich_with_comments(results, COMMENTS_TOP)

        save(results, mode, target)
        clusters = cluster_signals(results)
        save_winners(clusters, target)
        return
    else:
        usage()

    save(results, mode, target)


if __name__ == "__main__":
    asyncio.run(main())
