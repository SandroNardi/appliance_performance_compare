import ast
from datetime import datetime, timezone
from pathlib import Path

import utilization_rules as rules

DEVICES = [
    {"serial": "A", "model": "MX68", "networkId": "N_1", "name": "Branch MX"},
    {"serial": "A2", "model": "MX68", "networkId": "N_1", "name": "Spare"},
    {"serial": "B", "model": "Z3", "networkId": "N_2", "name": "Home"},
    {"serial": "C", "model": "VMX", "networkId": "N_3", "name": "Cloud"},
    {"model": "MX67", "networkId": "N_4"},
    {"serial": "D", "model": "  ", "networkId": "N_5"},
]


def test_rules_module_has_no_sdk_or_web_import():
    tree = ast.parse(Path(rules.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported.isdisjoint({"meraki", "streamlit", "os", "pandas"})


def test_discover_models_keeps_every_appliance_model_with_a_serial():
    assert rules.discover_models(DEVICES) == ["MX68", "VMX", "Z3"]
    assert rules.discover_models([]) == []


def test_subset_is_an_exact_model_match_and_all_includes_z_and_vmx():
    assert rules.select_devices(DEVICES, ["mx68"], all_models=False) == []
    assert [d["serial"] for d in rules.select_devices(DEVICES, ["Z3"], all_models=False)] == ["B"]
    assert [d["serial"] for d in rules.select_devices(DEVICES, [], all_models=True)] == ["A", "A2", "B", "C"]


def test_performance_resolutions_stay_between_30_minutes_and_the_window():
    options = rules.performance_resolutions(86_400, device_count=2)
    seconds = [o["seconds"] for o in options]
    assert seconds[0] == 1_800
    assert seconds[-1] == 86_400
    assert all(86_400 % s == 0 for s in seconds)
    first = options[0]
    assert first["points"] == 48 and first["calls"] == 96 and first["allowed"]


def test_performance_resolution_over_the_call_budget_is_disabled():
    options = {o["seconds"]: o for o in rules.performance_resolutions(1_209_600, device_count=10)}
    assert not options[1_800]["allowed"]
    assert "limit" in options[1_800]["reason"]
    assert options[86_400]["allowed"]
    assert 2_419_200 not in options
    assert max(options) == 1_209_600


def test_uplink_resolutions_use_api_values_that_fit_the_window():
    options = {o["seconds"]: o for o in rules.uplink_resolutions(7_200)}
    assert sorted(options) == [60, 300, 600, 1_800, 3_600]
    long_window = {o["seconds"]: o for o in rules.uplink_resolutions(2_419_200)}
    assert not long_window[60]["allowed"]
    assert long_window[1_800]["allowed"]


def test_defaults_and_request_validation():
    assert rules.default_performance_resolution(86_400, 1) == 1_800
    assert rules.default_uplink_resolution(86_400) == 300
    assert rules.validate_request(86_400, 1_800, 300, 1) == (True, "")
    assert rules.validate_request(86_400, 1_200, 300, 1)[0] is False
    assert rules.validate_request(86_400, 1_800, 120, 1)[0] is False
    assert rules.validate_request(1_209_600, 1_800, 3_600, 10)[0] is False
    assert rules.validate_request(12, 1_800, 300, 1)[0] is False
    assert rules.validate_request(86_400, True, 300, 1)[0] is False


def test_window_bounds_and_slices_tile_the_window():
    now = datetime(2026, 11, 4, 12, 7, 31, tzinfo=timezone.utc)
    start, end = rules.window_bounds(now, 7_200)
    assert rules.iso(end) == "2026-11-04T12:05:00Z"
    assert rules.iso(start) == "2026-11-04T10:05:00Z"
    slices = rules.performance_slices(start, 7_200, 1_800)
    assert len(slices) == 4
    assert slices[0] == ("2026-11-04T10:05:00Z", "2026-11-04T10:35:00Z")
    assert slices[-1][1] == "2026-11-04T12:05:00Z"


def test_parse_utilization_keeps_selected_serials_in_time_order():
    payload = {
        "items": [
            {"serial": "B", "intervals": [
                {"startTs": "t2", "endTs": "t3", "utilization": {"average": {"percentage": 61}}},
                {"startTs": "t1", "endTs": "t2", "utilization": {"average": {"percentage": 40}}},
            ]},
            {"serial": "OTHER", "intervals": [{"startTs": "t1", "utilization": {"average": {"percentage": 1}}}]},
        ]
    }
    series = rules.parse_utilization(payload, ["B"])
    assert series == {"B": [["t1", "t2", 40.0], ["t2", "t3", 61.0]]}
    assert rules.parse_utilization(None, ["B"]) == {}


def test_parse_uplink_history_and_mbps():
    payload = [
        {"startTime": "t2", "endTime": "t3", "byInterface": [{"interface": "wan1", "sent": 75_000_000, "received": 150_000_000}]},
        {"startTime": "t1", "endTime": "t2", "byInterface": [
            {"interface": "wan1", "sent": 0, "received": None},
            {"interface": "wan2", "sent": 10, "received": 20},
        ]},
    ]
    uplinks = rules.parse_uplink_history(payload)
    assert list(uplinks) == ["wan1", "wan2"]
    assert uplinks["wan1"][0] == ["t1", "t2", 0.0, None]
    mbps = rules.uplink_mbps(uplinks, 300)
    assert mbps["wan1"][1] == ["t2", 2.0, 4.0]
    totals, total_bytes = rules.uplink_totals(uplinks, 300)
    assert totals[-1] == ["t2", 6.0]
    assert total_bytes == 225_000_030


def test_feature_not_supported_is_the_performance_400():
    body = {"errors": ["Feature not supported"]}
    assert rules.feature_not_supported(400, body) is True
    assert rules.feature_not_supported(400, {"errors": ["Invalid timespan"]}) is False
    assert rules.feature_not_supported(404, body) is False
    assert rules.feature_not_supported(400, "Feature not supported") is False


def test_warm_spare_matches_the_spare_serial_only():
    settings = {"enabled": True, "primarySerial": "A", "spareSerial": "B"}
    assert rules.is_warm_spare(settings, "B") is True
    assert rules.is_warm_spare(settings, "b") is True
    assert rules.is_warm_spare(settings, "A") is False
    assert rules.is_warm_spare({"enabled": False, "spareSerial": "B"}, "B") is False
    assert rules.is_warm_spare({}, "B") is False


def test_performance_score_reads_perf_score():
    assert rules.performance_score(None) is None
    assert rules.performance_score({"perfScore": 0}) == 0
    assert rules.performance_score({"perfScore": True}) is None


def test_summaries_rank_by_the_higher_peak_and_mark_shared_networks():
    devices = rules.select_devices(DEVICES, [], all_models=True)
    utilization = {"A": [["t1", "t2", 20.0], ["t2", "t3", 30.0]], "B": [["t1", "t2", 50.0]]}
    performance = {"A": [["t1", "t3", 90.0]], "B": [["t1", "t3", 10.0]], "C": [["t1", "t3", None]]}
    uplinks = {"N_1": {"wan1": [["t1", "t2", 30_000_000.0, 10_000_000.0]]}}
    summaries = rules.summarize(devices, utilization, performance, uplinks, 300)
    assert [s["serial"] for s in summaries][:2] == ["A", "B"]
    first = summaries[0]
    assert first["util_peak"] == 30.0 and first["util_mean"] == 25.0 and first["util_latest"] == 30.0
    assert first["perf_peak"] == 90.0
    assert first["uplink_peak_mbps"] == 1.1
    assert first["uplink_total_gb"] == 0.04
    assert first["shared_network"] is True
    cloud = next(s for s in summaries if s["serial"] == "C")
    assert cloud["perf_points"] == 0 and cloud["uplink_total_gb"] is None and cloud["shared_network"] is False


def test_labels():
    assert rules.window_by_seconds(86_400) == "1 day"
    assert rules.window_by_seconds(12) is None
    assert rules.duration_label(1_800) == "30 minutes"
    assert rules.duration_label(3_600) == "1 hour"
    assert rules.duration_label(1_209_600) == "14 days"
