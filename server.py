"""Local web server for appliance utilization. Listens on 127.0.0.1 only."""

import argparse
import errno
import json
import os
import secrets
import socket
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import meraki

from core.logger import logger
import utilization_rules as rules

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
BAND_SIZE = 50
MIN_PORT = 1024
MAX_PORT = 65535
MAX_BODY_BYTES = 65_536
BROWSER_DELAY_SECONDS = 0.5

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/app.css": ("app.css", "text/css; charset=utf-8"),
    "/static/favicon.png": ("favicon.png", "image/png"),
    "/favicon.ico": ("favicon.png", "image/png"),
}

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; "
        "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


class PortsBusyError(RuntimeError):
    def __init__(self, tried):
        self.tried = list(tried)
        super().__init__(f"No free port among {self.tried}")


def band(start=DEFAULT_PORT, size=BAND_SIZE):
    return list(range(start, start + size))


def port_candidates(requested=None, start=DEFAULT_PORT, size=BAND_SIZE):
    """Ports to try, in order. A port outside the band is tried once."""
    ports = band(start, size)
    if requested is None:
        return ports
    if requested in ports:
        return [requested] + [port for port in ports if port != requested]
    return [requested]


def is_address_in_use(exc):
    return getattr(exc, "winerror", None) == 10048 or exc.errno == errno.EADDRINUSE


def bind_first(candidates, factory):
    """Return (server, port, busy_ports). Only 'address already in use' moves on."""
    busy = []
    for port in candidates:
        try:
            return factory(port), port, busy
        except OSError as exc:
            if is_address_in_use(exc):
                busy.append(port)
                continue
            raise
    raise PortsBusyError(busy)


def busy_message(tried, start=DEFAULT_PORT):
    first, last = tried[0], tried[-1]
    return (
        f"Ports {first}-{last} are all in use. Pick a free port and run:\n"
        "  Windows:      .\\.venv\\Scripts\\python.exe server.py --port <port>\n"
        "  macOS/Linux:  .venv/bin/python server.py --port <port>\n"
        f"Accepted ports are {MIN_PORT}-{MAX_PORT}; the automatic band starts at {start}."
    )


class LocalServer(ThreadingHTTPServer):
    allow_reuse_address = False
    allow_reuse_port = False
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class FetchJob:
    """One background fetch. The page polls it for progress and the final result."""

    def __init__(self, total, report=print):
        self.id = secrets.token_hex(8)
        self.planned = total  # fixed at start; reported as the result's call count
        self.total = total  # progress denominator; progress() may change it
        self.done = 0
        self.phase = "starting"
        self.state = "running"
        self.result = None
        self.error = None
        self.cancel = threading.Event()
        self.started = time.monotonic()
        self._lock = threading.Lock()
        self._report = report

    def progress(self, done, total, kind, name="", outcome="", failed=False):
        """Record one finished item. The page shows the short line; the terminal adds the outcome."""
        page_line = f"{kind} {done}/{total}: {name}" if name else f"{kind} {done}/{total}"
        with self._lock:
            self.done, self.total, self.phase = done, total, page_line
        detail = f"[{done}/{total}] {name or kind}"
        if outcome:
            detail += f" · {outcome}"
        if failed:
            detail = f"Warning: {detail}"
        self._report(detail, flush=True)

    def finish(self, state, result=None, error=None):
        with self._lock:
            self.state, self.result, self.error = state, result, error

    def snapshot(self):
        with self._lock:
            data = {
                "id": self.id,
                "state": self.state,
                "done": self.done,
                "total": self.total,
                "phase": self.phase,
                "error": self.error,
            }
            if self.state == "done":
                data["result"] = self.result
            return data


class AppState:
    """Holds the lazily built ProjectLogic so the page can still load without a key."""

    def __init__(self, logic_factory=None, report=print):
        self._factory = logic_factory
        self._logic = None
        self._error = None
        self._lock = threading.Lock()
        self.job = None
        self.job_lock = threading.Lock()
        self.report = report

    def _default_factory(self):
        from logic import ProjectLogic

        return ProjectLogic()

    def logic(self):
        with self._lock:
            if self._logic is not None:
                return self._logic
            try:
                factory = self._factory or self._default_factory
                self._logic = factory()
                self._error = None
            except EnvironmentError as exc:
                self._error = str(exc)
                raise
            return self._logic

    def status(self):
        try:
            self.logic()
        except EnvironmentError:
            pass
        return {
            "keySet": bool(os.getenv("MK_CSM_KEY")) or self._logic is not None,
            "error": self._error,
        }


class RequestError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# EnvironmentError is an alias of OSError. It covers the missing-key failure.
HANDLED_ERRORS = (RequestError, EnvironmentError, meraki.APIError)


def http_error(exc):
    """(status, message) for one of HANDLED_ERRORS."""
    if isinstance(exc, RequestError):
        return exc.status, exc.message
    if isinstance(exc, meraki.APIError):
        return 502, f"Dashboard API error: {exc}"
    return 503, str(exc)


def org_choices(organizations):
    return {
        str(org.get("id")): org.get("name") or str(org.get("id"))
        for org in organizations or []
        if org.get("id")
    }


def parse_fetch_request(body, organizations, inventory_loader):
    """Validate a fetch request against what the key can see.

    Returns (org_id, devices, timespan, performance_resolution, uplink_resolution).
    """
    if not isinstance(body, dict):
        raise RequestError(400, "Request body must be a JSON object.")
    org_id = str(body.get("organizationId") or "")
    if org_id not in org_choices(organizations):
        raise RequestError(400, "Unknown organization.")

    all_models = body.get("allModels") is True
    models = body.get("models") or []
    if not isinstance(models, list) or not all(isinstance(m, str) for m in models):
        raise RequestError(400, "Models must be a list of strings.")

    inventory = inventory_loader(org_id)
    known = set(rules.discover_models(inventory))
    if not all_models:
        if not models:
            raise RequestError(400, "Select at least one model, or all appliance models.")
        if any(m not in known for m in models):
            raise RequestError(400, "One or more models are not in this organization.")
    devices = rules.select_devices(inventory, models, all_models)
    if not devices:
        raise RequestError(400, "No appliances match the selected models.")

    timespan = body.get("timespan")
    performance_resolution = body.get("performanceResolution")
    uplink_resolution = body.get("uplinkResolution")
    ok, message = rules.validate_request(timespan, performance_resolution, uplink_resolution, len(devices))
    if not ok:
        raise RequestError(400, message)
    return org_id, devices, timespan, performance_resolution, uplink_resolution


def resolution_options(timespan, device_count):
    if timespan not in rules.WINDOW_SECONDS:
        raise RequestError(400, "Window must be one of the listed presets.")
    if device_count < 0:
        raise RequestError(400, "Device count must not be negative.")
    count = max(device_count, 1)
    return {
        "performance": rules.performance_resolutions(timespan, count),
        "uplink": rules.uplink_resolutions(timespan),
        "defaultPerformance": rules.default_performance_resolution(timespan, count),
        "defaultUplink": rules.default_uplink_resolution(timespan),
        "maxPerformanceCalls": rules.MAX_PERFORMANCE_CALLS,
    }


def build_result(devices, fetched, timespan, performance_resolution, uplink_resolution, total_calls):
    networks = {}
    for device in devices:
        uplinks = fetched["uplinks"].get(device["network_id"])
        if uplinks is not None and device["network_id"] not in networks:
            networks[device["network_id"]] = {
                "name": device["network_name"],
                "uplinks": rules.uplink_mbps(uplinks, uplink_resolution),
            }
    series = {
        device["serial"]: {
            "utilization": [
                [start, percent]
                for start, _end, percent in fetched["utilization"].get(device["serial"], [])
            ],
            "performance": fetched["performance"].get(device["serial"], []),
        }
        for device in devices
    }
    return {
        "devices": rules.summarize(
            devices, fetched["utilization"], fetched["performance"], fetched["uplinks"], uplink_resolution
        ),
        "series": series,
        "networks": networks,
        "errors": fetched["errors"],
        "requestedDevices": len(devices),
        "timespan": timespan,
        "window": rules.window_by_seconds(timespan),
        "start": fetched["start"],
        "end": fetched["end"],
        "interval": rules.UTILIZATION_INTERVAL,
        "performanceResolution": performance_resolution,
        "uplinkResolution": uplink_resolution,
        "calls": total_calls,
        "fetchedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def run_job(state, job, org_id, org_name, devices, timespan, performance_resolution, uplink_resolution):
    from logic import FetchCancelled

    try:
        fetched = state.logic().fetch(
            org_id, devices, timespan, performance_resolution, uplink_resolution,
            progress=job.progress, cancel=job.cancel,
        )
        elapsed = max(0, round(time.monotonic() - job.started))
        payload = build_result(devices, fetched, timespan, performance_resolution, uplink_resolution, job.planned)
        failed = sum(1 for error in fetched["errors"] if error.get("message") != rules.SPARE_PERFORMANCE_MESSAGE)
        summary = f"{org_name}: {len(devices)} appliances in {elapsed}s"
        if failed:
            summary += f". {failed} failed"
        payload["organizationName"] = org_name
        payload["elapsedSeconds"] = elapsed
        payload["summary"] = summary
        job.finish("done", payload)
        state.report(summary, flush=True)
    except FetchCancelled:
        job.finish("cancelled", error="Fetch cancelled.")
        state.report("Fetch cancelled.", flush=True)
    except Exception as exc:  # the page shows the message; the log keeps the detail
        logger.exception("Fetch failed")
        job.finish("error", error=f"Fetch failed: {exc}")
        state.report(f"Fetch failed: {exc}", flush=True)


def start_job(state, org_id, org_name, devices, timespan, performance_resolution, uplink_resolution, runner=run_job):
    with state.job_lock:
        if state.job is not None and state.job.state == "running":
            raise RequestError(409, "A fetch is already running.")
        total = rules.progress_total(devices)
        job = FetchJob(total, report=state.report)
        job.phase = "Utilization"
        state.job = job
    state.report(f"Fetching organization {org_name}", flush=True)
    threading.Thread(
        target=runner,
        args=(state, job, org_id, org_name, devices, timespan, performance_resolution, uplink_resolution),
        daemon=True,
    ).start()
    return job


def make_handler(state, port):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "ApplianceUtilization"
        sys_version = ""

        def log_message(self, format, *args):
            logger.debug("HTTP " + format % args)

        def _host_ok(self):
            return (self.headers.get("Host") or "").lower() in allowed_hosts

        def _send(self, status, body, content_type, cache="no-store"):
            payload = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", cache)
            for name, value in SECURITY_HEADERS.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(payload)

        def _json(self, status, data):
            self._send(status, json.dumps(data), "application/json; charset=utf-8")

        def _error(self, status, message):
            self._json(status, {"error": message})

        def _job(self, job_id):
            job = state.job
            if job is None or not secrets.compare_digest(job.id.encode(), job_id.encode()):
                raise RequestError(404, "Unknown fetch.")
            return job

        def do_GET(self):
            if not self._host_ok():
                return self._error(403, "Forbidden host.")
            parsed = urlparse(self.path)
            if parsed.path in STATIC_FILES:
                name, content_type = STATIC_FILES[parsed.path]
                return self._send(200, (STATIC_DIR / name).read_bytes(), content_type)
            query = parse_qs(parsed.query)
            try:
                if parsed.path == "/api/status":
                    return self._json(200, state.status())
                if parsed.path == "/api/organizations":
                    return self._json(200, self._organizations())
                if parsed.path == "/api/models":
                    return self._json(200, self._models((query.get("org") or [""])[0]))
                if parsed.path == "/api/options":
                    try:
                        timespan = int((query.get("window") or [""])[0])
                        device_count = int((query.get("devices") or ["0"])[0])
                    except ValueError:
                        raise RequestError(400, "Window and devices must be whole numbers.")
                    return self._json(200, resolution_options(timespan, device_count))
                if parsed.path.startswith("/api/jobs/"):
                    return self._json(200, self._job(parsed.path[len("/api/jobs/"):]).snapshot())
            except HANDLED_ERRORS as exc:
                return self._error(*http_error(exc))
            return self._error(404, "Not found.")

        def do_POST(self):
            if not self._host_ok():
                return self._error(403, "Forbidden host.")
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/api/fetch":
                    job = self._start(self._read_json())
                    return self._json(202, {"jobId": job.id, "total": job.planned})
                if parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/cancel"):
                    self._read_json()
                    job = self._job(parsed.path[len("/api/jobs/"):-len("/cancel")])
                    job.cancel.set()
                    return self._json(200, {"ok": True})
            except HANDLED_ERRORS as exc:
                return self._error(*http_error(exc))
            return self._error(404, "Not found.")

        def _read_json(self):
            content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip()
            if content_type != "application/json":
                raise RequestError(415, "Content-Type must be application/json.")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise RequestError(400, "Invalid Content-Length.")
            if length < 0 or length > MAX_BODY_BYTES:
                raise RequestError(413, "Request body is too large.")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise RequestError(400, "Request body is not valid JSON.")

        def _organizations(self):
            orgs = state.logic().get_organizations()
            return {
                "organizations": [
                    {"id": org_id, "name": name}
                    for org_id, name in sorted(org_choices(orgs).items(), key=lambda kv: kv[1].lower())
                ]
            }

        def _models(self, org_id):
            logic = state.logic()
            if str(org_id) not in org_choices(logic.get_organizations()):
                raise RequestError(400, "Unknown organization.")
            inventory = logic.get_appliance_devices(org_id)
            counts = {}
            networks = {}
            for device in rules.select_devices(inventory, [], all_models=True):
                counts[device["model"]] = counts.get(device["model"], 0) + 1
                if device["network_id"]:
                    networks.setdefault(device["model"], set()).add(device["network_id"])
            return {
                "models": [
                    {"model": m, "devices": counts.get(m, 0), "networks": sorted(networks.get(m, ()))}
                    for m in rules.discover_models(inventory)
                ],
                "windows": [{"label": label, "seconds": seconds} for label, seconds in rules.WINDOW_OPTIONS],
                "defaultWindow": rules.DEFAULT_TIMESPAN,
                "interval": rules.UTILIZATION_INTERVAL,
            }

        def _start(self, body):
            logic = state.logic()
            organizations = logic.get_organizations()
            org_id, devices, timespan, performance_resolution, uplink_resolution = parse_fetch_request(
                body, organizations, logic.get_appliance_devices
            )
            org_name = org_choices(organizations).get(org_id, org_id)
            return start_job(
                state, org_id, org_name, devices, timespan, performance_resolution, uplink_resolution
            )

    return Handler


def create_server(state, requested_port=None):
    def factory(port):
        return LocalServer((HOST, port), make_handler(state, port))

    return bind_first(port_candidates(requested_port), factory)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Appliance utilization web app (127.0.0.1 only).")
    parser.add_argument("--port", type=int, default=None, help=f"Port to try first ({MIN_PORT}-{MAX_PORT}).")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the browser.")
    args = parser.parse_args(argv)
    if args.port is not None and not (MIN_PORT <= args.port <= MAX_PORT):
        parser.error(f"--port must be between {MIN_PORT} and {MAX_PORT}.")
    return args


def main(argv=None):
    args = parse_args(argv)
    state = AppState()
    try:
        server, port, busy = create_server(state, args.port)
    except PortsBusyError as exc:
        if len(exc.tried) == 1:
            print(f"Port {exc.tried[0]} is in use. Choose another with --port.", file=sys.stderr)
        else:
            print(busy_message(exc.tried), file=sys.stderr)
        return 1

    url = f"http://{HOST}:{port}/"
    if busy:
        print(f"Ports in use: {', '.join(str(p) for p in busy)}. Using port {port} instead.")
    print(f"Appliance utilization is running at {url}")
    if not os.getenv("MK_CSM_KEY"):
        print(
            "Warning: MK_CSM_KEY is not set. Set a read-only Dashboard API key in that environment variable, then restart.",
            flush=True,
        )
    print("Press Ctrl+C to stop.", flush=True)
    if not args.no_browser:
        threading.Timer(BROWSER_DELAY_SECONDS, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
