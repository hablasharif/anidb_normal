"""
anidb.app Embed URL Scraper — GitHub Actions Edition
=====================================================
Reads config from environment variables (set via workflow inputs) with
sensible fallbacks so it can still be run locally.

Environment variables (all optional, fall back to defaults):
  ANIME_SLUG   – slug from anidb.app/anime/<slug>   [naruto-shippuden-3687]
  LANG_FILTER  – "all" | "eng" | "jpn"              [all]
  EP_START     – first episode number (inclusive)    [1]
  EP_END       – last episode number (inclusive)     [last episode]
  DELAY        – seconds between requests            [0.5]
  OUTPUT_FILE  – output JSON file path               [anidb_embeds.json]
  DEBUG        – "true" to enable verbose debug log  [false]

Run locally:
  python anidb_embed_scraper.py

Run with overrides:
  ANIME_SLUG=bleach-269 EP_START=1 EP_END=10 python anidb_embed_scraper.py
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
    from curl_cffi import requests                      # Chrome TLS fingerprint — bypasses Cloudflare JA3/JA4
    from curl_cffi.requests.exceptions import (
        ConnectionError as _ConnErr,
        Timeout         as _TimeoutErr,
    )
    CURL_CFFI = True
except ImportError:
    import requests                                     # fallback (will not bypass Cloudflare on GH Actions)
    _ConnErr    = requests.ConnectionError
    _TimeoutErr = requests.Timeout
    CURL_CFFI   = False

# ══════════════════════════════════════════
#  CONFIG  (env vars override these defaults)
# ══════════════════════════════════════════

ANIME_SLUG   = os.environ.get("ANIME_SLUG",   "naruto-shippuden-3687")
LANG_FILTER = os.environ.get("LANG_FILTER",  "all")
EP_START    = int(os.environ.get("EP_START", "1"))
_ep_end_raw = os.environ.get("EP_END",       "").strip()
EP_END      = int(_ep_end_raw) if _ep_end_raw else None
DELAY       = float(os.environ.get("DELAY",  "0.5"))
OUTPUT_FILE = os.environ.get("OUTPUT_FILE",  "anidb_embeds.json")
DEBUG        = os.environ.get("DEBUG", "false").lower() == "true"
SCRAPER_PROXY = os.environ.get("SCRAPER_PROXY", "").strip()  # e.g. "http://user:pass@host:port"

# ══════════════════════════════════════════

BASE_URL    = "https://anidb.app"
LOG_FILE    = "anidb_debug.log"
USER_AGENT  = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)

# ── Logging setup ─────────────────────────────────────────────────────────────

def setup_logging():
    """Configure logging: always write DEBUG to file; console level depends on DEBUG flag."""
    log = logging.getLogger("anidb")
    log.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    # File handler — always full DEBUG detail
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    log.addHandler(fh)

    # Console handler — INFO normally, DEBUG when DEBUG=true
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG if DEBUG else logging.INFO)
    ch.setFormatter(fmt)
    log.addHandler(ch)

    return log


log = setup_logging()


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def make_session() -> requests.Session:
    # curl_cffi: pass impersonate= so it uses Chrome's real TLS fingerprint.
    # Without this, Cloudflare rejects GH Actions IPs via JA3/JA4 fingerprinting.
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
        "Sec-Ch-Ua":                 '"Chromium";v="150", "Google Chrome";v="150", "Not:A-Brand";v="99"',
        "Sec-Ch-Ua-Mobile":          "?0",
        "Sec-Ch-Ua-Platform":        '"Windows"',
        "Sec-Fetch-Dest":            "document",
        "Sec-Fetch-Mode":            "navigate",
        "Sec-Fetch-Site":            "none",
        "Sec-Fetch-User":            "?1",
        "Upgrade-Insecure-Requests": "1",
        "DNT":                       "1",
    })
    if SCRAPER_PROXY:
        s.proxies = {"http": SCRAPER_PROXY, "https": SCRAPER_PROXY}
        log.info("Using proxy: %s", re.sub(r":[^:@]+@", ":***@", SCRAPER_PROXY))
    return s


def safe_get(session: requests.Session, url: str, label: str, **kwargs) -> requests.Response:
    """
    Wrapper around session.get() that:
      - logs request/response details at DEBUG level
      - retries once on 429 (rate limit)
      - raises a descriptive RuntimeError on any other HTTP error
    """
    log.debug("GET %s  kwargs=%s", url, kwargs.get("headers", {}))
    try:
        resp = session.get(url, **kwargs)
    except _ConnErr as exc:
        raise RuntimeError(
            f"[{label}] Connection failed for {url}\n"
            f"  Cause: {exc}\n"
            "  → Check your network / DNS, or the site may be down."
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
        return safe_get(session, url, label, **kwargs)  # single retry

    if resp.status_code == 403:
        raise RuntimeError(
            f"[{label}] 403 Forbidden — {url}\n"
            "  → The site may have blocked automated requests. "
            "Try increasing DELAY or updating the User-Agent."
        )

    if resp.status_code == 404:
        raise RuntimeError(
            f"[{label}] 404 Not Found — {url}\n"
            "  → Double-check ANIME_SLUG or the episode ID."
        )

    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        raise RuntimeError(
            f"[{label}] HTTP {resp.status_code} for {url}\n"
            f"  Body (first 500 chars): {resp.text[:500]}\n"
            f"  Cause: {exc}"
        ) from exc

    return resp


# ── Core scraping functions ───────────────────────────────────────────────────

def get_anime_id(session: requests.Session, slug: str) -> int:
    url  = f"{BASE_URL}/anime/{slug}"
    log.info("Fetching anime page: %s", url)
    resp = safe_get(session, url, "anime-page", headers={"Accept": "text/html"})

    # Detect Cloudflare / bot-block pages before the regex
    cf_indicators = [
        "cf-browser-verification",
        "cloudflare",
        "Just a moment",
        "Checking if the site connection is secure",
        "Enable JavaScript and cookies to continue",
        "DDoS protection by Cloudflare",
        "Ray ID",
    ]
    page_lower = resp.text.lower()
    cf_hit = [ind for ind in cf_indicators if ind.lower() in page_lower]
    if cf_hit:
        log.debug("Page HTML (first 3000 chars):\n%s", resp.text[:3000])
        raise RuntimeError(
            "Cloudflare / bot-detection challenge page received — the site is blocking GitHub Actions IPs.\n"
            f"  → Detected indicators: {cf_hit}\n"
            "  → Solutions:\n"
            "       1. Add SCRAPER_PROXY env var with a residential proxy URL (see README)\n"
            "       2. Use a self-hosted runner on a residential IP\n"
            "       3. Increase DELAY and retry — sometimes a single retry works\n"
            f"  → Tried slug: {slug}"
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
    return anime_id


def fetch_episodes(session: requests.Session, anime_id: int) -> list[dict]:
    url = f"{BASE_URL}/api/frontend/anime/{anime_id}/episodes"
    log.info("Fetching episode list …")
    resp = safe_get(session, url, "episode-list", headers={
        "Accept":           "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Referer":          f"{BASE_URL}/anime/{ANIME_SLUG}",
    })

    try:
        data = resp.json()
    except json.JSONDecodeError as exc:
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
            "  → The anime ID may be wrong, or the anime has no episodes listed yet.\n"
            f"  → Anime ID used: {anime_id}"
        )

    episodes.sort(key=lambda e: e["number"])
    log.info("Found %d episodes total", len(episodes))
    return episodes


def fetch_languages(session: requests.Session, ep_id: int) -> list[dict]:
    url = f"{BASE_URL}/api/frontend/episode/{ep_id}/languages"
    resp = safe_get(session, url, f"ep-{ep_id}-languages", headers={
        "Accept":           "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Referer":          f"{BASE_URL}/anime/{ANIME_SLUG}",
    })

    try:
        data = resp.json()
    except json.JSONDecodeError as exc:
        log.debug("Raw body for ep %d: %s", ep_id, resp.text[:500])
        raise RuntimeError(
            f"Languages API for episode {ep_id} returned non-JSON.\n"
            f"  Cause: {exc}"
        ) from exc

    langs = data.get("languages", [])
    log.debug("  ep_id=%d  langs=%s", ep_id, [l.get("code") for l in langs])
    return langs


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> list[dict]:
    log.info("══════════════════════════════════════════")
    log.info(" AniDB Embed Scraper — GitHub Actions")
    log.info("══════════════════════════════════════════")
    log.info("Config:")
    log.info("  ANIME_SLUG  = %s", ANIME_SLUG)
    log.info("  LANG_FILTER = %s", LANG_FILTER)
    log.info("  EP_START    = %d", EP_START)
    log.info("  EP_END      = %s", EP_END if EP_END is not None else "(last)")
    log.info("  DELAY       = %.1fs", DELAY)
    log.info("  OUTPUT_FILE = %s", OUTPUT_FILE)
    log.info("  DEBUG       = %s", DEBUG)
    log.info("  LOG_FILE    = %s", LOG_FILE)
    log.info("")

    session  = make_session()
    anime_id = get_anime_id(session, ANIME_SLUG)
    all_eps  = fetch_episodes(session, anime_id)

    ep_end_eff = EP_END if EP_END is not None else all_eps[-1]["number"]
    episodes   = [e for e in all_eps if EP_START <= e["number"] <= ep_end_eff]

    if not episodes:
        raise RuntimeError(
            f"No episodes found in range {EP_START}–{ep_end_eff}.\n"
            f"  Available range: {all_eps[0]['number']}–{all_eps[-1]['number']}"
        )

    log.info("Scraping episodes %d–%d (%d episodes)", EP_START, ep_end_eff, len(episodes))

    results      = []
    failed_eps   = []

    for i, ep in enumerate(episodes, 1):
        ep_num = ep["number"]
        ep_id  = ep["id"]
        filler = ep.get("filler", False)
        label  = f"Ep {ep_num:>4}" + (" [FILLER]" if filler else "")

        # Progress (CI-friendly: no \r, just periodic lines)
        if i == 1 or i % 10 == 0 or i == len(episodes):
            log.info("  Progress: %d/%d  %s", i, len(episodes), label)

        try:
            langs = fetch_languages(session, ep_id)
        except RuntimeError as exc:
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
            log.info("Failure details also saved to: %s  (field: 'failures')", OUTPUT_FILE)

    log.info("Debug log written to: %s", LOG_FILE)

    # Exit with non-zero code if any episodes failed — makes GH Actions mark the step ✗
    if failed_eps and len(failed_eps) == len(episodes):
        sys.exit(1)   # total failure

    return results


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:                          # top-level catch for debug output
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
        log.error("  DEBUG       = %s", DEBUG)
        log.error("")
        log.error("→ Full debug log saved to: %s", LOG_FILE)
        log.error("→ Download it from the GitHub Actions 'Artifacts' section.")
        sys.exit(1)
