"""Rules for appliance utilization, performance score, and uplink usage. No SDK imports."""

from datetime import datetime, timedelta, timezone

UTILIZATION_INTERVAL = 1_200
PERFORMANCE_MIN_SLICE = 1_800
PERFORMANCE_MAX_SLICE = 1_209_600
MAX_PERFORMANCE_CALLS = 2_000
MAX_SERIES_POINTS = 2_016
END_ALIGNMENT = 300
DEFAULT_TIMESPAN = 86_400
DEFAULT_PERFORMANCE_POINTS = 48
DEFAULT_UPLINK_POINTS = 288
SPARE_PERFORMANCE_MESSAGE = (
    "Performance score is not supported on spare appliances. "
    "This device is the warm spare for its network."
)

WINDOW_OPTIONS = (
    ("2 hours", 7_200),
    ("6 hours", 21_600),
    ("1 day", 86_400),
    ("7 days", 604_800),
    ("14 days", 1_209_600),
    ("28 days", 2_419_200),
)
WINDOW_SECONDS = frozenset(seconds for _, seconds in WINDOW_OPTIONS)

PERFORMANCE_RESOLUTIONS = (1_800, 3_600, 7_200, 21_600, 43_200, 86_400, 604_800, 1_209_600)
UPLINK_RESOLUTIONS = (60, 300, 600, 1_800, 3_600, 86_400)


def duration_label(seconds):
    for unit, size in (("day", 86_400), ("hour", 3_600), ("minute", 60)):
        if seconds >= size and seconds % size == 0:
            count = seconds // size
            return f"{count} {unit}{'' if count == 1 else 's'}"
    return f"{seconds} seconds"


def window_by_seconds(timespan):
    for label, seconds in WINDOW_OPTIONS:
        if seconds == timespan:
            return label
    return None


def _device_row(device):
    model = device.get("model")
    serial = device.get("serial")
    if not isinstance(model, str) or not model.strip():
        return None
    if not serial:
        return None
    return {
        "serial": serial,
        "model": model.strip(),
        "name": device.get("name") or serial,
        "network_id": device.get("networkId") or "",
        "network_name": device.get("networkName") or device.get("networkId") or "",
    }


def discover_models(devices):
    """Sorted model strings from appliance inventory rows that have a serial."""
    models = set()
    for device in devices or []:
        row = _device_row(device)
        if row is not None:
            models.add(row["model"])
    return sorted(models)


def select_devices(devices, selected_models, all_models):
    """Normalized rows for the chosen models, or all models, one per serial.

    The inventory is already limited to appliances by the API call.
    """
    selected = {m.strip() for m in selected_models or [] if isinstance(m, str) and m.strip()}
    chosen = []
    seen = set()
    for device in devices or []:
        row = _device_row(device)
        if row is None or row["serial"] in seen:
            continue
        if not all_models and row["model"] not in selected:
            continue
        seen.add(row["serial"])
        chosen.append(row)
    return chosen


def progress_total(devices):
    """Progress ticks: one for utilization, one per network, one per appliance."""
    network_ids = {device["network_id"] for device in devices if device["network_id"]}
    return 1 + len(network_ids) + len(devices)


def performance_call_count(device_count, timespan, resolution):
    return device_count * (timespan // resolution)


def performance_resolutions(timespan, device_count):
    """Score resolutions for a window. One call per appliance per slice; each slice is 30 minutes to 14 days."""
    options = []
    for seconds in PERFORMANCE_RESOLUTIONS:
        if seconds > timespan or timespan % seconds:
            continue
        calls = performance_call_count(device_count, timespan, seconds)
        reason = ""
        if calls > MAX_PERFORMANCE_CALLS:
            reason = f"{calls} calls is over the {MAX_PERFORMANCE_CALLS}-call limit"
        options.append({
            "seconds": seconds,
            "label": duration_label(seconds),
            "points": timespan // seconds,
            "calls": calls,
            "allowed": not reason,
            "reason": reason,
        })
    return options


def uplink_resolutions(timespan):
    """Uplink resolutions the API accepts that fit the window and stay readable on a chart."""
    options = []
    for seconds in UPLINK_RESOLUTIONS:
        if seconds > timespan:
            continue
        points = timespan // seconds
        reason = ""
        if points > MAX_SERIES_POINTS:
            reason = f"{points} points is over the {MAX_SERIES_POINTS}-point limit"
        options.append({
            "seconds": seconds,
            "label": duration_label(seconds),
            "points": points,
            "allowed": not reason,
            "reason": reason,
        })
    return options


def _default_resolution(options, target_points):
    allowed = [o for o in options if o["allowed"]]
    if not allowed:
        return None
    for option in allowed:
        if option["points"] <= target_points:
            return option["seconds"]
    return allowed[-1]["seconds"]


def default_performance_resolution(timespan, device_count):
    return _default_resolution(performance_resolutions(timespan, device_count), DEFAULT_PERFORMANCE_POINTS)


def default_uplink_resolution(timespan):
    return _default_resolution(uplink_resolutions(timespan), DEFAULT_UPLINK_POINTS)


def validate_request(timespan, performance_resolution, uplink_resolution, device_count):
    """Return (ok, message) for a window and the two chosen resolutions."""
    if isinstance(timespan, bool) or timespan not in WINDOW_SECONDS:
        return False, "Window must be one of the listed presets."
    for value in (performance_resolution, uplink_resolution):
        if isinstance(value, bool) or not isinstance(value, int):
            return False, "Resolutions must be whole numbers of seconds."
    performance_options = {o["seconds"]: o for o in performance_resolutions(timespan, device_count)}
    if performance_resolution not in performance_options:
        return False, "Performance resolution is not available for this window."
    if not performance_options[performance_resolution]["allowed"]:
        reason = performance_options[performance_resolution]["reason"]
        return False, f"Performance resolution: {reason}."
    uplink_options = {o["seconds"]: o for o in uplink_resolutions(timespan)}
    if uplink_resolution not in uplink_options:
        return False, "Uplink resolution is not available for this window."
    if not uplink_options[uplink_resolution]["allowed"]:
        return False, f"Uplink resolution: {uplink_options[uplink_resolution]['reason']}."
    return True, ""


def iso(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def window_bounds(now, timespan):
    """Window end rounded down to 5 minutes so successive fetches share the same bounds."""
    now = now.astimezone(timezone.utc)
    epoch = int(now.timestamp())
    end = datetime.fromtimestamp(epoch - epoch % END_ALIGNMENT, tz=timezone.utc)
    return end - timedelta(seconds=timespan), end


def performance_slices(start, timespan, resolution):
    """(t0, t1) ISO pairs that tile the window at the chosen resolution."""
    return [
        (iso(start + timedelta(seconds=offset)), iso(start + timedelta(seconds=offset + resolution)))
        for offset in range(0, timespan, resolution)
    ]


def utilization_items(payload):
    if payload is None:
        return []
    if isinstance(payload, dict):
        return list(payload.get("items") or [])
    if isinstance(payload, list):
        return payload
    return list(payload)


def performance_score(payload):
    if not isinstance(payload, dict):
        return None
    return _number(payload.get("perfScore"))


def feature_not_supported(status, message):
    """True for the 400 the performance endpoint returns when the device cannot serve it."""
    if status != 400 or not isinstance(message, dict):
        return False
    errors = message.get("errors")
    if not isinstance(errors, list):
        return False
    return any(str(item).strip().lower() == "feature not supported" for item in errors)


def is_warm_spare(settings, serial):
    """True when warm spare is enabled and this serial is the spare, not the primary."""
    if not isinstance(settings, dict) or settings.get("enabled") is not True:
        return False
    spare = settings.get("spareSerial")
    if not isinstance(spare, str) or not isinstance(serial, str):
        return False
    return spare.strip().lower() == serial.strip().lower()


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def parse_utilization(payload, serials):
    """{serial: [[startTs, endTs, percent], ...]} for the selected serials, oldest first."""
    allowed = set(serials)
    series = {}
    for item in utilization_items(payload):
        if not isinstance(item, dict) or item.get("serial") not in allowed:
            continue
        points = series.setdefault(item["serial"], [])
        for interval in item.get("intervals") or []:
            if not isinstance(interval, dict) or not interval.get("startTs"):
                continue
            average = (interval.get("utilization") or {}).get("average") or {}
            points.append([interval["startTs"], interval.get("endTs"), _number(average.get("percentage"))])
    for points in series.values():
        points.sort(key=lambda p: p[0])
    return series


def _interface_rows(entry):
    for key in ("byInterface", "byUplink", "uplinks"):
        value = entry.get(key)
        if isinstance(value, list):
            return value
    return []


def parse_uplink_history(payload):
    """{interface: [[startTime, endTime, sentBytes, receivedBytes], ...]}, oldest first."""
    series = {}
    for entry in payload or []:
        if not isinstance(entry, dict) or not entry.get("startTime"):
            continue
        for row in _interface_rows(entry):
            if not isinstance(row, dict):
                continue
            name = row.get("interface") or row.get("uplink")
            if not name:
                continue
            series.setdefault(name, []).append(
                [entry["startTime"], entry.get("endTime"), _number(row.get("sent")), _number(row.get("received"))]
            )
    for points in series.values():
        points.sort(key=lambda p: p[0])
    return dict(sorted(series.items()))


def to_mbps(byte_count, seconds):
    if byte_count is None or not seconds:
        return None
    return byte_count * 8 / seconds / 1_000_000


def uplink_mbps(uplinks, resolution):
    """{interface: [[start, sent Mbps, received Mbps], ...]}."""
    return {
        name: [[start, to_mbps(sent, resolution), to_mbps(received, resolution)]
               for start, _end, sent, received in points]
        for name, points in uplinks.items()
    }


def uplink_totals(uplinks, resolution):
    """[[start, total Mbps], ...] summed over interfaces, plus total bytes."""
    by_start = {}
    total_bytes = 0.0
    for points in uplinks.values():
        for start, _end, sent, received in points:
            if sent is None and received is None:
                continue
            value = (sent or 0) + (received or 0)
            total_bytes += value
            by_start[start] = by_start.get(start, 0.0) + value
    series = [[start, to_mbps(value, resolution)] for start, value in sorted(by_start.items())]
    return series, total_bytes


def _mean_and_peak(values):
    values = [v for v in values if v is not None]
    if not values:
        return None, None
    return round(sum(values) / len(values), 1), round(max(values), 1)


def _latest(points, index):
    for point in reversed(points):
        if point[index] is not None:
            return round(point[index], 1)
    return None


def summarize(devices, utilization, performance, uplinks_by_network, uplink_resolution):
    """One summary per selected appliance, ranked by the higher of its utilization and score peaks."""
    network_members = {}
    for device in devices:
        network_members.setdefault(device["network_id"], []).append(device["serial"])

    summaries = []
    for device in devices:
        util_points = utilization.get(device["serial"], [])
        perf_points = performance.get(device["serial"], [])
        uplinks = uplinks_by_network.get(device["network_id"], {})
        totals, total_bytes = uplink_totals(uplinks, uplink_resolution)
        util_mean, util_peak = _mean_and_peak(p[2] for p in util_points)
        perf_mean, perf_peak = _mean_and_peak(p[2] for p in perf_points)
        up_mean, up_peak = _mean_and_peak(p[1] for p in totals)
        summaries.append({
            "serial": device["serial"],
            "device_name": device["name"],
            "model": device["model"],
            "network_id": device["network_id"],
            "network_name": device["network_name"],
            "shared_network": len(network_members.get(device["network_id"], [])) > 1,
            "util_intervals": sum(1 for p in util_points if p[2] is not None),
            "util_mean": util_mean,
            "util_peak": util_peak,
            "util_latest": _latest(util_points, 2),
            "perf_points": sum(1 for p in perf_points if p[2] is not None),
            "perf_mean": perf_mean,
            "perf_peak": perf_peak,
            "perf_latest": _latest(perf_points, 2),
            "uplink_mean_mbps": up_mean,
            "uplink_peak_mbps": up_peak,
            "uplink_total_gb": round(total_bytes / 1_000_000_000, 2) if totals else None,
            "uplinks": sorted(uplinks),
        })

    def rank(item):
        peaks = [v for v in (item["util_peak"], item["perf_peak"]) if v is not None]
        return (-(max(peaks) if peaks else -1), item["device_name"])

    summaries.sort(key=rank)
    return summaries
