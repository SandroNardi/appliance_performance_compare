import errno
import http.client
import json
import socket
import threading
import time

import pytest

import server
from logic import FetchCancelled


def busy_error():
    exc = OSError(errno.EADDRINUSE, "Address already in use")
    exc.winerror = 10048
    return exc


class FakeFactory:
    def __init__(self, busy=()):
        self.busy = set(busy)
        self.tried = []

    def __call__(self, port):
        self.tried.append(port)
        if port in self.busy:
            raise busy_error()
        return f"httpd:{port}"


def test_default_walks_the_band_in_order():
    candidates = server.port_candidates()
    assert candidates == list(range(8765, 8815))
    factory = FakeFactory(busy={8765, 8766})
    httpd, port, busy = server.bind_first(candidates, factory)
    assert (httpd, port, busy) == ("httpd:8767", 8767, [8765, 8766])
    assert factory.tried == [8765, 8766, 8767]


def test_port_outside_the_band_is_a_single_attempt():
    assert server.port_candidates(9000) == [9000]
    factory = FakeFactory(busy={9000})
    with pytest.raises(server.PortsBusyError) as info:
        server.bind_first(server.port_candidates(9000), factory)
    assert info.value.tried == [9000]
    assert factory.tried == [9000]


def test_busy_port_inside_the_band_moves_to_the_next_free_one():
    candidates = server.port_candidates(8800)
    assert candidates[0] == 8800
    assert sorted(candidates) == list(range(8765, 8815))
    _, port, busy = server.bind_first(candidates, FakeFactory(busy={8800}))
    assert port == 8765
    assert busy == [8800]


def test_full_band_tells_the_person_how_to_pass_port():
    with pytest.raises(server.PortsBusyError) as info:
        server.bind_first(server.port_candidates(), FakeFactory(busy=set(range(8765, 8815))))
    message = server.busy_message(info.value.tried)
    assert "8765-8814" in message
    assert r".\.venv\Scripts\python.exe server.py --port <port>" in message
    assert ".venv/bin/python server.py --port <port>" in message


def test_log_file_sits_next_to_server_py():
    from pathlib import Path

    from core.logger import logger

    files = [h.baseFilename for h in logger.handlers if hasattr(h, "baseFilename")]
    assert files == [str(Path(server.__file__).resolve().parent / "application.log")]


def test_other_bind_errors_stop_startup():
    def factory(port):
        raise OSError(errno.EACCES, "Permission denied")

    with pytest.raises(OSError):
        server.bind_first([8765, 8766], factory)


def test_port_argument_must_be_in_range():
    assert server.parse_args(["--port", "9000", "--no-browser"]).port == 9000
    with pytest.raises(SystemExit):
        server.parse_args(["--port", "80"])


ORGS = [{"id": "1", "name": "Org One"}]
INVENTORY = [
    {"serial": "A", "model": "MX68", "networkId": "N_1", "name": "Branch"},
    {"serial": "B", "model": "Z3", "networkId": "N_2", "name": "Home"},
]
GOOD = {"organizationId": "1", "allModels": True, "timespan": 86_400,
        "performanceResolution": 3_600, "uplinkResolution": 300}


def test_fetch_request_validation():
    loader = lambda org_id: INVENTORY
    org_id, devices, timespan, perf, uplink = server.parse_fetch_request(
        {**GOOD, "allModels": False, "models": ["Z3"]}, ORGS, loader
    )
    assert (org_id, [d["serial"] for d in devices], timespan, perf, uplink) == ("1", ["B"], 86_400, 3_600, 300)

    bad_bodies = [
        [],
        {**GOOD, "organizationId": "2"},
        {**GOOD, "timespan": 1_234},
        {**GOOD, "performanceResolution": 1_200},
        {**GOOD, "uplinkResolution": 120},
        {**GOOD, "allModels": False, "models": []},
        {**GOOD, "allModels": False, "models": ["MX999"]},
        {**GOOD, "allModels": False, "models": "MX68"},
    ]
    for body in bad_bodies:
        with pytest.raises(server.RequestError):
            server.parse_fetch_request(body, ORGS, loader)


def test_resolution_options():
    options = server.resolution_options(86_400, 2)
    assert options["defaultPerformance"] == 1_800
    assert options["defaultUplink"] == 300
    assert options["performance"][0]["calls"] == 96
    with pytest.raises(server.RequestError):
        server.resolution_options(5, 1)


def test_job_progress_prints_one_line_per_item():
    lines = []
    job = server.FetchJob(3, report=lambda text, **_: lines.append(text))
    job.progress(1, 3, "Utilization", "system utilization", "2 appliances with history")
    job.progress(2, 3, "Network", "Branch", "uplink history")
    job.progress(3, 3, "Appliance", "Home", "4 of 4 calls failed", failed=True)
    assert lines == [
        "[1/3] system utilization · 2 appliances with history",
        "[2/3] Branch · uplink history",
        "Warning: [3/3] Home · 4 of 4 calls failed",
    ]
    assert job.phase == "Appliance 3/3: Home"
    assert job.snapshot()["phase"] == "Appliance 3/3: Home"


class FakeLogic:
    def __init__(self, block=False):
        self.block = block

    def get_organizations(self):
        return ORGS

    def get_appliance_devices(self, org_id):
        return INVENTORY

    def fetch(self, org_id, devices, timespan, perf_res, uplink_res, progress=None, cancel=None):
        if self.block:
            while not cancel.is_set():
                time.sleep(0.01)
            raise FetchCancelled()
        if progress:
            progress(1, 1, "Utilization", "system utilization", "ok")
        return {
            "start": "2026-11-03T12:00:00Z",
            "end": "2026-11-04T12:00:00Z",
            "utilization": {"B": [["2026-11-04T00:00:00Z", "2026-11-04T00:20:00Z", 90.0]],
                            "A": [["2026-11-04T00:00:00Z", "2026-11-04T00:20:00Z", 10.0]]},
            "performance": {"A": [["2026-11-03T12:00:00Z", "2026-11-03T13:00:00Z", 20.0]]},
            "uplinks": {"N_1": {"wan1": [["2026-11-04T00:00:00Z", "2026-11-04T00:05:00Z", 37_500_000.0, 75_000_000.0]]}},
            "errors": [],
        }


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((server.HOST, 0))
        return sock.getsockname()[1]


@pytest.fixture
def make_server():
    servers = []

    def start(logic):
        state = server.AppState(logic_factory=lambda: logic, report=lambda *a, **k: None)
        httpd, port, _ = server.create_server(state, free_port())
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        return port

    yield start
    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()


def request(port, method, path, body=None, host=None, content_type="application/json"):
    conn = http.client.HTTPConnection(server.HOST, port, timeout=5)
    headers = {"Host": host or f"127.0.0.1:{port}"}
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = content_type
    conn.request(method, path, body=payload, headers=headers)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response, json.loads(data) if response.getheader("Content-Type", "").startswith("application/json") else data


def wait_for_job(port, job_id, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, job = request(port, "GET", f"/api/jobs/{job_id}")
        if job["state"] != "running":
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_server_serves_page_and_runs_a_fetch_job(make_server):
    logic = FakeLogic()
    port = make_server(logic)
    response, data = request(port, "GET", "/")
    assert response.status == 200
    assert b"/static/app.js" in data
    assert b'class="mark" src="/static/favicon.png"' in data
    assert "default-src 'self'" in response.getheader("Content-Security-Policy")

    icon, icon_body = request(port, "GET", "/favicon.ico")
    assert icon.status == 200
    assert icon.getheader("Content-Type") == "image/png"
    assert icon_body.startswith(b"\x89PNG\r\n\x1a\n")

    _, models = request(port, "GET", "/api/models?org=1")
    assert models["models"][0] == {"model": "MX68", "devices": 1, "networks": ["N_1"]}

    _, options = request(port, "GET", "/api/options?window=86400&devices=2")
    assert options["defaultUplink"] == 300

    response, started = request(port, "POST", "/api/fetch", GOOD)
    assert response.status == 202
    assert started["total"] == 1 + 2 + 2
    job = wait_for_job(port, started["jobId"])
    assert job["state"] == "done"
    result = job["result"]
    assert [d["serial"] for d in result["devices"]] == ["B", "A"]
    assert result["networks"]["N_1"]["uplinks"]["wan1"][0][1:] == [1.0, 2.0]
    assert result["series"]["A"]["performance"][0][2] == 20.0
    assert result["window"] == "1 day"

    response, _ = request(port, "POST", "/api/refresh", {})
    assert response.status == 404


def test_fetch_can_be_cancelled_and_only_one_runs_at_a_time(make_server):
    port = make_server(FakeLogic(block=True))
    _, started = request(port, "POST", "/api/fetch", GOOD)
    response, body = request(port, "POST", "/api/fetch", GOOD)
    assert response.status == 409
    response, _ = request(port, "POST", f"/api/jobs/{started['jobId']}/cancel", {})
    assert response.status == 200
    assert wait_for_job(port, started["jobId"])["state"] == "cancelled"
    response, _ = request(port, "GET", "/api/jobs/not-a-job")
    assert response.status == 404


def test_server_rejects_bad_requests(make_server):
    port = make_server(FakeLogic())
    assert request(port, "GET", "/", host="evil.example")[0].status == 403
    assert request(port, "GET", "/api/models?org=999")[0].status == 400
    assert request(port, "GET", "/api/options?window=abc")[0].status == 400
    assert request(port, "GET", "/../server.py")[0].status == 404
    assert request(port, "POST", "/api/fetch", GOOD, content_type="text/plain")[0].status == 415
    assert request(port, "POST", "/api/fetch", {**GOOD, "timespan": 5})[0].status == 400
