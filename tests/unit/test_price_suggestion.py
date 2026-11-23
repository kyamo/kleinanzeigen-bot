# SPDX-FileCopyrightText: © Sebastian Thomschke and contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-ArtifactOfProjectHomePage: https://github.com/Second-Hand-Friends/kleinanzeigen-bot/
"""Tests for price suggestions from comparable listings (offline fixtures, no network)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kleinanzeigen_bot import price_suggestion as ps

pytestmark = pytest.mark.unit

FIXTURE = (Path(__file__).parent.parent / "fixtures" / "search_results_page.html").read_text(encoding = "utf-8")


def listing(listing_id:str, title:str, price:int | None, price_type:str = "FIXED") -> ps.Comparable:
    return ps.Comparable(id = listing_id, title = title, price = price, price_type = price_type, url = f"https://example.invalid/{listing_id}")


def test_parse_search_results_reads_titles_prices_and_types() -> None:
    results = {item.id: item for item in ps.parse_search_results(FIXTURE)}
    assert len(results) == 9
    assert results["1001"].title == "IKEA Billy Regal weiß"
    assert (results["1001"].price, results["1001"].price_type) == (30, "FIXED")
    assert (results["1002"].price, results["1002"].price_type) == (40, "NEGOTIABLE")
    assert results["1002"].title == "Billy Regal Birke & Türen"
    assert results["1003"].price == 35  # reduced price, not the struck-through one
    assert (results["1004"].price, results["1004"].price_type) == (0, "GIVE_AWAY")
    assert results["1005"].price == 1250
    assert (results["1009"].price, results["1009"].price_type) == (None, "NEGOTIABLE")
    assert results["1001"].url == "https://www.kleinanzeigen.de/s-anzeige/ikea-billy-regal-weiss/1001-88-1"


def test_compute_suggestion_ignores_outliers_placeholders_and_unrelated_listings() -> None:
    result = ps.compute_suggestion(
        ps.parse_search_results(FIXTURE), query = "ikea billy regal", category_id = "88", search_url = "u", exclude_id = "1008",
    )
    used = {item.id for item in result.comparables if item.used}
    # 1004 give-away, 1005 outlier, 1006 placeholder price, 1007 unrelated title, 1008 own ad, 1009 no price
    assert used == {"1001", "1002", "1003"}
    assert result.count == 3
    assert result.median == 35
    assert result.suggested_price == 35
    assert result.quick_sale_price == 30
    assert (result.minimum, result.maximum) == (30, 40)
    assert result.negotiable_share == pytest.approx(0.33)
    assert "1008" not in {item.id for item in result.comparables}


def test_compute_suggestion_needs_minimum_number_of_prices() -> None:
    result = ps.compute_suggestion(
        [listing("1", "Sofa grau", 100), listing("2", "Sofa grau", 120)], query = "sofa grau", category_id = None, search_url = "u",
    )
    assert result.count == 2
    assert result.suggested_price is None


def test_compute_suggestion_removes_high_outliers() -> None:
    prices = [100, 110, 120, 125, 130, 140, 150, 2000]
    listings = [listing(str(i), "Fahrrad 28 Zoll", price) for i, price in enumerate(prices)]
    result = ps.compute_suggestion(listings, query = "fahrrad 28 zoll", category_id = None, search_url = "u")
    assert result.outliers == 1
    assert result.maximum == 150
    assert result.suggested_price == 125


def test_compute_suggestion_deduplicates_listings() -> None:
    listings = [listing("1", "Tisch Eiche", 50), listing("1", "Tisch Eiche", 50), listing("2", "Tisch Eiche", 60), listing("3", "Tisch Eiche", 70)]
    result = ps.compute_suggestion(listings, query = "tisch eiche", category_id = None, search_url = "u")
    assert result.count == 3
    assert len(result.comparables) == 3


def test_suggest_price_falls_back_to_all_categories(monkeypatch:pytest.MonkeyPatch) -> None:
    requested:list[str] = []

    def fake_fetch(url:str) -> str:
        requested.append(url)
        return "<html></html>" if "k0c" in url else FIXTURE

    monkeypatch.setattr(ps, "fetch_search_page", fake_fetch)
    result = ps.suggest_price(title = "IKEA Billy Regal – sehr guter Zustand", category = "80/88/regale")
    assert requested == [
        "https://www.kleinanzeigen.de/s-ikea-billy-regal/k0c88",
        "https://www.kleinanzeigen.de/s-ikea-billy-regal/k0",
    ]
    assert result.category_id is None
    assert result.suggested_price == 30


def test_suggest_price_requires_query() -> None:
    with pytest.raises(ValueError, match = "title or search query"):
        ps.suggest_price(title = "   ")
