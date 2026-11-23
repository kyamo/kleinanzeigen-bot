# SPDX-FileCopyrightText: © Sebastian Thomschke and contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-ArtifactOfProjectHomePage: https://github.com/Second-Hand-Friends/kleinanzeigen-bot/
"""Tests for the local web interface (HTTP API on a real loopback server)."""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any

import pytest

from kleinanzeigen_bot import web_ui
from kleinanzeigen_bot.cli import parse_args

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

pytestmark = pytest.mark.unit

CONFIG_YAML = """\
ad_files:
  - "./**/ad_*.{json,yml,yaml}"
ad_defaults:
  contact:
    name: "Tester"
    zipcode: 12345
login:
  username: "user@example.com"
  password: "secret"
"""

SOFA_YAML = """\
# my comment
active: true
title: "Gemütliches Sofa, grau, 3-Sitzer"
description: "Sehr gut erhalten"
category: 80/86
price: 150
images:
  - "*.png"
"""

BIKE_YAML = """\
active: true
title: "Damenfahrrad 28 Zoll mit Korb"
description: "Fährt gut"
category: 210/217
price: 90
id: 3012345678
created_on: "2020-01-01T10:00:00"
updated_on: "2020-01-01T10:00:00"
"""

HEADERS = {"X-Requested-With": "kleinanzeigen-bot", "Content-Type": "application/json"}


@pytest.fixture
def workspace(tmp_path:Path) -> Path:
    (tmp_path / "config.yaml").write_text(CONFIG_YAML, encoding = "utf-8")
    (tmp_path / "ads" / "sofa").mkdir(parents = True)
    (tmp_path / "ads" / "sofa" / "ad_sofa.yaml").write_text(SOFA_YAML, encoding = "utf-8")
    (tmp_path / "ads" / "sofa" / "front.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    (tmp_path / "ads" / "ad_bike.yaml").write_text(BIKE_YAML, encoding = "utf-8")
    (tmp_path / "ads" / "ad_broken.yaml").write_text('title: "short"\n', encoding = "utf-8")
    (tmp_path / "secret.txt").write_text("top secret", encoding = "utf-8")
    return tmp_path


@pytest.fixture
def base_url(workspace:Path) -> Iterator[str]:
    server = web_ui.create_server(
        config_file_path = str(workspace / "config.yaml"), workspace = None, version = "test", host = "127.0.0.1", port = 0,
    )
    thread = threading.Thread(target = server.serve_forever, daemon = True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.jobs.cancel()
        server.shutdown()
        server.server_close()


def call(base_url:str, method:str, path:str, body:dict[str, Any] | None = None, headers:dict[str, str] | None = None) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base_url + path, data = data, method = method, headers = HEADERS if headers is None else headers)  # noqa: S310 loopback test server
    try:
        with urllib.request.urlopen(request, timeout = 30) as response:  # noqa: S310 loopback test server
            raw = response.read()
            status = response.status
            content_type = response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as ex:
        raw = ex.read()
        status = ex.code
        content_type = ex.headers.get("Content-Type", "")
    return status, json.loads(raw) if "json" in content_type else raw


def test_index_page_is_served(base_url:str) -> None:
    status, body = call(base_url, "GET", "/")
    assert status == 200
    assert b"kleinanzeigen-bot" in body


def test_list_ads_reports_status_images_and_errors(base_url:str) -> None:
    status, body = call(base_url, "GET", "/api/ads")
    assert status == 200
    ads = {ad["title"]: ad for ad in body["ads"]}
    assert ads["Gemütliches Sofa, grau, 3-Sitzer"]["status"] == "draft"
    assert ads["Gemütliches Sofa, grau, 3-Sitzer"]["images"] == ["ads/sofa/front.png"]
    assert ads["Damenfahrrad 28 Zoll mit Korb"]["status"] == "due"
    assert ads["Damenfahrrad 28 Zoll mit Korb"]["url"].endswith("/3012345678")
    assert [err["file"] for err in body["errors"]] == ["./ads/ad_broken.yaml"]


def test_list_ads_reports_missing_images_as_problem(base_url:str, workspace:Path) -> None:
    (workspace / "ads" / "sofa" / "front.png").unlink()
    _, body = call(base_url, "GET", "/api/ads")
    sofa = next(ad for ad in body["ads"] if ad["title"].startswith("Gemütliches"))
    assert sofa["images"] == []
    assert len(sofa["problems"]) == 1
    assert "*.png" in sofa["problems"][0]


def test_create_ad_writes_valid_yaml_file(base_url:str, workspace:Path) -> None:
    status, body = call(base_url, "POST", "/api/ads", {
        "title": "Kinderwagen für Zwillinge, top", "description": "Zeile 1\nZeile 2", "category": "17/18", "price": "40",
        "price_type": "FIXED", "images": "*.jpg\n", "active": True,
    })
    assert status == 201, body
    created = workspace / body["file"]
    assert created.name == "ad_kinderwagen_fur_zwillinge_top.yaml"
    content = created.read_text(encoding = "utf-8")
    assert "price: 40" in content
    assert "description: |" in content

    _, listing = call(base_url, "GET", "/api/ads")
    assert "Kinderwagen für Zwillinge, top" in [ad["title"] for ad in listing["ads"]]


def test_create_ad_rejects_invalid_input(base_url:str, workspace:Path) -> None:
    status, body = call(base_url, "POST", "/api/ads", {"title": "kurz", "description": "x", "category": "1"})
    assert status == 422
    assert "title" in body["error"]
    assert not (workspace / "ads" / "kurz").exists()


def test_edit_ad_validates_before_writing(base_url:str, workspace:Path) -> None:
    sofa = workspace / "ads" / "sofa" / "ad_sofa.yaml"
    status, body = call(base_url, "PUT", "/api/ad", {"file": "./ads/sofa/ad_sofa.yaml", "content": "title: [broken"})
    assert status == 422
    assert "Syntax error" in body["error"]
    assert sofa.read_text(encoding = "utf-8") == SOFA_YAML

    new_content = SOFA_YAML.replace("price: 150", "price: 120")
    status, _ = call(base_url, "PUT", "/api/ad", {"file": "./ads/sofa/ad_sofa.yaml", "content": new_content})
    assert status == 200
    assert sofa.read_text(encoding = "utf-8") == new_content


def test_toggle_active_preserves_comments(base_url:str, workspace:Path) -> None:
    status, _ = call(base_url, "POST", "/api/ad/active", {"file": "./ads/sofa/ad_sofa.yaml", "active": False})
    assert status == 200
    content = (workspace / "ads" / "sofa" / "ad_sofa.yaml").read_text(encoding = "utf-8")
    assert "# my comment" in content
    assert "active: false" in content


def test_paths_outside_known_files_are_rejected(base_url:str) -> None:
    status, _ = call(base_url, "GET", "/api/ad?file=../secret.txt")
    assert status == 404
    status, _ = call(base_url, "PUT", "/api/ad", {"file": "../../etc/passwd", "content": "a: b"})
    assert status == 404
    status, _ = call(base_url, "GET", "/api/image?path=../secret.txt")
    assert status == 403
    status, _ = call(base_url, "GET", "/api/image?path=secret.txt")
    assert status == 403
    status, body = call(base_url, "GET", "/api/image?path=ads/sofa/front.png")
    assert status == 200
    assert body.startswith(b"\x89PNG")


def test_state_changing_requests_require_same_origin_header(base_url:str) -> None:
    status, _ = call(base_url, "POST", "/api/jobs", {"command": "verify"}, headers = {"Content-Type": "application/json"})
    assert status == 403
    status, _ = call(base_url, "POST", "/api/jobs", {"command": "verify"}, headers = {**HEADERS, "Origin": "http://evil.example"})
    assert status == 403


def test_config_validation_and_save(base_url:str, workspace:Path) -> None:
    status, body = call(base_url, "GET", "/api/config")
    assert status == 200
    assert body["content"] == CONFIG_YAML

    status, body = call(base_url, "POST", "/api/config/validate", {"content": "ad_files: []\n"})
    assert status == 422
    assert "ad_files" in body["error"]

    status, _ = call(base_url, "PUT", "/api/config", {"content": "login: 5\n"})
    assert status == 422
    assert (workspace / "config.yaml").read_text(encoding = "utf-8") == CONFIG_YAML

    updated = CONFIG_YAML + "categories:\n  Mein Sofa: 80/86\n"
    status, _ = call(base_url, "PUT", "/api/config", {"content": updated})
    assert status == 200
    _, categories = call(base_url, "GET", "/api/categories")
    assert {"name": "Mein Sofa", "id": "80/86"} in categories


@pytest.mark.parametrize(("body", "expected"), [
    ({"command": "login"}, "Unsupported command"),
    ({"command": "publish", "ads": "1;rm -rf /"}, "Invalid ads selector"),
    ({"command": "delete"}, "requires an ads selector"),
])
def test_job_start_rejects_unsafe_requests(base_url:str, body:dict[str, Any], expected:str) -> None:
    status, response = call(base_url, "POST", "/api/jobs", body)
    assert status == 400
    assert expected in response["error"]


def test_job_runs_cli_and_streams_output(base_url:str, monkeypatch:pytest.MonkeyPatch) -> None:
    script = "import sys; print('hello from', sys.argv[1:]); sys.exit(3)"
    monkeypatch.setattr(web_ui, "cli_base_command", lambda: [sys.executable, "-c", script])

    status, job = call(base_url, "POST", "/api/jobs", {"command": "verify", "ads": "1, 2", "verbose": True, "lang": "de"})
    assert status == 202
    assert job["args"] == ["verify", "--ads=1,2", "--verbose", "--lang=de"]

    deadline = time.monotonic() + 20
    current:dict[str, Any] = {}
    while time.monotonic() < deadline:
        _, data = call(base_url, "GET", "/api/jobs/current")
        current = data["job"]
        if not current["running"]:
            break
        time.sleep(0.1)
    assert current["exit_code"] == 3
    output = "\n".join(current["lines"])
    assert "hello from ['verify', '--ads=1,2', '--verbose', '--lang=de', '--config=" in output
    _, data = call(base_url, "GET", f"/api/jobs/current?since={current['next']}")
    assert data["job"]["lines"] == []
    assert data["history"][0]["exit_code"] == 3


def test_second_job_is_rejected_while_running(base_url:str, monkeypatch:pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_ui, "cli_base_command", lambda: [sys.executable, "-c", "import time; time.sleep(30)"])
    status, _ = call(base_url, "POST", "/api/jobs", {"command": "verify"})
    assert status == 202
    status, body = call(base_url, "POST", "/api/jobs", {"command": "verify"})
    assert status == 409
    assert "still running" in body["error"]
    status, body = call(base_url, "POST", "/api/jobs/cancel", {})
    assert body == {"cancelled": True}


@pytest.mark.parametrize(("text", "expected"), [
    ("Gemütliches Sofa, grau", "gemutliches_sofa_grau"),
    ("Straße & Weg!!", "strasse_weg"),
    ("!!!", "anzeige"),
])
def test_slugify(text:str, expected:str) -> None:
    assert web_ui.slugify(text) == expected


def test_parse_args_web_options() -> None:
    parsed = parse_args(["app", "web", "--host=0.0.0.0", "--port=9000", "--open"])
    assert parsed.command == "web"
    assert parsed.web_host == "0.0.0.0"  # noqa: S104 just parsing
    assert parsed.web_port == 9000
    assert parsed.open_browser


@pytest.mark.parametrize("port", ["0", "70000", "abc"])
def test_parse_args_rejects_invalid_port(port:str) -> None:
    with pytest.raises(SystemExit) as exc:
        parse_args(["app", "web", f"--port={port}"])
    assert exc.value.code == 2


def test_price_suggestion_for_existing_ad_and_apply(base_url:str, workspace:Path, monkeypatch:pytest.MonkeyPatch) -> None:
    from kleinanzeigen_bot import price_suggestion  # noqa: PLC0415 only needed here

    requested:list[str] = []
    page = "".join(
        f'<article class="x" data-adid="{i}" data-href="/s-anzeige/{i}"><script type="application/ld+json">{{"title":"Damenfahrrad 28 Zoll"}}</script>'
        f'<p class="text-title3">{price} €</p></article>'
        for i, price in enumerate([80, 90, 100, 110], start = 1)
    )

    def fake_fetch(url:str) -> str:
        requested.append(url)
        return page

    monkeypatch.setattr(price_suggestion, "fetch_search_page", fake_fetch)

    status, body = call(base_url, "GET", "/api/price-suggestion?file=./ads/ad_bike.yaml")
    assert status == 200, body
    assert requested[0].endswith("/s-damenfahrrad-28-zoll-korb/k0c217")
    assert body["suggested_price"] == 95
    assert body["count"] == 4

    status, body = call(base_url, "GET", "/api/price-suggestion?title=Damenfahrrad%2028%20Zoll&category=210/217")
    assert status == 200
    assert body["query"] == "damenfahrrad 28 zoll"

    status, _ = call(base_url, "POST", "/api/ad/price", {"file": "./ads/ad_bike.yaml", "price": 95})
    assert status == 200
    assert "price: 95" in (workspace / "ads" / "ad_bike.yaml").read_text(encoding = "utf-8")
    status, body = call(base_url, "POST", "/api/ad/price", {"file": "./ads/ad_bike.yaml", "price": "abc"})
    assert status == 422


def test_price_suggestion_reports_search_failures(base_url:str, monkeypatch:pytest.MonkeyPatch) -> None:
    import requests  # noqa: PLC0415 only needed here

    from kleinanzeigen_bot import price_suggestion  # noqa: PLC0415 only needed here

    def fail(url:str) -> str:
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(price_suggestion, "fetch_search_page", fail)
    status, body = call(base_url, "GET", "/api/price-suggestion?title=Sofa%20grau%20gemuetlich")
    assert status == 502
    assert "offline" in body["error"]
