"""
anidb.app Embed URL Scraper — GitHub Actions Edition
=====================================================
Reads config from environment variables (set via workflow inputs) with
sensible fallbacks so it can still be run locally.

Environment variables (all optional, fall back to defaults):
  ANIME_SLUG        – slug from anidb.app/anime/<slug>         [naruto-shippuden-3687]
  LANG_FILTER       – "all" | "eng" | "jpn"                    [all]
  EP_START          – first episode number (inclusive)          [1]
  EP_END            – last episode number (inclusive)           [last episode]
  DELAY             – seconds between requests                  [0.5]
  OUTPUT_FILE       – output JSON file path                     [anidb_embeds.json]
  DEBUG             – "true" to enable verbose debug log        [false]
  SCRAPER_PROXY     – single proxy  http://user:pass@host:port  [none]
  SCRAPER_PROXY_LIST– comma-separated proxies, rotated on 403  [none]

Run locally:
  python anidb_embed_scraper.py

Run with proxy list:
  SCRAPER_PROXY_LIST="http://u:p@h1:p1,http://u:p@h2:p2" python anidb_embed_scraper.py
"""

import json
import logging
import os
import re
import sys
import time
import traceback
from pathlib import Path

try:
    from curl_cffi import requests                  # Chrome TLS fingerprint — bypasses Cloudflare JA3/JA4
    from curl_cffi.requests.exceptions import (
        ConnectionError as _ConnErr,
        Timeout         as _TimeoutErr,
    )
    CURL_CFFI = True
except ImportError:
    import requests                                 # fallback (will not bypass Cloudflare on GH Actions)
    _ConnErr    = requests.ConnectionError
    _TimeoutErr = requests.Timeout
    CURL_CFFI   = False

# ══════════════════════════════════════════
#  CONFIG  (env vars override these defaults)
# ══════════════════════════════════════════

ANIME_SLUG   = os.environ.get("ANIME_SLUG",   "naruto-shippuden-3687")
LANG_FILTER  = os.environ.get("LANG_FILTER",  "all")
EP_START     = int(os.environ.get("EP_START", "1"))
_ep_end_raw  = os.environ.get("EP_END", "").strip()
EP_END       = int(_ep_end_raw) if _ep_end_raw else None
DELAY        = float(os.environ.get("DELAY",  "0.5"))
OUTPUT_FILE  = os.environ.get("OUTPUT_FILE",  "anidb_embeds.json")
DEBUG        = os.environ.get("DEBUG", "false").lower() == "true"

# Proxy config — SCRAPER_PROXY_LIST takes priority; falls back to SCRAPER_PROXY
_proxy_list_raw = os.environ.get("SCRAPER_PROXY_LIST", "").strip()
_proxy_single   = os.environ.get("SCRAPER_PROXY",      "").strip()

if _proxy_list_raw:
    PROXY_LIST = [p.strip() for p in _proxy_list_raw.split(",") if p.strip()]
elif _proxy_single:
    PROXY_LIST = [_proxy_single]
else:
    PROXY_LIST = []

# ══════════════════════════════════════════

BASE_URL   = "https://anidb.app"
LOG_FILE   = "anidb_debug.log"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ── Logging setup ─────────────────────────────────────────────────────────────

def setup_logging():
    log = logging.getLogger("anidb")
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    log.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG if DEBUG else logging.INFO)
    ch.setFormatter(fmt)
    log.addHandler(ch)
    return log


log = setup_logging()


# ── Proxy helpers ─────────────────────────────────────────────────────────────

def _mask(proxy_url: str) -> str:
    """Hide password in proxy URL for safe logging."""
    return re.sub(r":[^:@/]+@", ":***@", proxy_url)


def make_session(proxy: str = "") -> requests.Session:
    """
    Build a requests/curl_cffi session.
    curl_cffi impersonates Chrome 124 TLS fingerprint — bypasses Cloudflare JA3/JA4.
    proxy: full URL like http://user:pass@host:port
    """
    try:
        s = requests.Session(impersonate="chrome124")
    except TypeError:
        s = requests.Session()  # plain requests fallback

    s.headers.update({
        "User-Agent":                USER_AGENT,
        "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language":           "en-US,en;q=0.9",
        "Accept-Encoding":           "gzip, deflate, br",
        "Cache-Control":             "no-cache",
        "Pragma":                    "no-cache",
        "Sec-Ch-Ua":                 '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "Sec-Ch-Ua-Mobile":          "?0",
        "Sec-Ch-Ua-Platform":        '"Windows"',
        "Sec-Fetch-Dest":            "document",
        "Sec-Fetch-Mode":            "navigate",
        "Sec-Fetch-Site":            "none",
        "Sec-Fetch-User":            "?1",
        "Upgrade-Insecure-Requests": "1",
        "DNT":                       "1",
    })

    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
        log.info("  Session proxy: %s", _mask(proxy))

    return s


# ── HTTP helper ───────────────────────────────────────────────────────────────

def safe_get(session, url: str, label: str, **kwargs):
    """
    GET with error handling:
      - retries once on 429 (rate limit)
      - raises RuntimeError with clear message on 403/404/other errors
    """
    log.debug("GET %s", url)
    try:
        resp = session.get(url, timeout=30, **kwargs)
    except _ConnErr as exc:
        raise RuntimeError(
            f"[{label}] Connection failed for {url}\n"
            f"  Cause: {exc}\n"
            "  → Check proxy settings or network."
        ) from exc
    except _TimeoutErr as exc:
        raise RuntimeError(
            f"[{label}] Request timed out for {url}\n"
            f"  Cause: {exc}"
        ) from exc

    log.debug(
        "  Response: %d  Content-Type: %s  Size: %d bytes",
        resp.status_code,
        resp.headers.get("Content-Type", "?"),
        len(resp.content),
    )

    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", 10))
        log.warning("[%s] Rate limited (429). Sleeping %ds …", label, retry_after)
        time.sleep(retry_after)
        return safe_get(session, url, label, **kwargs)

    if resp.status_code == 403:
        raise RuntimeError(
            f"[{label}] 403 Forbidden — {url}\n"
            "  → IP is blocked. Proxy will be rotated if available."
        )

    if resp.status_code == 404:
        raise RuntimeError(
            f"[{label}] 404 Not Found — {url}\n"
            "  → Double-check ANIME_SLUG or the episode ID."
        )

    try:
        resp.raise_for_status()
    except Exception as exc:
        raise RuntimeError(
            f"[{label}] HTTP {resp.status_code} for {url}\n"
            f"  Body (first 500 chars): {resp.text[:500]}\n"
            f"  Cause: {exc}"
        ) from exc

    return resp


# ── Proxy-rotating request ────────────────────────────────────────────────────

def get_with_proxy_rotation(url: str, label: str, extra_headers: dict = None) -> tuple:
    """
    Try each proxy in PROXY_LIST in order.
    Falls back to no-proxy if list is empty or all fail.
    Returns (response, session) of the first successful attempt.
    """
    kwargs = {}
    if extra_headers:
        kwargs["headers"] = extra_headers

    proxies_to_try = PROXY_LIST if PROXY_LIST else [""]  # "" = no proxy

    last_exc = None
    for i, proxy in enumerate(proxies_to_try):
        attempt_label = f"proxy {i+1}/{len(proxies_to_try)}" if proxy else "no proxy"
        log.info("  Trying %s …", attempt_label if not proxy else f"{attempt_label} ({_mask(proxy)})")

        session = make_session(proxy)
        try:
            resp = safe_get(session, url, label, **kwargs)
            log.info("  ✓ Success with %s", attempt_label)
            return resp, session
        except RuntimeError as exc:
            err_str = str(exc)
            log.warning("  ✗ Failed (%s): %s", attempt_label, err_str.split("\n")[0])
            last_exc = exc
            if i < len(proxies_to_try) - 1:
                log.info("  Rotating to next proxy …")
                time.sleep(1)
            continue

    raise RuntimeError(
        f"All {len(proxies_to_try)} proxy attempt(s) failed for {url}.\n"
        f"  Last error: {last_exc}"
    )


# ── Core scraping functions ───────────────────────────────────────────────────

def get_anime_id(slug: str) -> tuple:
    """Returns (anime_id, session) — reuses the session that worked."""
    url = f"{BASE_URL}/anime/{slug}"
    log.info("Fetching anime page: %s", url)

    resp, session = get_with_proxy_rotation(
        url, "anime-page",
        extra_headers={"Accept": "text/html"}
    )

    # Detect Cloudflare challenge page
    cf_indicators = [
        "cf-browser-verification",
        "just a moment",
        "checking if the site connection is secure",
        "enable javascript and cookies to continue",
        "ddos protection by cloudflare",
    ]
    page_lower = resp.text.lower()
    cf_hit = [ind for ind in cf_indicators if ind in page_lower]
    if cf_hit:
        log.debug("Page HTML (first 3000 chars):\n%s", resp.text[:3000])
        raise RuntimeError(
            "Cloudflare challenge page received — TLS fingerprint passed but JS challenge active.\n"
            f"  → Indicators: {cf_hit}\n"
            "  → Try adding more residential proxies to SCRAPER_PROXY_LIST."
        )

    m = re.search(r"watchPage\((\d+)", resp.text)
    if not m:
        log.debug("Page HTML (first 2000 chars):\n%s", resp.text[:2000])
        raise RuntimeError(
            "Could not find anime ID in the page source.\n"
            "  → The slug may be wrong, or the site's HTML structure changed.\n"
            f"  → Tried slug: {slug}\n"
            "  → Enable DEBUG=true and check anidb_debug.log for the raw HTML."
        )

    anime_id = int(m.group(1))
    log.info("Anime ID: %d", anime_id)
    return anime_id, session


def fetch_episodes(session, anime_id: int) -> list:
    url = f"{BASE_URL}/api/frontend/anime/{anime_id}/episodes"
    log.info("Fetching episode list …")
    resp = safe_get(session, url, "episode-list", headers={
        "Accept":           "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Referer":          f"{BASE_URL}/anime/{ANIME_SLUG}",
    })

    try:
        data = resp.json()
    except Exception as exc:
        log.debug("Raw body: %s", resp.text[:1000])
        raise RuntimeError(
            f"Episode list API returned non-JSON.\n"
            f"  Body (first 500 chars): {resp.text[:500]}\n"
            f"  Cause: {exc}"
        ) from exc

    episodes = data.get("episodes", [])
    if not episodes:
        raise RuntimeError(
            "Episode list is empty.\n"
            f"  → Anime ID used: {anime_id}"
        )

    episodes.sort(key=lambda e: e["number"])
    log.info("Found %d episodes total", len(episodes))
    return episodes


def fetch_languages(session, ep_id: int) -> list:
    url = f"{BASE_URL}/api/frontend/episode/{ep_id}/languages"
    resp = safe_get(session, url, f"ep-{ep_id}-languages", headers={
        "Accept":           "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Referer":          f"{BASE_URL}/anime/{ANIME_SLUG}",
    })

    try:
        data = resp.json()
    except Exception as exc:
        log.debug("Raw body for ep %d: %s", ep_id, resp.text[:500])
        raise RuntimeError(
            f"Languages API for episode {ep_id} returned non-JSON.\n"
            f"  Cause: {exc}"
        ) from exc

    langs = data.get("languages", [])
    log.debug("  ep_id=%d  langs=%s", ep_id, [l.get("code") for l in langs])
    return langs


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> list:
    log.info("══════════════════════════════════════════")
    log.info(" AniDB Embed Scraper — GitHub Actions")
    log.info("══════════════════════════════════════════")
    log.info("Config:")
    log.info("  ANIME_SLUG   = %s", ANIME_SLUG)
    log.info("  LANG_FILTER  = %s", LANG_FILTER)
    log.info("  EP_START     = %d", EP_START)
    log.info("  EP_END       = %s", EP_END if EP_END is not None else "(last)")
    log.info("  DELAY        = %.1fs", DELAY)
    log.info("  OUTPUT_FILE  = %s", OUTPUT_FILE)
    log.info("  DEBUG        = %s", DEBUG)
    log.info("  LOG_FILE     = %s", LOG_FILE)
    log.info("  CURL_CFFI    = %s", CURL_CFFI)
    if PROXY_LIST:
        log.info("  PROXIES      = %d configured", len(PROXY_LIST))
        for i, p in enumerate(PROXY_LIST, 1):
            log.info("    [%d] %s", i, _mask(p))
    else:
        log.info("  PROXIES      = none (direct connection)")
    log.info("")

    anime_id, session = get_anime_id(ANIME_SLUG)
    all_eps = fetch_episodes(session, anime_id)

    ep_end_eff = EP_END if EP_END is not None else all_eps[-1]["number"]
    episodes   = [e for e in all_eps if EP_START <= e["number"] <= ep_end_eff]

    if not episodes:
        raise RuntimeError(
            f"No episodes found in range {EP_START}–{ep_end_eff}.\n"
            f"  Available range: {all_eps[0]['number']}–{all_eps[-1]['number']}"
        )

    log.info("Scraping episodes %d–%d (%d episodes)", EP_START, ep_end_eff, len(episodes))

    results    = []
    failed_eps = []

    for i, ep in enumerate(episodes, 1):
        ep_num = ep["number"]
        ep_id  = ep["id"]
        filler = ep.get("filler", False)
        label  = f"Ep {ep_num:>4}" + (" [FILLER]" if filler else "")

        if i == 1 or i % 10 == 0 or i == len(episodes):
            log.info("  Progress: %d/%d  %s", i, len(episodes), label)

        try:
            langs = fetch_languages(session, ep_id)
        except RuntimeError as exc:
            # On 403 mid-scrape, try rotating the proxy for this episode
            if "403" in str(exc) and PROXY_LIST:
                log.warning("  403 mid-scrape on %s — attempting proxy rotation …", label)
                try:
                    url = f"{BASE_URL}/api/frontend/episode/{ep_id}/languages"
                    resp, session = get_with_proxy_rotation(
                        url, f"ep-{ep_id}-languages-retry",
                        extra_headers={
                            "Accept":           "application/json",
                            "X-Requested-With": "XMLHttpRequest",
                            "Referer":          f"{BASE_URL}/anime/{ANIME_SLUG}",
                        }
                    )
                    data  = resp.json()
                    langs = data.get("languages", [])
                    log.info("  ✓ Recovered %s via proxy rotation", label)
                except Exception as retry_exc:
                    log.error("  FAILED (after rotation): %s — %s", label, retry_exc)
                    failed_eps.append({"episode": ep_num, "ep_id": ep_id, "error": str(retry_exc)})
                    time.sleep(DELAY)
                    continue
            else:
                log.error("  FAILED: %s — %s", label, exc)
                failed_eps.append({"episode": ep_num, "ep_id": ep_id, "error": str(exc)})
                time.sleep(DELAY)
                continue

        time.sleep(DELAY)

        if LANG_FILTER != "all":
            langs = [l for l in langs if l.get("code") == LANG_FILTER]

        results.append({
            "episode":   ep_num,
            "ep_id":     ep_id,
            "filler":    filler,
            "languages": langs,
        })

    # ── Summary ───────────────────────────────────────────────────────────────
    log.info("")
    log.info("=" * 65)
    for r in results:
        filler_tag = " [FILLER]" if r["filler"] else ""
        log.info("Episode %d%s  (id=%d)", r["episode"], filler_tag, r["ep_id"])
        for lang in r["languages"]:
            log.info("  [%s]  %s", lang.get("code", "?"), lang.get("embed_url", "N/A"))
    log.info("=" * 65)

    if failed_eps:
        log.warning("")
        log.warning("⚠ %d episode(s) failed:", len(failed_eps))
        for f in failed_eps:
            log.warning("  Ep %d (id=%d): %s", f["episode"], f["ep_id"], f["error"])

    # ── Save JSON ─────────────────────────────────────────────────────────────
    if OUTPUT_FILE:
        output = {
            "anime_slug":  ANIME_SLUG,
            "lang_filter": LANG_FILTER,
            "ep_range":    [EP_START, ep_end_eff],
            "scraped":     len(results),
            "failed":      len(failed_eps),
            "results":     results,
            "failures":    failed_eps,
        }
        Path(OUTPUT_FILE).write_text(
            json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        log.info("")
        log.info("Saved %d episodes → %s", len(results), OUTPUT_FILE)
        if failed_eps:
            log.info("Failure details also in: %s  (field: 'failures')", OUTPUT_FILE)

    log.info("Debug log written to: %s", LOG_FILE)

    if failed_eps and len(failed_eps) == len(episodes):
        sys.exit(1)  # total failure — mark Actions step as ✗

    return results


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log.error("")
        log.error("╔══════════════════════════════════════════╗")
        log.error("║          SCRAPER FAILED — DEBUG          ║")
        log.error("╚══════════════════════════════════════════╝")
        log.error("Error: %s", exc)
        log.error("")
        log.error("Full traceback:")
        log.error(traceback.format_exc())
        log.error("")
        log.error("Config at time of failure:")
        log.error("  ANIME_SLUG  = %s", ANIME_SLUG)
        log.error("  LANG_FILTER = %s", LANG_FILTER)
        log.error("  EP_START    = %d", EP_START)
        log.error("  EP_END      = %s", EP_END)
        log.error("  DELAY       = %s", DELAY)
        log.error("  PROXIES     = %d", len(PROXY_LIST))
        log.error("  DEBUG       = %s", DEBUG)
        log.error("")
        log.error("→ Full debug log saved to: %s", LOG_FILE)
        log.error("→ Download it from the GitHub Actions 'Artifacts' section.")
        sys.exit(1)
