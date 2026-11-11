import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import meraki
import pytest

import utilization_rules as rules
from logic import FetchCancelled, ProjectLogic

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
DEVICES = rules.select_devices(
    [
        {"serial": "A", "model": "MX68", "networkId": "N_1", "name": "Branch"},
        {"serial": "B", "model": "MX68", "networkId": "N_1", "name": "Branch spare"},
        {"serial": "C", "model": "Z3", "networkId": "N_2", "name": "Home"},
    ],
    [],
    all_models=True,
)


def api_error(status, message="boom"):
    response = SimpleNamespace(status_code=status, reason=message, text=message, json=lambda: {"errors": [message]})
    return meraki.APIError({"tags": ["appliance"], "operation": "op"}, response)


class FakeDashboard:
    def __init__(self, failing_serial=None):
        self.performance_calls = []
        self.uplink_calls = []
        self.lock = threading.Lock()
        self.failing_serial = failing_serial
        self.organizations = SimpleNamespace(
            getOrganizationApplianceDevicesSystemUtilizationByInterval=self.utilization,
        )
        self.appliance = SimpleNamespace(
            getDeviceAppliancePerformance=self.performance,
            getNetworkApplianceUplinksUsageHistory=self.uplinks,
        )

    def utilization(self, **kwargs):
        assert kwargs["interval"] == 1200
        return {"items": [{"serial": "A", "intervals": [
            {"startTs": "2026-11-04T11:00:00Z", "endTs": "2026-11-04T11:20:00Z", "utilization": {"average": {"percentage": 55}}},
        ]}]}

    def performance(self, serial, t0, t1):
        with self.lock:
            self.performance_calls.append((serial, t0, t1))
        if serial == self.failing_serial:
            raise api_error(500)
        return {"perfScore": 42}

    def uplinks(self, network_id, t0, t1, resolution):
        with self.lock:
            self.uplink_calls.append((network_id, t0, t1, resolution))
        return [{"startTime": t0, "endTime": t1, "byInterface": [{"interface": "wan1", "sent": 1, "received": 2}]}]


def test_fetch_slices_performance_and_calls_uplinks_once_per_network():
    dashboard = FakeDashboard()
    progress = []
    result = ProjectLogic(dashboard).fetch(
        "1", DEVICES, 7_200, 1_800, 300, progress=lambda *a: progress.append(a), now=NOW
    )
    assert len(dashboard.performance_calls) == 3 * 4
    assert sorted({c[1] for c in dashboard.performance_calls}) == [
        "2026-11-04T10:00:00Z", "2026-11-04T10:30:00Z", "2026-11-04T11:00:00Z", "2026-11-04T11:30:00Z",
    ]
    assert sorted(c[0] for c in dashboard.uplink_calls) == ["N_1", "N_2"]
    assert dashboard.uplink_calls[0][1:] == ("2026-11-04T10:00:00Z", "2026-11-04T12:00:00Z", 300)
    assert [p[2] for p in result["performance"]["A"]] == [42.0] * 4
    assert result["utilization"]["A"][0][2] == 55.0
    assert progress[0][2] == "Utilization"
    assert len(progress) == 1 + 2 + 3
    assert progress[-1][:2] == (6, 6)
    assert {item[2] for item in progress} == {"Utilization", "Network", "Appliance"}
    assert result["errors"] == []


def test_failed_score_calls_become_one_error_per_appliance():
    result = ProjectLogic(FakeDashboard(failing_serial="C")).fetch("1", DEVICES, 7_200, 1_800, 300, now=NOW)
    assert [e["serial"] for e in result["errors"]] == ["C"]
    assert result["errors"][0]["message"].startswith("4 of 4 calls failed")
    assert [p[2] for p in result["performance"]["C"]] == [None] * 4


def test_unsupported_performance_on_a_warm_spare_is_reported_as_a_spare():
    class SpareDashboard(FakeDashboard):
        def __init__(self):
            super().__init__()
            self.warm_spare_calls = []
            self.appliance = SimpleNamespace(
                getDeviceAppliancePerformance=self.performance,
                getNetworkApplianceUplinksUsageHistory=self.uplinks,
                getNetworkApplianceWarmSpare=self.warm_spare,
            )

        def performance(self, serial, t0, t1):
            with self.lock:
                self.performance_calls.append((serial, t0, t1))
            if serial == "B":
                raise api_error(400, "Feature not supported")
            return {"perfScore": 42}

        def warm_spare(self, network_id):
            with self.lock:
                self.warm_spare_calls.append(network_id)
            return {"enabled": True, "primarySerial": "A", "spareSerial": "B"}

    dashboard = SpareDashboard()
    result = ProjectLogic(dashboard).fetch("1", DEVICES, 7_200, 1_800, 300, now=NOW)
    spare_notes = [e for e in result["errors"] if e["serial"] == "B"]
    assert len(spare_notes) == 1
    assert spare_notes[0]["message"] == rules.SPARE_PERFORMANCE_MESSAGE
    assert "400" not in spare_notes[0]["message"]
    assert [p[2] for p in result["performance"]["A"]] == [42.0] * 4
    assert all(p[2] is None for p in result["performance"]["B"])
    assert dashboard.warm_spare_calls == ["N_1"]
    assert 1 <= len([c for c in dashboard.performance_calls if c[0] == "B"]) <= 4


def test_unsupported_performance_on_a_primary_stays_an_error():
    class PrimaryDashboard(FakeDashboard):
        def __init__(self):
            super().__init__()
            self.appliance = SimpleNamespace(
                getDeviceAppliancePerformance=self.performance,
                getNetworkApplianceUplinksUsageHistory=self.uplinks,
                getNetworkApplianceWarmSpare=lambda network_id: {"enabled": True, "primarySerial": "A", "spareSerial": "B"},
            )

        def performance(self, serial, t0, t1):
            if serial == "A":
                raise api_error(400, "Feature not supported")
            return {"perfScore": 7}

    result = ProjectLogic(PrimaryDashboard()).fetch("1", [DEVICES[0]], 7_200, 3_600, 300, now=NOW)
    assert len(result["errors"]) == 1
    assert result["errors"][0]["message"].startswith("2 of 2 calls failed")
    assert "spare" not in result["errors"][0]["message"]


def test_cancel_stops_the_fetch():
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(FetchCancelled):
        ProjectLogic(FakeDashboard()).fetch("1", DEVICES, 7_200, 1_800, 300, cancel=cancel, now=NOW)


def test_organization_and_device_lists_are_fetched_live_each_time():
    class CountingDashboard:
        def __init__(self):
            self.org_calls = 0
            self.device_calls = 0
            self.organizations = SimpleNamespace(
                getOrganizations=self.orgs,
                getOrganizationDevices=self.devices,
            )

        def orgs(self, **kwargs):
            self.org_calls += 1
            return [{"id": "1", "name": "A"}]

        def devices(self, **kwargs):
            self.device_calls += 1
            return []

    dashboard = CountingDashboard()
    logic = ProjectLogic(dashboard)
    logic.get_organizations()
    logic.get_organizations()
    logic.get_appliance_devices("1")
    logic.get_appliance_devices("1")
    assert dashboard.org_calls == 2
    assert dashboard.device_calls == 2
