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
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

OUT_DIR = Path("results")
OUT_DIR.mkdir(exist_ok=True)

PRODUCT_KEYWORDS = [
    "link in bio", "shop now", "buy now", "available at", "use code",
    "discount", "promo", "amazon", "shopify", "shop my", "tiktokshop",
    "tiktok shop", "tiktokmademebuyit", "tiktok made me buy",
]
PRODUCT_HASHTAGS = {
    "tiktokmademebuyit", "tiktokshop", "amazonfinds", "amazonmusthaves",
    "musthaves", "founditonamazon", "productreview", "ad", "sponsored",
}
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


BUY_INTENT_PHRASES = [
    "link?", "link please", "drop the link", "drop a link", "where did you get",
    "where can i", "where to buy", "where to get", "what's it called",
    "what is this called", "what is this", "i need this", "i need it",
    "i want this", "i want it", "i'm buying", "im buying", "ordered",
    "just ordered", "bought it", "got it", "obsessed", "name?", "brand?",
    "what brand", "what's the brand", "code?", "discount code", "@ me",
    "tag me", "send link", "drop name",
]


def detect_product(caption, hashtags, links):
    cap = (caption or "").lower()
    score, matched = 0, []
    for kw in PRODUCT_KEYWORDS:
        if kw in cap:
            score += 2
            matched.append(kw)
    for tag in hashtags:
        if tag.lower() in PRODUCT_HASHTAGS:
            score += 2
            matched.append("#" + tag)
    for link in links:
        host = urlparse(link).netloc.lower()
        if any(s in host for s in ["amazon.", "shopify", "shopee", "tiktok.com/t/", "linktr.ee", "beacons.ai"]):
            score += 3
            matched.append(host)
    return score, matched


def viral_score(views, likes, comments, shares):
    if views <= 0:
        return 0.0
    engagement = (likes + comments * 2 + shares * 3) / max(views, 1)
    return round(engagement * 100 + (views / 100_000), 2)


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
        "velocity_views_per_day": velocity(views, age) if age else 0.0,
        "duration": (item.get("video") or {}).get("duration", 0),
        "music": (item.get("music") or {}).get("title", ""),
        "hashtags": ",".join(hashtags),
        "links": ",".join(links),
        "product_score": p_score,
        "product_signals": ",".join(p_match),
        "viral_score": viral_score(views, likes, comments, shares),
        "comment_intent_score": 0,
        "comment_intent_samples": "",
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
    if not comments:
        return 0, []
    score = 0
    samples = []
    for c in comments:
        cl = c.lower()
        for phrase in BUY_INTENT_PHRASES:
            if phrase in cl:
                score += 1
                if len(samples) < 5:
                    samples.append(c[:120])
                break
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


def filter_viral_products(results, min_views=50_000, min_product_score=2, max_age_days=0):
    out = []
    for r in results:
        if r.get("views", 0) < min_views:
            continue
        if r.get("product_score", 0) < min_product_score:
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
    """Group videos by shared product signal. A signal = product hashtag, shop link, or product keyword."""
    clusters = {}

    def add(key, row):
        c = clusters.setdefault(key, {
            "signal": key, "videos": [], "creators": set(),
            "total_views": 0, "total_likes": 0, "total_comments": 0,
            "total_shares": 0, "total_intent": 0,
        })
        c["videos"].append(row)
        c["creators"].add(row.get("author_username", ""))
        c["total_views"] += row.get("views", 0)
        c["total_likes"] += row.get("likes", 0)
        c["total_comments"] += row.get("comments", 0)
        c["total_shares"] += row.get("shares", 0)
        c["total_intent"] += row.get("comment_intent_score", 0)

    for r in rows:
        for tag in (r.get("hashtags", "") or "").split(","):
            t = tag.strip().lower()
            if t and t in PRODUCT_HASHTAGS:
                add(f"#{t}", r)
        for link in (r.get("links", "") or "").split(","):
            link = link.strip()
            if not link:
                continue
            host = urlparse(link).netloc.lower()
            if any(s in host for s in ["amazon.", "shopify", "shopee", "tiktok.com/t/", "linktr.ee", "beacons.ai"]):
                add(canonicalize_link(link), r)
        cap = (r.get("caption", "") or "").lower()
        for kw in PRODUCT_KEYWORDS:
            if kw in cap and kw not in ("amazon", "shopify"):
                add(f"kw:{kw}", r)

    out = []
    for key, c in clusters.items():
        n_creators = len({u for u in c["creators"] if u})
        if n_creators < 2:
            continue
        n_videos = len(c["videos"])
        win_score = round(
            n_creators * 10
            + (c["total_views"] / 100_000)
            + c["total_intent"] * 5,
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
            "comment_intent_total": c["total_intent"],
            "win_score": win_score,
            "example_video_urls": " | ".join(v.get("url", "") for v in c["videos"][:3]),
            "example_captions": " || ".join((v.get("caption", "") or "")[:100] for v in c["videos"][:3]),
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
        print(f"  {c['win_score']:>8.1f}  {c['signal']:<40}  {c['unique_creators']} creators, "
              f"{c['total_views']:,} views, intent={c['comment_intent_total']}")


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
                r.get("product_score", 0),
                r.get("velocity_views_per_day", 0),
                r.get("viral_score", 0),
            ),
            reverse=True,
        )
        print(f"[+] {len(results)} viral product videos out of {len(all_results)} "
              f"(filter: views>=50k, product>=2, age<={DAYS_FILTER}d)" if DAYS_FILTER else
              f"[+] {len(results)} viral product videos out of {len(all_results)}")

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
