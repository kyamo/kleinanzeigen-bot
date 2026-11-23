# SPDX-FileCopyrightText: © Sebastian Thomschke and contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-ArtifactOfProjectHomePage: https://github.com/Second-Hand-Friends/kleinanzeigen-bot/
"""Price suggestions based on comparable listings on kleinanzeigen.de.

Fetches the public search result pages for a query (optionally restricted to the
ad's category), extracts the asking prices of the listings and derives a robust
price suggestion (median after removing outliers).

The parsing and statistics are browser-independent and pure; only
:func:`fetch_search_page` performs network I/O.
"""
from __future__ import annotations

import html
import json
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from gettext import gettext as _
from typing import Any, Final
from urllib.parse import quote

import requests

from .utils import loggers as _loggers

LOG:Final[_loggers.Logger] = _loggers.get_logger(__name__)

BASE_URL:Final[str] = "https://www.kleinanzeigen.de"
USER_AGENT:Final[str] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)
MIN_COMPARABLES:Final[int] = 3
_CACHE_TTL_SECONDS:Final[float] = 15 * 60
_REQUEST_TIMEOUT_SECONDS:Final[float] = 15
_MAX_QUERY_WORDS:Final[int] = 6
_PLACEHOLDER_PRICE_RATIO:Final[float] = 0.1

_ARTICLE_PATTERN:Final[re.Pattern[str]] = re.compile(
    r'<article\b[^>]*\bdata-adid="(?P<id>\d+)"[^>]*\bdata-href="(?P<href>[^"]*)"[^>]*>(?P<body>.*?)</article>', re.DOTALL,
)
_LD_JSON_PATTERN:Final[re.Pattern[str]] = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.DOTALL)
_PRICE_PATTERN:Final[re.Pattern[str]] = re.compile(r'<p class="[^"]*\btext-title3\b[^"]*">(.*?)</p>', re.DOTALL)
_TAG_PATTERN:Final[re.Pattern[str]] = re.compile(r"<[^>]+>")
_AMOUNT_PATTERN:Final[re.Pattern[str]] = re.compile(r"(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d{1,2}))?\s*€")
_WORD_PATTERN:Final[re.Pattern[str]] = re.compile(r"[0-9a-zäöüß]+", re.IGNORECASE)

# Words that describe the offer rather than the item and only narrow down the search.
_STOP_WORDS:Final[frozenset[str]] = frozenset({
    "und", "oder", "mit", "ohne", "für", "fur", "der", "die", "das", "den", "dem", "ein", "eine", "einer", "zu", "im", "in", "an",
    "auf", "von", "vom", "bei", "inkl", "inklusive", "neu", "neuwertig", "wie", "top", "gut", "sehr", "guter", "gutem", "zustand",
    "erhalten", "gebraucht", "verkaufe", "verkauf", "abholung", "versand", "vb", "np", "originalverpackt", "ovp", "super", "kaum",
    "benutzt", "selten", "set", "stück", "stk", "x",
})


@dataclass(frozen = True, slots = True)
class Comparable:
    id:str
    title:str
    price:int | None  # whole euros, None if no price is given
    price_type:str  # FIXED | NEGOTIABLE | GIVE_AWAY | UNKNOWN
    url:str
    used:bool = False  # whether the price went into the statistics


@dataclass(slots = True)
class PriceSuggestion:
    query:str
    category_id:str | None
    search_url:str
    comparables:list[Comparable] = field(default_factory = list)
    count:int = 0  # number of prices used for the statistics
    outliers:int = 0
    minimum:int | None = None
    maximum:int | None = None
    lower_quartile:float | None = None
    median:float | None = None
    upper_quartile:float | None = None
    suggested_price:int | None = None
    quick_sale_price:int | None = None
    negotiable_share:float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- #
# Query and URL building
# --------------------------------------------------------------------------- #


def build_query(title:str) -> str:
    """Reduce an ad title to its distinctive words for the search.

    >>> build_query("Gemütliches Sofa, grau, 3-Sitzer – sehr guter Zustand!")
    'gemütliches sofa grau 3 sitzer'
    """
    words = [word.lower() for word in _WORD_PATTERN.findall(title)]
    meaningful = [word for word in words if word not in _STOP_WORDS]
    return " ".join((meaningful or words)[:_MAX_QUERY_WORDS])


def search_category(category:str | None) -> str | None:
    """Return the numeric search category of a bot category id.

    >>> search_category("80/88/regale")
    '88'
    >>> search_category("161/176/sonstige")
    '176'
    >>> search_category("Haus & Garten") is None
    True
    """
    if not category:
        return None
    numeric = [part for part in category.split("/") if part.isdigit()]
    return numeric[-1] if numeric else None


def build_search_url(query:str, category_id:str | None = None, page:int = 1) -> str:
    """Build the public search URL.

    >>> build_search_url("ikea billy regal")
    'https://www.kleinanzeigen.de/s-ikea-billy-regal/k0'
    >>> build_search_url("bücherregal eiche", "88", page = 2)
    'https://www.kleinanzeigen.de/s-seite:2/b%C3%BCcherregal-eiche/k0c88'
    """
    slug = quote("-".join(query.lower().split()), safe = "-")
    page_part = f"seite:{page}/" if page > 1 else ""
    category_part = f"c{category_id}" if category_id else ""
    return f"{BASE_URL}/s-{page_part}{slug}/k0{category_part}"


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def parse_price(text:str) -> tuple[int | None, str]:
    """Parse the price label of a search result.

    >>> parse_price("1.250 € VB")
    (1250, 'NEGOTIABLE')
    >>> parse_price("<s>40 €</s> 35 €")
    (35, 'FIXED')
    >>> parse_price(" Zu verschenken")
    (0, 'GIVE_AWAY')
    >>> parse_price("VB")
    (None, 'NEGOTIABLE')
    """
    plain = html.unescape(_TAG_PATTERN.sub(" ", text))
    if "verschenken" in plain.lower():
        return 0, "GIVE_AWAY"
    negotiable = re.search(r"\bVB\b", plain) is not None
    amounts = _AMOUNT_PATTERN.findall(plain)
    if not amounts:
        return None, "NEGOTIABLE" if negotiable else "UNKNOWN"
    euros, cents = amounts[-1]  # a reduced price is shown after the struck-through old one
    value = int(euros.replace(".", "")) + (1 if cents and int(cents) >= 50 else 0)  # noqa: PLR2004 rounding threshold
    return value, "NEGOTIABLE" if negotiable else "FIXED"


def parse_search_results(page_html:str) -> list[Comparable]:
    """Extract the listings of a search result page."""
    results:list[Comparable] = []
    for match in _ARTICLE_PATTERN.finditer(page_html):
        body = match.group("body")
        title = ""
        ld_json = _LD_JSON_PATTERN.search(body)
        if ld_json:
            try:
                title = html.unescape(str(json.loads(ld_json.group(1)).get("title") or ""))
            except (json.JSONDecodeError, AttributeError):
                title = ""
        price_match = _PRICE_PATTERN.search(body)
        price, price_type = parse_price(price_match.group(1)) if price_match else (None, "UNKNOWN")
        results.append(Comparable(
            id = match.group("id"),
            title = title,
            price = price,
            price_type = price_type,
            url = BASE_URL + html.unescape(match.group("href")),
        ))
    return results


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #


def _quantile(sorted_values:list[int], fraction:float) -> float:
    """Linear-interpolated quantile of sorted values (same as ``statistics.quantiles(method="inclusive")``).

    The ``statistics`` module is excluded from the PyInstaller build, so it must not be used here.

    >>> _quantile([1, 2, 3, 4], 0.5), _quantile([10, 20, 30, 40, 50], 0.25), _quantile([7], 0.75)
    (2.5, 20.0, 7.0)
    """
    position = (len(sorted_values) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    return float(sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * (position - lower))


def round_price(value:float) -> int:
    """Round to a price people actually ask for.

    >>> [round_price(v) for v in (7.4, 23, 47, 138, 1234, 18760)]
    [7, 25, 45, 140, 1250, 18800]
    """
    if value < 20:  # noqa: PLR2004 price bands
        step = 1
    elif value < 200:  # noqa: PLR2004 price bands
        step = 5
    elif value < 1000:  # noqa: PLR2004 price bands
        step = 10
    elif value < 10000:  # noqa: PLR2004 price bands
        step = 50
    else:
        step = 100
    return max(int(round(value / step) * step), 0)


def _relevant(listing:Comparable, query_words:set[str]) -> bool:
    """Keep listings whose title shares at least half of the query words."""
    if not query_words:
        return True
    title_words = {word.lower() for word in _WORD_PATTERN.findall(listing.title)}
    hits = sum(1 for word in query_words if any(word in title_word for title_word in title_words))
    return hits * 2 >= len(query_words)


def compute_suggestion(
    listings:list[Comparable], *, query:str, category_id:str | None, search_url:str, exclude_id:str | None = None,
) -> PriceSuggestion:
    """Derive a price suggestion from comparable listings.

    Listings without a price, give-aways, the own ad and listings whose title does not
    match the query are ignored; placeholder prices (below 10 % of the median) and
    outliers (Tukey's fences, 1.5 × IQR) are removed.
    """
    query_words = {word for word in query.lower().split() if len(word) > 1}
    seen:set[str] = set()
    candidates:list[Comparable] = []
    for listing in listings:
        if listing.id in seen or listing.id == exclude_id:
            continue
        seen.add(listing.id)
        candidates.append(listing)

    priced = [item for item in candidates if item.price and item.price_type != "GIVE_AWAY" and _relevant(item, query_words)]
    outliers = 0
    if priced:
        # placeholder prices like "1 €" are far below the typical price and not caught by the quartile fences
        floor = _quantile(sorted(item.price for item in priced if item.price), 0.5) * _PLACEHOLDER_PRICE_RATIO
        kept = [item for item in priced if item.price is not None and item.price >= floor]
        outliers += len(priced) - len(kept)
        priced = kept
    prices = sorted(item.price for item in priced if item.price)
    if len(prices) >= 4:  # noqa: PLR2004 quartiles need a few values
        q1, q3 = _quantile(prices, 0.25), _quantile(prices, 0.75)
        low, high = q1 - 1.5 * (q3 - q1), q3 + 1.5 * (q3 - q1)
        kept = [item for item in priced if item.price is not None and low <= item.price <= high]
        outliers += len(priced) - len(kept)
        priced = kept
    used_ids = {item.id for item in priced}

    result = PriceSuggestion(
        query = query,
        category_id = category_id,
        search_url = search_url,
        comparables = [
            Comparable(item.id, item.title, item.price, item.price_type, item.url, used = item.id in used_ids) for item in candidates
        ],
        outliers = outliers,
    )
    values = sorted(item.price for item in priced if item.price is not None)
    result.count = len(values)
    if len(values) < MIN_COMPARABLES:
        return result

    result.minimum, result.maximum = values[0], values[-1]
    result.median = _quantile(values, 0.5)
    q1, q3 = _quantile(values, 0.25), _quantile(values, 0.75)
    result.lower_quartile, result.upper_quartile = float(q1), float(q3)
    result.suggested_price = round_price(result.median)
    result.quick_sale_price = round_price(q1)
    result.negotiable_share = round(sum(1 for item in priced if item.price_type == "NEGOTIABLE") / len(priced), 2)
    return result


# --------------------------------------------------------------------------- #
# Network
# --------------------------------------------------------------------------- #


_cache:dict[str, tuple[float, str]] = {}
_cache_lock = threading.Lock()


def fetch_search_page(url:str) -> str:
    """Fetch a public search page (cached for a few minutes to avoid repeated requests)."""
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(url)
        if cached and now - cached[0] < _CACHE_TTL_SECONDS:
            return cached[1]
    LOG.debug("Fetching comparable listings from %s", url)
    response = requests.get(
        url,
        headers = {"User-Agent": USER_AGENT, "Accept-Language": "de-DE,de;q=0.9", "Accept": "text/html"},
        timeout = _REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    with _cache_lock:
        _cache[url] = (now, response.text)
    return response.text


def suggest_price(
    *, title:str | None = None, query:str | None = None, category:str | None = None, exclude_id:str | None = None, pages:int = 2,
) -> PriceSuggestion:
    """Search comparable listings and compute a price suggestion.

    Searches within the category first and falls back to all categories when that
    yields too few comparable prices.
    """
    search_query = (query or "").strip() or build_query(title or "")
    if not search_query:
        raise ValueError(_("A title or search query is required"))
    category_id = search_category(category)

    attempts = [category_id, None] if category_id else [None]
    result:PriceSuggestion | None = None
    for attempt in attempts:
        listings:list[Comparable] = []
        for page in range(1, pages + 1):
            page_listings = parse_search_results(fetch_search_page(build_search_url(search_query, attempt, page)))
            listings.extend(page_listings)
            if len(page_listings) < 20:  # noqa: PLR2004 last page reached
                break
        result = compute_suggestion(
            listings, query = search_query, category_id = attempt, search_url = build_search_url(search_query, attempt), exclude_id = exclude_id,
        )
        if result.count >= MIN_COMPARABLES:
            break
    assert result is not None  # noqa: S101 attempts is never empty
    return result
