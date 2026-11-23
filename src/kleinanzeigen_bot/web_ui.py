# SPDX-FileCopyrightText: © Sebastian Thomschke and contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-ArtifactOfProjectHomePage: https://github.com/Second-Hand-Friends/kleinanzeigen-bot/
"""Local web interface for kleinanzeigen-bot.

Serves a single-page UI plus a small JSON API on top of the existing
domain modules:

* ad overview with status (``ad_loading`` + ``ad_status``)
* creating ads and editing ad / config files with validation
* running CLI commands (publish, update, delete, ...) as subprocesses
  with live log output

Only the standard library HTTP server is used, so no extra runtime
dependencies are required. The server binds to ``127.0.0.1`` by default
because it exposes the configuration including login credentials.
"""
from __future__ import annotations

import io
import json
import mimetypes
import os
import re
import subprocess  # noqa: S404 the UI starts the bot CLI itself
import sys
import threading
import time
import unicodedata
import webbrowser
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast
from urllib.parse import parse_qs, urlparse

import requests
from pydantic import ValidationError
from ruamel.yaml import YAML

from . import ad_loading, ad_status
from . import price_suggestion as _price_suggestion
from . import resources as _resources
from . import runtime_config as _runtime_config
from .model.config_model import Config
from .utils import loggers as _loggers
from .utils import misc as _misc
from .utils import xdg_paths as _xdg_paths
from .utils.files import abspath
from .utils.misc import is_frozen

if TYPE_CHECKING:
    from .model.ad_model import Ad

LOG:Final[_loggers.Logger] = _loggers.get_logger(__name__)
LOG.setLevel(_loggers.INFO)

DEFAULT_HOST:Final[str] = "127.0.0.1"
DEFAULT_PORT:Final[int] = 8090

# Commands the UI may start. "login" needs an interactive terminal and the
# workspace-free/meta commands make no sense from the UI.
JOB_COMMANDS:Final[frozenset[str]] = frozenset({
    "verify", "publish", "update", "delete", "extend", "reserve", "activate",
    "download", "list", "status", "update-content-hash", "update-check", "diagnose",
})
_MAX_LOG_LINES:Final[int] = 5000
_MAX_BODY_BYTES:Final[int] = 2 * 1024 * 1024
_ADS_SELECTOR_PATTERN:Final[re.Pattern[str]] = re.compile(r"^[a-z0-9,\-]{1,500}$")
_ANSI_PATTERN:Final[re.Pattern[str]] = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_IMAGE_SUFFIXES:Final[frozenset[str]] = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"})


class ApiError(Exception):
    def __init__(self, status:HTTPStatus, message:str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# --------------------------------------------------------------------------- #
# Jobs (CLI subprocesses)
# --------------------------------------------------------------------------- #


@dataclass
class Job:
    id:int
    command:str
    args:list[str]
    started:float = field(default_factory = time.time)
    finished:float | None = None
    exit_code:int | None = None
    lines:list[str] = field(default_factory = list)
    first_line_no:int = 0
    process:subprocess.Popen[bytes] | None = None
    lock:threading.Lock = field(default_factory = threading.Lock)

    @property
    def running(self) -> bool:
        return self.finished is None

    def append(self, line:str) -> None:
        with self.lock:
            self.lines.append(line)
            overflow = len(self.lines) - _MAX_LOG_LINES
            if overflow > 0:
                del self.lines[:overflow]
                self.first_line_no += overflow

    def snapshot(self, since:int = 0) -> dict[str, Any]:
        with self.lock:
            start = max(since - self.first_line_no, 0)
            lines = self.lines[start:]
            next_line = self.first_line_no + len(self.lines)
        return {
            "id": self.id,
            "command": self.command,
            "args": self.args,
            "started": self.started,
            "finished": self.finished,
            "running": self.running,
            "exit_code": self.exit_code,
            "lines": lines,
            "next": next_line,
        }


def cli_base_command() -> list[str]:
    """Return the argv prefix that starts this bot's CLI in a child process."""
    if is_frozen():
        return [sys.executable]
    return [sys.executable, "-m", "kleinanzeigen_bot"]


class JobRunner:
    """Runs at most one CLI command at a time and keeps its output."""

    def __init__(self, base_args:list[str]) -> None:
        self._base_args = base_args
        self._lock = threading.Lock()
        self._next_id = 1
        self.current:Job | None = None
        self.history:list[dict[str, Any]] = []

    def start(self, command:str, *, ads:str | None, keep_old:bool, verbose:bool, lang:str | None) -> Job:
        if command not in JOB_COMMANDS:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"Unsupported command: {command}")
        args = [command]
        if ads:
            ads = ads.strip().lower().replace(" ", "")
            if not _ADS_SELECTOR_PATTERN.match(ads):
                raise ApiError(HTTPStatus.BAD_REQUEST, f"Invalid ads selector: {ads}")
            args.append(f"--ads={ads}")
        elif command == "delete":
            raise ApiError(HTTPStatus.BAD_REQUEST, "The delete command requires an ads selector")
        if keep_old:
            args.append("--keep-old")
        if verbose:
            args.append("--verbose")
        if lang in {"de", "en"}:
            args.append(f"--lang={lang}")

        with self._lock:
            if self.current and self.current.running:
                raise ApiError(HTTPStatus.CONFLICT, "Another command is still running")
            job = Job(id = self._next_id, command = command, args = args)
            self._next_id += 1
            self.current = job

        env = dict(os.environ, PYTHONUNBUFFERED = "1", PYTHONIOENCODING = "utf-8", NO_COLOR = "1")
        argv = [*cli_base_command(), *args, *self._base_args]
        job.append("$ kleinanzeigen-bot " + " ".join(args))
        try:
            job.process = subprocess.Popen(  # noqa: S603 argv is built from a fixed allow-list
                argv,
                stdin = subprocess.DEVNULL,
                stdout = subprocess.PIPE,
                stderr = subprocess.STDOUT,
                env = env,
            )
        except OSError as ex:
            job.append(f"Failed to start: {ex}")
            self._finish(job, -1)
            return job
        threading.Thread(target = self._pump, args = (job,), daemon = True, name = f"job-{job.id}").start()
        return job

    def _pump(self, job:Job) -> None:
        assert job.process is not None  # noqa: S101
        assert job.process.stdout is not None  # noqa: S101
        for raw in io.TextIOWrapper(job.process.stdout, encoding = "utf-8", errors = "replace"):
            job.append(_ANSI_PATTERN.sub("", raw.rstrip("\r\n")))
        self._finish(job, job.process.wait())

    def _finish(self, job:Job, exit_code:int) -> None:
        job.exit_code = exit_code
        job.finished = time.time()
        with self._lock:
            self.history.insert(0, {
                "id": job.id, "command": job.command, "args": job.args,
                "started": job.started, "finished": job.finished, "exit_code": exit_code,
            })
            del self.history[20:]

    def cancel(self) -> bool:
        job = self.current
        if not job or not job.running or job.process is None:
            return False
        job.append("Cancelled by user.")
        job.process.terminate()
        try:
            job.process.wait(timeout = 10)
        except subprocess.TimeoutExpired:
            job.process.kill()
        return True


# --------------------------------------------------------------------------- #
# Ad and config access
# --------------------------------------------------------------------------- #


def _round_trip_yaml() -> YAML:
    yaml = YAML()
    yaml.indent(mapping = 2, sequence = 4, offset = 2)
    yaml.preserve_quotes = True
    return yaml


def _parse_document(text:str, suffix:str) -> Any:
    try:
        if suffix == ".json":
            return json.loads(text)
        return _round_trip_yaml().load(text)
    except Exception as ex:
        raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, f"Syntax error: {ex}") from ex


def _format_validation_error(ex:ValidationError) -> str:
    parts = []
    for err in ex.errors():
        loc = ".".join(str(item) for item in err.get("loc", ()))
        parts.append(f"{loc}: {err.get('msg')}" if loc else str(err.get("msg")))
    return "\n".join(parts) or str(ex)


def slugify(text:str) -> str:
    normalized = unicodedata.normalize("NFKD", text.replace("ß", "ss")).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "_", normalized.lower()).strip("_")
    return slug[:50].rstrip("_") or "anzeige"


class BotContext:
    """File-system facing operations used by the HTTP handler."""

    def __init__(self, config_file_path:str, workspace:_xdg_paths.Workspace | None) -> None:
        self.config_file_path = abspath(config_file_path)
        self.config_dir = Path(self.config_file_path).parent
        self.workspace = workspace

    # -- config -------------------------------------------------------------

    def load_state(self) -> _runtime_config.RuntimeState:
        try:
            return _runtime_config.load_config(self.config_file_path, None, "web")
        except ValidationError as ex:
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, _format_validation_error(ex)) from ex
        except Exception as ex:
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, str(ex)) from ex

    def read_config_text(self) -> str:
        path = Path(self.config_file_path)
        if not path.exists():
            _runtime_config.create_default_config(self.config_file_path, self.workspace)
        return path.read_text(encoding = "utf-8")

    def validate_config_text(self, text:str) -> None:
        data = _parse_document(text, Path(self.config_file_path).suffix)
        if not isinstance(data, dict):
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "The config file must contain a mapping")
        data = json.loads(json.dumps(data, default = str))
        try:
            _runtime_config._resolve_login_credentials(data)  # noqa: SLF001 same validation as the CLI
        except ValueError:
            pass  # missing env vars are reported by the CLI at runtime
        try:
            Config.model_validate(data, strict = True, context = self.config_file_path)
        except ValidationError as ex:
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, _format_validation_error(ex)) from ex

    def write_config_text(self, text:str) -> None:
        self.validate_config_text(text)
        Path(self.config_file_path).write_text(text, encoding = "utf-8")

    # -- ads ----------------------------------------------------------------

    def _ad_files(self, config:Config) -> dict[str, str]:
        return ad_loading.discover_ad_files(self.config_file_path, config.ad_files)

    def resolve_ad_file(self, rel:str, config:Config | None = None) -> str:
        """Map a relative ad path from the client to a known ad file (no arbitrary paths)."""
        cfg = config or self.load_state().config
        for abs_path, rel_path in self._ad_files(cfg).items():
            if rel in {rel_path, abs_path}:
                return abs_path
        raise ApiError(HTTPStatus.NOT_FOUND, f"Unknown ad file: {rel}")

    def list_ads(self) -> dict[str, Any]:
        state = self.load_state()
        config = state.config
        now = _misc.now()
        ads:list[dict[str, Any]] = []
        errors:list[dict[str, str]] = []
        for abs_path, rel_path in sorted(self._ad_files(config).items(), key = lambda kv: kv[1]):
            try:
                raw = _parse_document(Path(abs_path).read_text(encoding = "utf-8"), Path(abs_path).suffix)
                raw_plain = json.loads(json.dumps(raw, default = str))
                ad = ad_loading.load_ad(raw, config.ad_defaults, abs_path)
            except ApiError as ex:
                errors.append({"file": rel_path, "error": ex.message})
                continue
            except ValidationError as ex:
                errors.append({"file": rel_path, "error": _format_validation_error(ex)})
                continue
            except Exception as ex:
                errors.append({"file": rel_path, "error": str(ex)})
                continue
            ads.append(self._ad_summary(abs_path, rel_path, ad, raw = raw, raw_plain = raw_plain, categories = state.categories, now = now))
        return {"ads": ads, "errors": errors}

    def _ad_summary(
        self, abs_path:str, rel_path:str, ad:Ad, *, raw:dict[str, Any], raw_plain:dict[str, Any],
        categories:dict[str, str], now:Any,
    ) -> dict[str, Any]:
        try:
            status = ad_status.compute_ad_status(ad, raw, now = now)
        except Exception:
            status = "unknown"
        images:list[str] = []
        problems:list[str] = []
        if ad.images:
            try:
                images = ad_loading.resolve_ad_images(abs_path, list(ad.images))
            except Exception as ex:  # noqa: BLE001 surfaced to the user instead of failing the listing
                problems.append(str(ex))
        category_id = categories.get(ad.category, ad.category) if ad.category else None
        category_name = ad.category
        if ad.category and ad.category not in categories:
            reverse = {v: k for k, v in categories.items()}
            category_name = reverse.get(ad.category, ad.category)
        return {
            "file": rel_path,
            "title": ad.title,
            "description": ad.description,
            "price": ad.price,
            "price_type": ad.price_type,
            "type": ad.type,
            "active": ad.active,
            "id": ad.id,
            "status": status,
            "category": category_name,
            "category_id": category_id,
            "shipping_type": ad.shipping_type,
            "images": [os.path.relpath(img, self.config_dir) for img in images],
            "image_count": len(images),
            "problems": problems,
            "created_on": raw_plain.get("created_on"),
            "updated_on": raw_plain.get("updated_on"),
            "repost_count": ad.repost_count,
            "republication_interval": ad.republication_interval,
            "url": f"https://www.kleinanzeigen.de/s-anzeige/{ad.id}" if ad.id else None,
        }

    def read_ad_text(self, rel:str) -> dict[str, str]:
        abs_path = self.resolve_ad_file(rel)
        return {"file": rel, "path": abs_path, "content": Path(abs_path).read_text(encoding = "utf-8")}

    def _validate_ad_data(self, data:Any, config:Config, file_hint:str) -> None:
        if not isinstance(data, dict):
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "The ad file must contain a mapping")
        try:
            ad_loading.load_ad(data, config.ad_defaults, file_hint)
        except ValidationError as ex:
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, _format_validation_error(ex)) from ex
        except Exception as ex:
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, str(ex)) from ex

    def write_ad_text(self, rel:str, text:str) -> None:
        config = self.load_state().config
        abs_path = self.resolve_ad_file(rel, config)
        data = _parse_document(text, Path(abs_path).suffix)
        self._validate_ad_data(json.loads(json.dumps(data, default = str)), config, abs_path)
        Path(abs_path).write_text(text, encoding = "utf-8")

    def set_ad_field(self, rel:str, key:str, value:Any) -> None:
        """Change a single top-level field of an ad file, keeping comments and formatting."""
        config = self.load_state().config
        abs_path = self.resolve_ad_file(rel, config)
        path = Path(abs_path)
        if path.suffix == ".json":
            data = json.loads(path.read_text(encoding = "utf-8"))
            data[key] = value
            self._validate_ad_data(data, config, abs_path)
            path.write_text(json.dumps(data, indent = 2, ensure_ascii = False), encoding = "utf-8")
            return
        yaml = _round_trip_yaml()
        data = yaml.load(path.read_text(encoding = "utf-8"))
        data[key] = value
        self._validate_ad_data(json.loads(json.dumps(data, default = str)), config, abs_path)
        buffer = io.StringIO()
        yaml.dump(data, buffer)
        path.write_text(buffer.getvalue(), encoding = "utf-8")

    def set_ad_active(self, rel:str, *, active:bool) -> None:
        self.set_ad_field(rel, "active", active)

    def set_ad_price(self, rel:str, price:Any) -> None:
        try:
            value = int(price)
        except (TypeError, ValueError) as ex:
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "price: must be a whole number") from ex
        if value < 0:
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "price: must not be negative")
        self.set_ad_field(rel, "price", value)

    def price_suggestion(self, query:dict[str, str]) -> dict[str, Any]:
        """Suggest a price for an existing ad (``file``) or for a title/category from the form."""
        state = self.load_state()
        title = query.get("title", "")
        category = query.get("category", "")
        exclude_id:str | None = None
        if query.get("file"):
            abs_path = self.resolve_ad_file(query["file"], state.config)
            raw = _parse_document(Path(abs_path).read_text(encoding = "utf-8"), Path(abs_path).suffix)
            ad = ad_loading.load_ad(raw, state.config.ad_defaults, abs_path)
            title = title or ad.title
            category = category or ad.category
            exclude_id = str(ad.id) if ad.id else None
        category_id = state.categories.get(category, category) if category else None
        try:
            result = _price_suggestion.suggest_price(
                title = title, query = query.get("query"), category = category_id, exclude_id = exclude_id,
            )
        except ValueError as ex:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(ex)) from ex
        except requests.RequestException as ex:
            raise ApiError(HTTPStatus.BAD_GATEWAY, f"kleinanzeigen.de search failed: {ex}") from ex
        return result.to_dict()

    def create_ad(self, fields:dict[str, Any]) -> dict[str, str]:
        state = self.load_state()
        config = state.config
        title = str(fields.get("title") or "").strip()
        if not title:
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "title: Field required")

        data:dict[str, Any] = {"active": bool(fields.get("active", True)), "type": fields.get("type") or "OFFER", "title": title}
        description = str(fields.get("description") or "").strip()
        data["description"] = description
        data["category"] = str(fields.get("category") or "").strip()
        price = str(fields.get("price") or "").strip()
        if price:
            try:
                data["price"] = int(price)
            except (TypeError, ValueError) as ex:
                raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "price: must be a whole number") from ex
        for key in ("price_type", "shipping_type"):
            if fields.get(key):
                data[key] = fields[key]
        images = [line.strip() for line in str(fields.get("images") or "").splitlines() if line.strip()]
        if images:
            data["images"] = images

        folder = self.config_dir / "ads" / slugify(title)
        target = folder / f"ad_{slugify(title)}.yaml"
        counter = 2
        while target.exists():
            target = folder / f"ad_{slugify(title)}_{counter}.yaml"
            counter += 1
        self._validate_ad_data(data, config, str(target))

        folder.mkdir(parents = True, exist_ok = True)
        buffer = io.StringIO()
        yaml = _round_trip_yaml()
        yaml.representer.add_representer(
            str, lambda dumper, value: dumper.represent_scalar("tag:yaml.org,2002:str", value, style = "|" if "\n" in value else None),
        )
        yaml.dump(data, buffer)
        target.write_text(
            "# yaml-language-server: $schema=https://raw.githubusercontent.com/Second-Hand-Friends/kleinanzeigen-bot/main/schemas/ad.schema.json\n"
            + buffer.getvalue(),
            encoding = "utf-8",
        )
        rel = os.path.relpath(target, self.config_dir)
        if abspath(str(target)) not in self._ad_files(config):
            raise ApiError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                f"Created {rel}, but it does not match the ad_files patterns in the config: {config.ad_files}",
            )
        return {"file": rel}

    def delete_ad_file(self, rel:str) -> None:
        abs_path = self.resolve_ad_file(rel)
        Path(abs_path).unlink()

    def image_path(self, rel:str) -> Path:
        """Return an image path below the config directory, refusing anything else."""
        candidate = (self.config_dir / rel).resolve()
        if not candidate.is_relative_to(self.config_dir.resolve()) or candidate.suffix.lower() not in _IMAGE_SUFFIXES:
            raise ApiError(HTTPStatus.FORBIDDEN, "Access denied")
        if not candidate.is_file():
            raise ApiError(HTTPStatus.NOT_FOUND, "Not found")
        return candidate

    def categories(self) -> list[dict[str, str]]:
        state = self.load_state()
        return [{"name": name, "id": cid} for name, cid in sorted(state.categories.items())]


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #


def _static_file(name:str) -> bytes:
    path = Path(_resources.__file__).parent / "web" / name
    return path.read_bytes()


class WebUIServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address:tuple[str, int], context:BotContext, jobs:JobRunner, version:str) -> None:
        super().__init__(address, WebUIRequestHandler)
        self.context = context
        self.jobs = jobs
        self.version = version


class WebUIRequestHandler(BaseHTTPRequestHandler):
    server_version = "kleinanzeigen-bot-web"

    def log_message(self, format:str, *args:Any) -> None:  # noqa: A002 signature defined by the base class
        if _loggers.is_debug(LOG):
            LOG.debug("%s - %s", self.address_string(), format % args)

    # -- helpers --------------------------------------------------------------

    def _send(self, status:HTTPStatus, body:bytes, content_type:str, extra_headers:dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload:Any, status:HTTPStatus = HTTPStatus.OK) -> None:
        self._send(status, json.dumps(payload, ensure_ascii = False, default = str).encode("utf-8"), "application/json; charset=utf-8")

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > _MAX_BODY_BYTES:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request too large")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as ex:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Invalid JSON body") from ex
        if not isinstance(data, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "JSON body must be an object")
        return data

    def _check_same_origin(self) -> None:
        """Block cross-site requests (CSRF / DNS rebinding) to state-changing endpoints."""
        if self.headers.get("X-Requested-With") != "kleinanzeigen-bot":
            raise ApiError(HTTPStatus.FORBIDDEN, "Missing X-Requested-With header")
        origin = self.headers.get("Origin")
        host = self.headers.get("Host")
        if origin and host and urlparse(origin).netloc != host:
            raise ApiError(HTTPStatus.FORBIDDEN, "Cross-origin request rejected")

    def _dispatch(self, method:str) -> None:
        url = urlparse(self.path)
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        try:
            if method != "GET":
                self._check_same_origin()
            self._route(method, url.path, query)
        except ApiError as ex:
            self._json({"error": ex.message}, ex.status)
        except Exception as ex:
            LOG.exception("Web UI request failed: %s %s", method, url.path)
            self._json({"error": str(ex)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_GET(self) -> None:  # noqa: N802 name defined by BaseHTTPRequestHandler
        self._dispatch("GET")

    def do_HEAD(self) -> None:  # noqa: N802 name defined by BaseHTTPRequestHandler
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802 name defined by BaseHTTPRequestHandler
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802 name defined by BaseHTTPRequestHandler
        self._dispatch("PUT")

    def do_DELETE(self) -> None:  # noqa: N802 name defined by BaseHTTPRequestHandler
        self._dispatch("DELETE")

    # -- routes ----------------------------------------------------------------

    def _route(self, method:str, path:str, query:dict[str, str]) -> None:  # noqa: PLR0911, PLR0912, C901 flat routing table
        server = cast(WebUIServer, self.server)
        ctx = server.context
        jobs = server.jobs
        match method, path:
            case "GET", "/" | "/index.html":
                self._send(HTTPStatus.OK, _static_file("index.html"), "text/html; charset=utf-8", {
                    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                    "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'",
                    "X-Frame-Options": "DENY",
                })
            case "GET", "/favicon.svg":
                self._send(HTTPStatus.OK, _static_file("favicon.svg"), "image/svg+xml")
            case "GET", "/api/info":
                self._json({
                    "version": server.version,
                    "config_file": ctx.config_file_path,
                    "config_exists": Path(ctx.config_file_path).exists(),
                    "workspace_mode": ctx.workspace.mode if ctx.workspace else None,
                    "commands": sorted(JOB_COMMANDS),
                })
            case "GET", "/api/ads":
                self._json(ctx.list_ads())
            case "POST", "/api/ads":
                self._json(ctx.create_ad(self._body()), HTTPStatus.CREATED)
            case "GET", "/api/ad":
                self._json(ctx.read_ad_text(query.get("file", "")))
            case "PUT", "/api/ad":
                body = self._body()
                ctx.write_ad_text(str(body.get("file", "")), str(body.get("content", "")))
                self._json({"ok": True})
            case "POST", "/api/ad/active":
                body = self._body()
                ctx.set_ad_active(str(body.get("file", "")), active = bool(body.get("active")))
                self._json({"ok": True})
            case "POST", "/api/ad/price":
                body = self._body()
                ctx.set_ad_price(str(body.get("file", "")), body.get("price"))
                self._json({"ok": True})
            case "GET", "/api/price-suggestion":
                self._json(ctx.price_suggestion(query))
            case "DELETE", "/api/ad":
                ctx.delete_ad_file(query.get("file", ""))
                self._json({"ok": True})
            case "GET", "/api/image":
                image = ctx.image_path(query.get("path", ""))
                content_type = mimetypes.guess_type(image.name)[0] or "application/octet-stream"
                self._send(HTTPStatus.OK, image.read_bytes(), content_type)
            case "GET", "/api/categories":
                self._json(ctx.categories())
            case "GET", "/api/config":
                self._json({"path": ctx.config_file_path, "content": ctx.read_config_text()})
            case "PUT", "/api/config":
                ctx.write_config_text(str(self._body().get("content", "")))
                self._json({"ok": True})
            case "POST", "/api/config/validate":
                ctx.validate_config_text(str(self._body().get("content", "")))
                self._json({"ok": True})
            case "POST", "/api/jobs":
                body = self._body()
                job = jobs.start(
                    str(body.get("command", "")),
                    ads = str(body["ads"]) if body.get("ads") else None,
                    keep_old = bool(body.get("keep_old")),
                    verbose = bool(body.get("verbose")),
                    lang = str(body["lang"]) if body.get("lang") else None,
                )
                self._json(job.snapshot(), HTTPStatus.ACCEPTED)
            case "GET", "/api/jobs/current":
                current = jobs.current
                since = int(query.get("since", "0") or 0)
                self._json({"job": current.snapshot(since) if current else None, "history": jobs.history})
            case "POST", "/api/jobs/cancel":
                self._json({"cancelled": jobs.cancel()})
            case _:
                raise ApiError(HTTPStatus.NOT_FOUND, "Not found")


def create_server(
    *,
    config_file_path:str,
    workspace:_xdg_paths.Workspace | None,
    version:str,
    host:str = DEFAULT_HOST,
    port:int = DEFAULT_PORT,
) -> WebUIServer:
    base_args = [f"--config={abspath(config_file_path)}"]
    if workspace is not None:
        base_args.append(f"--workspace-mode={workspace.mode}")
    context = BotContext(config_file_path, workspace)
    return WebUIServer((host, port), context, JobRunner(base_args), version)


def serve(
    *,
    config_file_path:str,
    workspace:_xdg_paths.Workspace | None,
    version:str,
    host:str = DEFAULT_HOST,
    port:int = DEFAULT_PORT,
    open_browser:bool = False,
) -> None:
    server = create_server(config_file_path = config_file_path, workspace = workspace, version = version, host = host, port = port)
    url = f"http://{'localhost' if host in {'127.0.0.1', '::1', '0.0.0.0', '::', ''} else host}:{server.server_address[1]}/"  # noqa: S104 display only
    if host not in {"127.0.0.1", "localhost", "::1"}:
        LOG.warning("The web interface is reachable from other hosts and exposes your configuration including credentials!")
    LOG.info("Web interface running at %s (press CTRL+C to stop)", url)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.jobs.cancel()
        server.server_close()
        LOG.info("Web interface stopped.")
