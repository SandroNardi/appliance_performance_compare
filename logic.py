import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import meraki

from core.api import session
from core.logger import logger
import utilization_rules as rules

_MISSING = object()
_SPARE = object()
_SERIAL_QUERY_LIMIT = 50
_WORKERS = 3
API_CALL_COUNTER = 0
_counter_lock = threading.Lock()


def _increment_counter(endpoint):
    global API_CALL_COUNTER
    with _counter_lock:
        API_CALL_COUNTER += 1
        number = API_CALL_COUNTER
    logger.debug(f"[bold cyan][API CALL #{number}][/] SDK Method: [green]{endpoint}[/]")


class FetchCancelled(Exception):
    pass


class PerformanceCall:
    """One performance window for one appliance."""

    def __init__(self, device, window, score=None, spare=False, error=None, skipped=False):
        self.device = device
        self.window = window
        self.score = score
        self.spare = spare
        self.error = error
        self.skipped = skipped


class ScoreRecord:
    """Spare flag and failure count for one appliance.

    first_error is the first failure passed to add(), so the caller decides the order.
    """

    def __init__(self, device):
        self.device = device
        self.failures = 0
        self.first_error = None
        self.spare = False

    def add(self, call):
        if call.spare:
            self.spare = True
        elif call.error:
            self.failures += 1
            if self.first_error is None:
                self.first_error = call.error


class ProjectLogic:
    """Backend for appliance utilization, performance score, and uplink usage."""

    def __init__(self, dashboard=None):
        self.dashboard = dashboard if dashboard is not None else session.get_dashboard()
        self._spare_serials = set()
        self._spare_lock = threading.Lock()
        logger.info("[bold green]ProjectLogic initialized with Meraki SDK.[/]")

    def get_organizations(self):
        _increment_counter("organizations.getOrganizations")
        try:
            return self.dashboard.organizations.getOrganizations(
                perPage=9000,
                total_pages="all",
            ) or []
        except meraki.APIError as exc:
            logger.error(f"[bold red]SDK Error (getOrganizations): {exc}[/]")
            raise

    def get_appliance_devices(self, organization_id):
        _increment_counter("organizations.getOrganizationDevices")
        try:
            return self.dashboard.organizations.getOrganizationDevices(
                organizationId=organization_id,
                productTypes=["appliance"],
                perPage=1000,
                total_pages="all",
            ) or []
        except meraki.APIError as exc:
            logger.error(f"[bold red]SDK Error (getOrganizationDevices): {exc}[/]")
            raise

    def _generated_utilization(self, organization_id, timespan, serials):
        method = getattr(
            self.dashboard.organizations,
            "getOrganizationApplianceDevicesSystemUtilizationByInterval",
            None,
        )
        if method is None:
            method = getattr(
                self.dashboard.appliance,
                "getOrganizationApplianceDevicesSystemUtilizationByInterval",
                None,
            )
        if method is None:
            return _MISSING
        kwargs = {
            "organizationId": organization_id,
            "timespan": timespan,
            "interval": rules.UTILIZATION_INTERVAL,
            "perPage": 1000,
            "total_pages": "all",
        }
        if serials is not None:
            kwargs["serials"] = list(serials)
        return method(**kwargs)

    def _raw_utilization(self, organization_id, timespan, serials):
        metadata = {
            "tags": ["appliance", "monitor", "devices", "system", "utilization", "byInterval"],
            "operation": "getOrganizationApplianceDevicesSystemUtilizationByInterval",
        }
        quoted = urllib.parse.quote(str(organization_id), safe="")
        resource = f"/organizations/{quoted}/appliance/devices/system/utilization/byInterval"
        params = {
            "timespan": timespan,
            "interval": rules.UTILIZATION_INTERVAL,
            "perPage": 1000,
        }
        if serials is not None:
            params["serials[]"] = list(serials)
        return self.dashboard.organizations._session.get_pages(
            metadata,
            resource,
            params,
            total_pages="all",
            direction="next",
        )

    def get_system_utilization(self, organization_id, timespan, serial_filter):
        """serial_filter is None to fetch every appliance, or a sequence of serials to limit the query."""
        _increment_counter(
            "organizations.getOrganizationApplianceDevicesSystemUtilizationByInterval"
        )
        serials = None if serial_filter is None else list(serial_filter)
        try:
            payload = self._generated_utilization(organization_id, timespan, serials)
            if payload is _MISSING:
                payload = self._raw_utilization(organization_id, timespan, serials)
            return rules.utilization_items(payload)
        except meraki.APIError as exc:
            logger.error(
                f"[bold red]SDK Error (getOrganizationApplianceDevicesSystemUtilizationByInterval): {exc}[/]"
            )
            raise

    def get_warm_spare(self, network_id):
        _increment_counter("appliance.getNetworkApplianceWarmSpare")
        try:
            return self.dashboard.appliance.getNetworkApplianceWarmSpare(network_id) or {}
        except meraki.APIError as exc:
            logger.error(f"[bold red]SDK Error (getNetworkApplianceWarmSpare {network_id}): {exc}[/]")
            raise

    def _remember_spare(self, network_id, serial):
        """After a 400, confirm the serial is the warm spare. One info line per appliance."""
        try:
            settings = self.get_warm_spare(network_id)
        except meraki.APIError:
            return False
        if not rules.is_warm_spare(settings, serial):
            return False
        with self._spare_lock:
            first = serial not in self._spare_serials
            self._spare_serials.add(serial)
        if first:
            logger.info(
                f"Performance score is not supported for spare appliance {serial}. "
                "Further calls for this appliance are skipped."
            )
        return True

    def get_performance(self, serial, t0, t1, network_id=""):
        with self._spare_lock:
            if serial in self._spare_serials:
                return _SPARE
        _increment_counter("appliance.getDeviceAppliancePerformance")
        try:
            return self.dashboard.appliance.getDeviceAppliancePerformance(
                serial,
                t0=t0,
                t1=t1,
            )
        except meraki.APIError as exc:
            if getattr(exc, "status", None) == 204:
                return None
            if rules.feature_not_supported(getattr(exc, "status", None), getattr(exc, "message", None)):
                if network_id and self._remember_spare(network_id, serial):
                    return _SPARE
            logger.error(
                f"[bold red]SDK Error (getDeviceAppliancePerformance {serial}): {exc}[/]"
            )
            raise

    def get_uplink_history(self, network_id, t0, t1, resolution):
        _increment_counter("appliance.getNetworkApplianceUplinksUsageHistory")
        try:
            return self.dashboard.appliance.getNetworkApplianceUplinksUsageHistory(
                network_id,
                t0=t0,
                t1=t1,
                resolution=resolution,
            ) or []
        except meraki.APIError as exc:
            logger.error(
                f"[bold red]SDK Error (getNetworkApplianceUplinksUsageHistory {network_id}): {exc}[/]"
            )
            raise

    def fetch(self, organization_id, devices, timespan, performance_resolution,
              uplink_resolution, progress=None, cancel=None, now=None):
        """All three series for the selected appliances over one window.

        progress(done, total, kind, name, outcome, failed) runs once per item:
        utilization, each network's uplink history, and each appliance's score.
        Setting the cancel event stops before the next call and raises FetchCancelled.
        """
        with self._spare_lock:
            self._spare_serials.clear()
        start, end = rules.window_bounds(now or datetime.now(timezone.utc), timespan)
        t0, t1 = rules.iso(start), rules.iso(end)
        slices = rules.performance_slices(start, timespan, performance_resolution)
        network_ids = sorted({d["network_id"] for d in devices if d["network_id"]})
        total = rules.progress_total(devices)
        done = 0
        lock = threading.Lock()
        errors = []

        def add_error(source, message, serial="", device_name=""):
            errors.append({
                "source": source,
                "serial": serial,
                "device_name": device_name,
                "message": message,
            })

        def finish_item(kind, name, outcome, failed=False):
            nonlocal done
            with lock:
                done += 1
                current = done
            if progress:
                progress(current, total, kind, name, outcome, failed)

        def cancelled():
            return cancel is not None and cancel.is_set()

        def check_cancel():
            if cancelled():
                raise FetchCancelled()

        result = {
            "start": t0,
            "end": t1,
            "utilization": {},
            "performance": {},
            "uplinks": {},
            "errors": errors,
        }
        if not devices:
            return result

        serials = tuple(d["serial"] for d in devices)
        serial_filter = serials if len(serials) <= _SERIAL_QUERY_LIMIT else None
        try:
            payload = self.get_system_utilization(organization_id, timespan, serial_filter)
            result["utilization"] = rules.parse_utilization(payload, serials)
            with_history = sum(1 for points in result["utilization"].values() if points)
            finish_item("Utilization", "system utilization", f"{with_history} appliances with history")
        except meraki.APIError as exc:
            add_error("Utilization", str(exc))
            finish_item("Utilization", "system utilization", str(exc), failed=True)

        names_by_network = {d["network_id"]: d["network_name"] for d in devices}

        def load_uplinks(network_id):
            name = names_by_network.get(network_id, network_id)
            if cancelled():
                finish_item("Network", name, "stopped")
                return network_id, None, None
            try:
                payload = self.get_uplink_history(network_id, t0, t1, uplink_resolution)
                finish_item("Network", name, "uplink history")
                return network_id, rules.parse_uplink_history(payload), None
            except meraki.APIError as exc:
                finish_item("Network", name, str(exc), failed=True)
                return network_id, None, str(exc)

        remaining = {d["serial"]: len(slices) for d in devices}
        # Calls finish in thread order, so this record's first_error is whichever failed first.
        progress_records = {}

        def load_score(task):
            device, window = task
            call = PerformanceCall(device, window, skipped=cancelled())
            if not call.skipped:
                try:
                    payload = self.get_performance(
                        device["serial"], window[0], window[1], device["network_id"]
                    )
                    if payload is _SPARE:
                        call.spare = True
                    else:
                        call.score = rules.performance_score(payload)
                except meraki.APIError as exc:
                    call.error = str(exc)
            item = None
            with lock:
                record = progress_records.setdefault(device["serial"], ScoreRecord(device))
                record.add(call)
                remaining[device["serial"]] -= 1
                if remaining[device["serial"]] == 0:
                    name = device["name"]
                    if record.spare:
                        item = (name, "warm spare", False)
                    elif record.failures:
                        item = (name, f"{record.failures} of {len(slices)} calls failed. {record.first_error}", True)
                    elif call.skipped or cancelled():
                        item = (name, "stopped", False)
                    else:
                        item = (name, "performance score", False)
            if item:
                finish_item("Appliance", *item)
            return call

        with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
            for network_id, uplinks, error in pool.map(load_uplinks, network_ids):
                if error:
                    add_error(
                        "Uplink usage",
                        error,
                        device_name=names_by_network.get(network_id, network_id),
                    )
                elif uplinks is not None:
                    result["uplinks"][network_id] = uplinks
            check_cancel()

            score_tasks = [(device, window) for device in devices for window in slices]
            # pool.map yields in task order, so this record's first_error follows the slice list.
            outcomes = {}
            for call in pool.map(load_score, score_tasks):
                points = result["performance"].setdefault(call.device["serial"], [])
                points.append([call.window[0], call.window[1], call.score])
                outcomes.setdefault(call.device["serial"], ScoreRecord(call.device)).add(call)
            check_cancel()

        for record in outcomes.values():
            if record.spare:
                add_error(
                    "Performance score",
                    rules.SPARE_PERFORMANCE_MESSAGE,
                    record.device["serial"],
                    record.device["name"],
                )
        for record in outcomes.values():
            if record.spare or not record.failures:
                continue
            add_error(
                "Performance score",
                f"{record.failures} of {len(slices)} calls failed. First error: {record.first_error}",
                record.device["serial"],
                record.device["name"],
            )
        return result
