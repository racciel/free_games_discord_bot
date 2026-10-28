"""
Free Game Watcher
Monitors limited-time, free-to-keep game offers and shows Windows notifications.

Sources:
- Epic Games Store public promotions endpoint (official storefront data)
- SteamDB's free-promotion page (public page; markup may change)
- GamerPower's public API (candidate discovery for supported platforms)
- Humble Bundle's public giveaway page (best-effort; markup may change)

This intentionally errs on the side of missing an ambiguous offer rather than
alerting for free weekends, subscriptions, free-to-play games, demos, or DLC.
It never logs into stores or claims games automatically.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from winotify import Notification, audio

APP_NAME = "Free Game Watcher"
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "free_games.sqlite3"
LOG_PATH = BASE_DIR / "free_game_watcher.log"
CONFIG_PATH = BASE_DIR / "config.json"

DEFAULT_CONFIG = {
    "check_every_minutes": 180,
    "country": "GB",
    "notify_on_first_run": True,
    "sources": {
        "epic": True,
        "steamdb": True,
        "gamerpower": True,
        "humble": True
    }
}

HEADERS = {
    "User-Agent": "FreeGameWatcher/1.0 (personal-use; contact: local-user)",
    "Accept": "application/json,text/html,*/*"
}
TIMEOUT = 20

# Deliberately strict. Ambiguous wording does not qualify.
REJECT_WORDS = (
    "free weekend", "free-to-play weekend", "play for free", "free to play",
    "free trial", "trial weekend", "game pass", "xbox game pass",
    "playstation plus", "subscription required", "prime gaming subscription",
    "demo", "playtest", "beta access", "beta key", "prologue",
    "dlc", "soundtrack", "in-game", "loot", "skin", "starter pack",
    "free access", "temporary access", "limited-time access"
)

KEEP_PHRASES = (
    "free to keep", "free-to-keep", "keep forever", "yours to keep",
    "permanently keep", "permanent copy", "add to your library for free",
    "free copy to keep", "claim to keep"
)

BAD_GIVEAWAY_TERMS = (
    "alienware", "arena", "arp", "gleam", "steelseries", "razer",
    "key giveaway", "claim key", "redeem code", "enter code",
    "create account", "link account", "points", "level", "task",
    "social media", "wallet", "survey"
)

BAD_TITLE_WORDS = (
    "demo", "playtest", "beta", "prologue", "soundtrack", "season pass",
    "starter pack", "dlc", "skin pack", "test server"
)

# Specific sites/systems that require accounts, points, or task completion
BAD_THIRD_PARTY_SITES = (
    "alienware", "alienwarearena", "arena.alienwarefortress.com", 
    "gleam.io", "gleam", "steelseries", "razer", "keyhub", 
    "lootboy", "prizes.game", "gamehag"
)

# Words indicating account/point walls
BAD_REQUIREMENT_TERMS = (
    "arp", "level", "points", "task", "social media", 
    "create account", "link account", "survey"
)


@dataclass(frozen=True)
class Offer:
    store: str
    title: str
    url: str
    end_date: str = ""
    source: str = ""
    details: str = ""

    @property
    def key(self) -> str:
        canonical_url = self.url.split("?")[0].rstrip("/").lower()
        raw = f"{self.store.lower()}|{self.title.casefold()}|{canonical_url}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def normalize_title(title: str) -> str:
    """Strip store tags, subtitles, and giveaway suffixes for foolproof deduplication."""
    # Convert to lowercase first
    t = title.casefold()
    # Remove anything in parentheses or brackets like (Epic Games) Giveaway
    t = re.sub(r"\s*[\(\[\{].*?[\)\]\}]", "", t)
    # If there's a subtitle separated by a colon or dash, cut it to match core titles 
    # (e.g., "Home - A Unique Horror Adventure" -> "home")
    if " - " in t:
        t = t.split(" - ")[0]
    if ":" in t:
        t = t.split(":")[0]
    # Strip trailing promotional words
    t = re.sub(r"\s+(giveaway|key|free|edition|pack)$", "", t)
    # Keep only alphanumeric characters
    cleaned = re.sub(r"[^\w\s]", "", t)
    return " ".join(cleaned.split())

@dataclass(frozen=True)
class Offer:
    store: str
    title: str
    url: str
    end_date: str = ""
    source: str = ""
    details: str = ""

    @property
    def key(self) -> str:
        # Key on store + normalized title so different URL formats for the same game hash identically
        norm_t = normalize_title(self.title)
        raw = f"{self.store.lower()}|{norm_t}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_date(value) -> str:
    if not value:
        return ""
    if isinstance(value, (int, float)):
        try:
            # Accept seconds or milliseconds.
            stamp = value / 1000 if value > 10_000_000_000 else value
            return datetime.fromtimestamp(stamp, timezone.utc).isoformat()
        except (ValueError, OSError, OverflowError):
            return ""
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError):
        return ""


def is_future_or_unknown(end_date: str) -> bool:
    if not end_date:
        return True
    try:
        return datetime.fromisoformat(end_date).astimezone(timezone.utc) > utc_now()
    except ValueError:
        return True


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def clearly_keepable(title: str, details: str, url: str, strict: bool = True) -> bool:
    title_l = title.casefold()
    text = f"{title} {details} {url}".casefold()

    if any(word in title_l for word in BAD_TITLE_WORDS):
        return False
    if any(word in text for word in REJECT_WORDS):
        return False
    if strict and not any(phrase in text for phrase in KEEP_PHRASES):
        return False
    if not url.startswith(("https://", "http://")):
        return False
    return True


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
        return DEFAULT_CONFIG.copy()
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        merged = DEFAULT_CONFIG.copy()
        merged.update({k: v for k, v in data.items() if k != "sources"})
        merged["sources"] = {**DEFAULT_CONFIG["sources"], **data.get("sources", {})}
        return merged
    except (OSError, json.JSONDecodeError) as exc:
        logging.error("Could not read config.json: %s; using defaults", exc)
        return DEFAULT_CONFIG.copy()


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def get_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def epic_offers(session: requests.Session, country: str) -> list[Offer]:
    """Read Epic's promotions feed, capturing main giveaways and secret store flash sales."""
    endpoint = (
        "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
        f"?locale=en-GB&country={country}&allowCountries={country}"
    )
    response = session.get(endpoint, timeout=TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    elements = (
        payload.get("data", {}).get("Catalog", {}).get("searchStore", {}).get("elements", [])
    )
    offers: list[Offer] = []

    for item in elements:
        offer_type = item.get("offerType")
        if offer_type not in (None, "BASE_GAME", "BUNDLE", "OTHERS"):
            continue

        price = item.get("price", {}).get("totalPrice", {})
        original = price.get("originalPrice")
        discounted = price.get("discountPrice")
        final = price.get("finalPrice")

        if not isinstance(original, (int, float)) or original <= 0:
            continue
        # Check if the game is 100% off (free)
        if discounted != 0 and final != 0:
            continue

        title = clean_text(item.get("title", ""))
        mappings = item.get("catalogNs", {}).get("mappings", [])
        page_slug = mappings[0].get("pageSlug") if mappings else None
        slug = page_slug or item.get("productSlug") or item.get("urlSlug")

        if slug and slug != "[]":
            url = f"https://store.epicgames.com/en-GB/p/{slug}"
        else:
            url = "https://store.epicgames.com/en-GB/free-games"

        # Try to extract end date from promotional blocks if available
        promos = item.get("promotions") or {}
        current = promos.get("promotionalOffers") or []
        current_blocks = [b for group in current for b in group.get("promotionalOffers", [])]
        end_values = [parse_date(block.get("endDate")) for block in current_blocks]
        end_values = [x for x in end_values if x]
        end_date = min(end_values) if end_values else ""

        details = "Official Epic 100% discount store item."
        if title and clearly_keepable(title, details, url, strict=False):
            offers.append(Offer("Epic Games Store", title, url, end_date, "Epic public promotions API", details))

    return offers


from curl_cffi import requests as cffi_requests

def steamdb_offers(session: requests.Session) -> list[Offer]:
    """
    SteamDB page scan using curl_cffi to bypass Cloudflare TLS fingerprinting.
    """
    url = "https://steamdb.info/upcoming/free/"
    
    try:
        # impersonate="chrome" mimics Chrome's exact TLS handshake and headers
        response = cffi_requests.get(url, impersonate="chrome", timeout=TIMEOUT)
        response.raise_for_status()
    except Exception as exc:
        raise requests.RequestException(f"SteamDB cffi request failed: {exc}")

    soup = BeautifulSoup(response.text, "html.parser")
    offers: list[Offer] = []

    for node in soup.select("tr, .game, .item, article"):
        text = clean_text(node.get_text(" ", strip=True))
        if not text or "free to keep" not in text.casefold():
            continue
        link = node.select_one('a[href*="store.steampowered.com/app/"], a[href*="/app/"]')
        if not link:
            continue
        title = clean_text(link.get_text(" ", strip=True) or link.get("title", ""))
        href = link.get("href", "")
        if not title or not href:
            continue
        if href.startswith("/"):
            href = "https://steamdb.info" + href
            
        end_date = ""
        match = re.search(
            r"Expires:\s*(\d{1,2}\s+\w+\s+\d{4})\s*[–-]\s*(\d{2}:\d{2}:\d{2})\s*UTC",
            text, re.I
        )
        if match:
            try:
                raw = f"{match.group(1)} {match.group(2)}"
                end_date = datetime.strptime(raw, "%d %B %Y %H:%M:%S").replace(
                    tzinfo=timezone.utc
                ).isoformat()
            except ValueError:
                end_date = ""
        offers.append(Offer("Steam", title, href, end_date, "SteamDB Free to Keep page", text[:300]))

    unique = {offer.key: offer for offer in offers}
    return list(unique.values())

def gamerpower_offers(session: requests.Session) -> list[Offer]:
    """
    Pulls community-verified freebies from GamerPower.
    Strictly blocks third-party account/point walls (Alienware, Gleam) 
    while safely allowing direct Epic store flash sales (Mafia Hotel, GetHigh.EXE).
    """
    response = session.get(
        "https://www.gamerpower.com/api/giveaways",
        params={"type": "game"},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()

    if not isinstance(data, list):
        return []

    # Hard domains/platforms that require accounts, tasks, or points
    bad_third_party_sites = (
        "alienware", "alienwarearena", "gleam.io", "gleam", 
        "steelseries", "razer", "keyhub", "lootboy", "gamehag"
    )
    bad_requirement_terms = (
        "arp", "level", "points", "task", "social media", 
        "create account", "link account", "survey"
    )

    offers: list[Offer] = []
    for item in data:
        if str(item.get("type", "")).casefold() != "game":
            continue

        raw_title = clean_text(item.get("title", ""))
        details = clean_text(item.get("description", ""))
        instructions = clean_text(item.get("instructions", ""))
        platforms = str(item.get("platforms", "")).casefold()
        url = str(item.get("open_giveaway_url") or item.get("gamerpower_url") or "")
        end_date = parse_date(item.get("end_date"))

        if not raw_title or not url.startswith("https://"):
            continue

        full_meta = f"{raw_title} {details} {instructions} {url}".casefold()

        # Block reward/task sites (Alienware, Gleam, etc.)
        if any(site in full_meta for site in bad_third_party_sites):
            continue
        # Block point or account level walls
        if any(term in full_meta for term in bad_requirement_terms):
            continue
        # Block generic trials/demos
        if any(word in full_meta for word in REJECT_WORDS):
            continue
        if any(word in raw_title.casefold() for word in BAD_TITLE_WORDS):
            continue
        if end_date and not is_future_or_unknown(end_date):
            continue

        store = None
        if "epic" in platforms or "epicgames" in url:
            store = "Epic Games Store"
        elif "steam" in platforms:
            store = "Steam"
        elif "gog" in platforms:
            store = "GOG"
        elif "xbox" in platforms:
            store = "Xbox"
        elif "humble" in platforms or "humblebundle.com" in url:
            store = "Humble Bundle"

        if not store:
            continue

        # Clean title tags (e.g. "GetHigh.EXE (Epic Games) Giveaway" -> "GetHigh.EXE")
        clean_t = re.sub(r"\s*[\(\[\{].*?[\)\]\}]", "", raw_title).strip()
        clean_t = re.sub(r"(?i)\s+giveaway$", "", clean_t).strip()

        offers.append(
            Offer(
                store=store,
                title=clean_t,
                url=url,
                end_date=end_date,
                source="GamerPower API",
                details=details[:300],
            )
        )

    return list({offer.key: offer for offer in offers}.values())


def humble_offers(session: requests.Session) -> list[Offer]:
    """
    Best-effort scan of Humble's public store page for explicitly free-to-keep
    giveaway wording. Humble changes its page frequently; the GamerPower adapter
    is the other discovery route. Paid bundles and Humble Choice are excluded.
    """
    urls = (
        "https://www.humblebundle.com/store",
        "https://www.humblebundle.com/games",
    )
    offers: list[Offer] = []
    for page_url in urls:
        try:
            response = session.get(page_url, timeout=TIMEOUT)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            for link in soup.select('a[href*="/store/"], a[href*="/games/"]'):
                title = clean_text(link.get_text(" ", strip=True) or link.get("aria-label", ""))
                href = link.get("href", "")
                if href.startswith("/"):
                    href = "https://www.humblebundle.com" + href
                parent = link.parent
                details = clean_text(parent.parent.get_text(" ", strip=True)) if parent and parent.parent else ""
                if not title or not href or "humblebundle.com" not in href:
                    continue
                if not any(x in f"{title} {details}".casefold() for x in KEEP_PHRASES):
                    continue
                if any(x in f"{title} {details}".casefold() for x in REJECT_WORDS):
                    continue
                if any(x in title.casefold() for x in BAD_TITLE_WORDS):
                    continue
                # Avoid bundles/Choice pages; those are often paid or subscription offers.
                if "/bundle/" in href or "choice" in href.casefold() or "bundle" in title.casefold():
                    continue
                offers.append(Offer("Humble Bundle", title, href, "", "Humble public store page", details[:300]))
        except requests.RequestException as exc:
            logging.warning("Humble page unavailable (%s): %s", page_url, exc)
    return list({offer.key: offer for offer in offers}.values())


def gog_offers(session: requests.Session) -> list[Offer]:
    """Query GOG's catalog API for free-to-keep base games."""
    url = "https://embed.gog.com/games/ajax/filtered?mediaType=game&price=free&sort=popularity"
    response = session.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    data = response.json()
    
    offers = []
    for product in data.get("products", []):
        title = clean_text(product.get("title", ""))
        slug = product.get("slug", "")
        game_url = f"https://www.gog.com/game/{slug}" if slug else "https://www.gog.com"
        
        # Ensure it's genuinely free and a base game
        price_obj = product.get("price", {})
        if price_obj.get("isFree") or price_obj.get("finalMoney", "0") == "0":
            if title and clearly_keepable(title, "GOG permanent freebie", game_url, strict=False):
                offers.append(Offer("GOG", title, game_url, "", "GOG public catalog API", "GOG free promotion"))
                
    return list({o.key: o for o in offers}.values())


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS notified (
                offer_key TEXT PRIMARY KEY,
                store TEXT NOT NULL,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                end_date TEXT,
                first_seen TEXT NOT NULL,
                source TEXT
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS source_status (
                source TEXT PRIMARY KEY,
                last_success TEXT,
                last_error TEXT
            )
        """)


def already_notified(offer: Offer) -> bool:
    with sqlite3.connect(DB_PATH) as db:
        row = db.execute("SELECT 1 FROM notified WHERE offer_key = ?", (offer.key,)).fetchone()
    return row is not None


def record_offer(offer: Offer) -> None:
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """INSERT OR IGNORE INTO notified
               (offer_key, store, title, url, end_date, first_seen, source)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (offer.key, offer.store, offer.title, offer.url, offer.end_date,
             utc_now().isoformat(), offer.source),
        )


def notify(offer: Offer) -> None:
    end = ""
    if offer.end_date:
        try:
            dt = datetime.fromisoformat(offer.end_date).astimezone()
            end = f"Claim by: {dt.strftime('%a %d %b, %H:%M %Z')}"
        except ValueError:
            end = f"Claim by: {offer.end_date}"
    message = f"{offer.store}: {offer.title}"
    if end:
        message += f"\n{end}"
    toast = Notification(
        app_id=APP_NAME,
        title="Free game to keep",
        msg=message[:250],
        duration="long",
    )
    toast.set_audio(audio.Default, loop=False)
    toast.add_actions(label="Open claim page", launch=offer.url)
    toast.show()


def collect(config: dict) -> tuple[list[Offer], dict[str, str]]:
    session = get_session()
    all_offers: list[Offer] = []
    statuses: dict[str, str] = {}
    sources = config["sources"]

    jobs = []
    if sources.get("epic", True):
        jobs.append(("Epic public API", lambda: epic_offers(session, config.get("country", "GB"))))
    if sources.get("steamdb", True):
        jobs.append(("SteamDB", lambda: steamdb_offers(session)))
    if sources.get("gamerpower", True):
        jobs.append(("GamerPower", lambda: gamerpower_offers(session)))
    if sources.get("humble", True):
        jobs.append(("Humble page", lambda: humble_offers(session)))
    if sources.get("gog", True):
        jobs.append(("GOG API", lambda: gog_offers(session)))

    for name, job in jobs:
        try:
            offers = job()
            all_offers.extend(offers)
            statuses[name] = f"OK ({len(offers)} candidates)"
            logging.info("%s: found %d candidate offers", name, len(offers))
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            statuses[name] = f"ERROR: {exc}"
            logging.warning("%s failed: %s", name, exc)

    priority = {
        "Epic public promotions API": 0,
        "SteamDB Free to Keep page": 0,
        "GOG public catalog API": 0,
        "Humble public store page": 0,
        "GamerPower API": 1,
    }

    merged: dict[str, Offer] = {}
    for offer in all_offers:
        if offer.key not in merged or priority.get(offer.source, 2) < priority.get(merged[offer.key].source, 2):
            merged[offer.key] = offer

    return list(merged.values()), statuses


def run_once(config: dict, first_run: bool = False) -> int:
    offers, statuses = collect(config)
    logging.info("Source status: %s", statuses)
    new_count = 0
    for offer in offers:
        if not is_future_or_unknown(offer.end_date):
            continue
        if already_notified(offer):
            continue
        # On first launch, let the user choose whether current offers should alert.
        if first_run and not config.get("notify_on_first_run", True):
            record_offer(offer)
            continue
        try:
            notify(offer)
            record_offer(offer)
            new_count += 1
            logging.info("Notified: [%s] %s (%s)", offer.store, offer.title, offer.url)
            time.sleep(1)  # avoid flooding Windows toast notifications
        except Exception as exc:
            logging.exception("Notification failed for %s: %s", offer.title, exc)
    logging.info("Finished check: %d current offers, %d new notifications", len(offers), new_count)
    return new_count


def main() -> None:
    setup_logging()
    init_db()
    config = load_config()
    interval = max(30, int(config.get("check_every_minutes", 180)))
    first_run = not DB_PATH.exists()  # init_db creates it, so detect using DB contents below
    with sqlite3.connect(DB_PATH) as db:
        total = db.execute("SELECT COUNT(*) FROM notified").fetchone()[0]
    first_run = total == 0

    if "--once" in sys.argv:
        run_once(config, first_run)
        return
    if "--test-notification" in sys.argv:
        notify(Offer("Test store", "This is a test notification", "https://store.steampowered.com/"))
        logging.info("Sent a test notification.")
        return

    logging.info("%s started; checking every %d minutes. Ctrl+C to stop.", APP_NAME, interval)
    while True:
        try:
            run_once(config, first_run)
            first_run = False
        except Exception:
            logging.exception("Unexpected error during check")
        time.sleep(interval * 60)

def gamerpower_offers(session: requests.Session) -> list[Offer]:
    response = session.get(
        "https://www.gamerpower.com/api/giveaways",
        params={"type": "game"},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()

    if not isinstance(data, list):
        return []

    # Strict domains requiring points, tasks, or accounts (like Alienware)
    bad_third_party_sites = (
        "alienware", "alienwarearena", "gleam.io", "gleam", 
        "steelseries", "razer", "keyhub", "lootboy", "gamehag"
    )
    bad_requirement_terms = (
        "arp", "level", "points", "task", "social media", 
        "create account", "link account", "survey"
    )

    offers: list[Offer] = []
    for item in data:
        if str(item.get("type", "")).casefold() != "game":
            continue

        raw_title = clean_text(item.get("title", ""))
        details = clean_text(item.get("description", ""))
        instructions = clean_text(item.get("instructions", ""))
        platforms = str(item.get("platforms", "")).casefold()
        url = str(item.get("open_giveaway_url") or item.get("gamerpower_url") or "")
        end_date = parse_date(item.get("end_date"))

        if not raw_title or not url.startswith("https://"):
            continue

        full_meta = f"{raw_title} {details} {instructions} {url}".casefold()

        # Block actual third-party reward sites
        if any(site in full_meta for site in bad_third_party_sites):
            continue
        # Block point/level/task walls
        if any(term in full_meta for term in bad_requirement_terms):
            continue
        # Block generic trials/demos
        if any(word in full_meta for word in REJECT_WORDS):
            continue
        if any(word in raw_title.casefold() for word in BAD_TITLE_WORDS):
            continue
        if end_date and not is_future_or_unknown(end_date):
            continue

        store = None
        # Explicitly capture Epic indie sales whether listed via platform or URL
        if "epic" in platforms or "epicgames" in url or "epic games" in full_meta:
            store = "Epic Games Store"
        elif "steam" in platforms:
            store = "Steam"
        elif "gog" in platforms:
            store = "GOG"
        elif "xbox" in platforms:
            store = "Xbox"
        elif "humble" in platforms or "humblebundle.com" in url:
            store = "Humble Bundle"

        if not store:
            continue

        # Clean title tags (e.g. "GetHigh.EXE (Epic Games) Giveaway" -> "GetHigh.EXE")
        clean_t = re.sub(r"\s*[\(\[\{].*?[\)\]\}]", "", raw_title).strip()
        clean_t = re.sub(r"(?i)\s+giveaway$", "", clean_t).strip()

        offers.append(
            Offer(
                store=store,
                title=clean_t,
                url=url,
                end_date=end_date,
                source="GamerPower API",
                details=details[:300],
            )
        )

    return list({offer.key: offer for offer in offers}.values())

if __name__ == "__main__":
    main()
