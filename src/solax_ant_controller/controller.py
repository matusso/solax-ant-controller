#!/usr/bin/env python3
"""
SolaXCloud + Braiins OS miner controller.

What it does:
  - Polls SolaXCloud every 5 minutes by default.
  - Reads battery SOC and battery power.
  - Detects whether battery is draining.
  - Logs readable one-line status to console.
  - Stores full JSONL logs into file.
  - Logs into Braiins OS with username/password.
  - Uses returned token as:
        Authorization: <TOKEN>
    NOT:
        Authorization: Bearer <TOKEN>
  - If requested Braiins power target is below miner minimum, pauses mining.

Control logic:
  - SOC below 75%:
      pause mining
  - SOC >= 75% and battery is not charging:
      pause mining
  - SOC >= 75% and battery is charging:
      resume/start miner and increase target step by step while keeping battery charging

Required env vars:
  SOLAX_TOKEN_ID="your-solax-token"
  SOLAX_WIFI_SN="your-solax-wifi-sn"
  BRAIINS_BASE_URL="http://base.url"
  BRAIINS_USERNAME="root"
  BRAIINS_PASSWORD="your-password"

Optional env vars:
  SOLAX_API_URL="https://global.solaxcloud.com/api/v2/dataAccess/realtimeInfo/get"
  SOLAX_POLL_INTERVAL="300"
  SOLAX_TIMEOUT="600"

  BRAIINS_TIMEOUT="10"

  MINER_ENABLE_CONTROL="true"
  MINER_DRY_RUN="false"

  MINER_REFERENCE="1"
    1 = nominal/sticker rating
    2 = min target
    3 = max target
    4 = current target

  MINER_FULL_PERCENT="100"
  MINER_START_SOC_PERCENT="75"
  MINER_START_PERCENT="50"
  MINER_RAMP_UP_PERCENT_STEP="10"
  MINER_BATTERY_CHARGE_RESERVE_W="0"
  MINER_MIN_POWER_W=""
  MINER_MAX_POWER_W=""
  MINER_RATED_POWER_W=""

  SOLAX_LOG_LEVEL="INFO"
  SOLAX_LOG_FILE="./solax-miner-controller.jsonl"

Install:
  pip install requests

Run:
  export SOLAX_TOKEN_ID='TOKEN_ID'
  export SOLAX_WIFI_SN='WIFI_SN'
  export BRAIINS_BASE_URL='http://192.168.1.164'
  export BRAIINS_USERNAME='root'
  export BRAIINS_PASSWORD='password'
  python3 solax_miner_controller.py
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Optional

import requests
from requests import Response
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


DEFAULT_SOLAX_API_URL = "https://global.solaxcloud.com/api/v2/dataAccess/realtimeInfo/get"


INVERTER_STATUS_MAP = {
    "100": "Waiting for operation",
    "101": "Self-test",
    "102": "Normal",
    "103": "Recoverable fault",
    "104": "Permanent fault",
    "105": "Firmware upgrade",
    "106": "EPS detection",
    "107": "Off-grid",
    "108": "Self-test mode",
    "109": "Sleep mode",
    "110": "Standby mode",
    "111": "Photovoltaic wake-up battery mode",
    "112": "Generator detection mode",
    "113": "Generator mode",
    "114": "Fast shutdown standby mode",
    "130": "VPP mode",
    "131": "TOU-Self use",
    "132": "TOU-Charging",
    "133": "TOU-Discharging",
}


BATTERY_STATUS_MAP = {
    "0": "Normal",
    "1": "Fault",
    "2": "Disconnected",
}


MINER_STATUS_MAP = {
    0: "Unknown",
    1: "Starting",
    2: "Running",
    3: "Stopping",
    4: "Stopped",
    5: "Fault",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        reserved = {
            "name",
            "msg",
            "args",
            "levelname",
            "levelno",
            "pathname",
            "filename",
            "module",
            "exc_info",
            "exc_text",
            "stack_info",
            "lineno",
            "funcName",
            "created",
            "msecs",
            "relativeCreated",
            "thread",
            "threadName",
            "processName",
            "process",
            "message",
        }

        for key, value in record.__dict__.items():
            if key not in reserved and not key.startswith("_"):
                payload[key] = value

        return json.dumps(payload, default=str, ensure_ascii=False)


class ReadableFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        parts = [
            ts,
            f"{record.levelname:<7}",
            record.getMessage(),
        ]

        important_fields = [
            "battery_soc_percent",
            "battery_status",
            "battery_power_w",
            "battery_draining",
            "ac_output_power_w",
            "pv_total_power_w",
            "eps_total_power_w",
            "grid_feed_in_power_w",
            "inverter_status",
            "miner_action",
            "miner_target_percent",
            "miner_last_state",
            "miner_applied",
            "miner_host",
            "miner_hostname",
            "miner_status",
            "miner_status_text",
            "miner_power_w",
            "miner_hashrate_ths",
            "miner_attempted_power_w",
            "miner_min_power_w",
            "miner_max_power_w",
            "miner_decision_reason",
            "same_upload_time_as_previous_poll",
            "upload_time",
        ]

        fields = []

        for field in important_fields:
            value = getattr(record, field, None)
            if value is not None:
                fields.append(f"{field}={value}")

        if fields:
            parts.append("| " + " ".join(fields))

        if record.exc_info:
            exception = self.formatException(record.exc_info).replace("\n", " | ")
            parts.append(f"| error={exception}")

        return " ".join(parts)


def setup_logging() -> logging.Logger:
    log_level = os.getenv("SOLAX_LOG_LEVEL", "INFO").upper()
    log_file = os.getenv("SOLAX_LOG_FILE", "./solax-miner-controller.jsonl")

    logger = logging.getLogger("solax.miner.controller")
    logger.setLevel(log_level)
    logger.handlers.clear()
    logger.propagate = False

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(ReadableFormatter())

    file_handler = RotatingFileHandler(
        filename=log_file,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(JsonFormatter())

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)

    return logger


@dataclass
class SolaxSnapshot:
    inverter_sn: Optional[str]
    wifi_sn: Optional[str]
    upload_time: Optional[str]
    utc_datetime: Optional[str]

    inverter_status_code: Optional[str]
    inverter_status: str

    battery_status_code: Optional[str]
    battery_status: str
    battery_soc_percent: Optional[float]
    battery_power_w: Optional[float]
    battery_draining: bool

    ac_output_power_w: Optional[float]
    pv_total_power_w: float
    pv1_power_w: Optional[float]
    pv2_power_w: Optional[float]
    pv3_power_w: Optional[float]
    pv4_power_w: Optional[float]

    grid_feed_in_power_w: Optional[float]
    meter2_power_w: Optional[float]

    eps1_power_w: Optional[float]
    eps2_power_w: Optional[float]
    eps3_power_w: Optional[float]
    eps_total_power_w: float

    yield_today_kwh: Optional[float]
    yield_total_kwh: Optional[float]
    grid_import_energy_kwh: Optional[float]
    grid_export_energy_kwh: Optional[float]


@dataclass
class MinerDecision:
    action: str
    target_percent: Optional[float]
    reason: str


@dataclass
class MinerPowerLimits:
    min_power_w: Optional[float] = None
    max_power_w: Optional[float] = None
    rated_power_w: Optional[float] = None

    def update_from_braiins_range_error(
        self,
        requested_percent: Optional[float],
        attempted_w: Optional[int],
        min_w: Optional[int],
        max_w: Optional[int],
    ) -> None:
        if min_w is not None:
            self.min_power_w = float(min_w)

        if max_w is not None:
            self.max_power_w = float(max_w)

        if (
            requested_percent is not None
            and requested_percent > 0
            and attempted_w is not None
            and attempted_w > 0
        ):
            self.rated_power_w = float(attempted_w) * 100.0 / requested_percent

    def minimum_percent(self) -> Optional[float]:
        if self.min_power_w is None or self.rated_power_w in {None, 0}:
            return None

        return self.min_power_w * 100.0 / self.rated_power_w

    def maximum_percent(self) -> Optional[float]:
        if self.max_power_w is None or self.rated_power_w in {None, 0}:
            return None

        return self.max_power_w * 100.0 / self.rated_power_w

    def power_for_percent(self, percent: float) -> Optional[float]:
        if self.rated_power_w is None:
            return None

        return self.rated_power_w * percent / 100.0

    def percent_for_power(self, power_w: float) -> Optional[float]:
        if self.rated_power_w in {None, 0}:
            return None

        return power_w * 100.0 / self.rated_power_w

    def clamp_percent(self, percent: float) -> float:
        target = percent
        min_percent = self.minimum_percent()
        max_percent = self.maximum_percent()

        if min_percent is not None and 0 < target < min_percent:
            target = min_percent

        if max_percent is not None and target > max_percent:
            target = max_percent

        return round(target, 3)


class HttpClientMixin:
    @staticmethod
    def build_session() -> requests.Session:
        session = requests.Session()

        retry = Retry(
            total=3,
            connect=3,
            read=3,
            status=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "POST", "PUT", "PATCH"]),
            raise_on_status=False,
        )

        adapter = HTTPAdapter(
            max_retries=retry,
            pool_connections=8,
            pool_maxsize=16,
        )

        session.mount("https://", adapter)
        session.mount("http://", adapter)

        return session


class SolaxClient(HttpClientMixin):
    def __init__(
        self,
        api_url: str,
        token_id: str,
        wifi_sn: str,
        timeout: float,
    ) -> None:
        self.api_url = api_url
        self.token_id = token_id
        self.wifi_sn = wifi_sn
        self.timeout = timeout
        self.session = self.build_session()

    def fetch_realtime_info(self) -> dict[str, Any]:
        headers = {
            "Content-Type": "application/json",
            "tokenId": self.token_id,
        }

        body = {
            "wifiSn": self.wifi_sn,
        }

        response: Response = self.session.post(
            self.api_url,
            headers=headers,
            json=body,
            timeout=self.timeout,
        )

        response.raise_for_status()

        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"SolaXCloud returned non-JSON response: {response.text[:500]}"
            ) from exc

        if not isinstance(payload, dict):
            raise RuntimeError(
                f"SolaXCloud returned unexpected payload type: {type(payload).__name__}"
            )

        success = payload.get("success")
        code = payload.get("code")
        exception = payload.get("exception")

        if success is not True or code != 0:
            raise RuntimeError(
                f"SolaXCloud API error: success={success}, code={code}, exception={exception}"
            )

        result = payload.get("result")

        if not isinstance(result, dict):
            raise RuntimeError("SolaXCloud response does not contain result object")

        return result


class BraiinsClient(HttpClientMixin):
    def __init__(
        self,
        base_url: str,
        timeout: float,
        username: str,
        password: str,
        dry_run: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.username = username
        self.password = password
        self.dry_run = dry_run
        self.session = self.build_session()
        self.auth_token: Optional[str] = None
        self.auth_token_expire_at_monotonic: Optional[float] = None

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def login(self) -> str:
        response = self.session.post(
            self._url("/api/v1/auth/login"),
            json={
                "username": self.username,
                "password": self.password,
            },
            timeout=self.timeout,
            headers={
                "Content-Type": "application/json",
            },
        )

        if response.status_code != 200:
            raise RuntimeError(
                f"Braiins login failed: status={response.status_code} body={response.text[:500]}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"Braiins login returned non-JSON response: {response.text[:500]}"
            ) from exc

        token = payload.get("token")
        if not token:
            raise RuntimeError(
                f"Braiins login response does not contain token: {payload}"
            )

        timeout_s = payload.get("timeout_s", 3600)

        try:
            timeout_seconds = int(timeout_s)
        except (TypeError, ValueError):
            timeout_seconds = 3600

        self.auth_token = token
        self.auth_token_expire_at_monotonic = (
            time.monotonic() + max(60, timeout_seconds - 60)
        )

        # Correct for your miner:
        #   Authorization: <TOKEN>
        # Not:
        #   Authorization: Bearer <TOKEN>
        self.session.headers.update(
            {
                "Authorization": self.auth_token,
            }
        )

        return token

    def ensure_authenticated(self) -> None:
        token_missing = not self.auth_token

        token_expired = (
            self.auth_token_expire_at_monotonic is not None
            and time.monotonic() >= self.auth_token_expire_at_monotonic
        )

        if token_missing or token_expired:
            self.login()

    def request(
        self,
        method: str,
        path: str,
        json_body: Any = None,
        expected_statuses: tuple[int, ...] = (200, 204),
        retry_auth: bool = True,
    ) -> Any:
        method = method.upper()

        if path != "/api/v1/auth/login":
            self.ensure_authenticated()

        if self.dry_run and method in {"PUT", "PATCH", "POST", "DELETE"}:
            return {
                "dry_run": True,
                "method": method,
                "path": path,
                "json": json_body,
            }

        headers = {
            "Content-Type": "application/json",
            "Authorization": self.auth_token or "",
        }

        response = self.session.request(
            method=method,
            url=self._url(path),
            json=json_body,
            timeout=self.timeout,
            headers=headers,
        )

        if response.status_code == 401 and retry_auth:
            self.auth_token = None
            self.auth_token_expire_at_monotonic = None
            self.session.headers.pop("Authorization", None)

            self.login()

            headers = {
                "Content-Type": "application/json",
                "Authorization": self.auth_token or "",
            }

            response = self.session.request(
                method=method,
                url=self._url(path),
                json=json_body,
                timeout=self.timeout,
                headers=headers,
            )

        if response.status_code not in expected_statuses:
            raise RuntimeError(
                f"Braiins API error: method={method} path={path} "
                f"status={response.status_code} body={response.text[:500]}"
            )

        if response.status_code == 204 or not response.text.strip():
            return None

        try:
            return response.json()
        except ValueError:
            return response.text

    def get_miner_details(self) -> Any:
        return self.request("GET", "/api/v1/miner/details")

    def get_miner_stats(self) -> Any:
        return self.request("GET", "/api/v1/miner/stats")

    def get_performance_mode(self) -> Any:
        return self.request("GET", "/api/v1/performance/mode")

    def set_relative_power_target(self, percentage: float, reference: int) -> Any:
        return self.request(
            "PATCH",
            "/api/v1/performance/power-target/relative",
            json_body={
                "percentage": percentage,
                "reference": reference,
            },
        )

    def stop_mining(self) -> Any:
        return self.request("PUT", "/api/v1/actions/stop")

    def pause_mining(self) -> Any:
        return self.request("PUT", "/api/v1/actions/pause")

    def start_mining(self) -> Any:
        return self.request("PUT", "/api/v1/actions/start")

    def resume_mining(self) -> Any:
        return self.request("PUT", "/api/v1/actions/resume")


def to_float(value: Any) -> Optional[float]:
    if value is None:
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_int(value: Any) -> Optional[int]:
    if value is None:
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def sum_optional(*values: Optional[float]) -> float:
    return sum(value for value in values if value is not None)


def parse_snapshot(result: dict[str, Any]) -> SolaxSnapshot:
    inverter_status_code = (
        str(result.get("inverterStatus"))
        if result.get("inverterStatus") is not None
        else None
    )

    battery_status_code = (
        str(result.get("batStatus"))
        if result.get("batStatus") is not None
        else None
    )

    pv1 = to_float(result.get("powerdc1"))
    pv2 = to_float(result.get("powerdc2"))
    pv3 = to_float(result.get("powerdc3"))
    pv4 = to_float(result.get("powerdc4"))

    eps1 = to_float(result.get("peps1"))
    eps2 = to_float(result.get("peps2"))
    eps3 = to_float(result.get("peps3"))

    battery_power_w = to_float(result.get("batPower"))

    # Most SolaX setups report negative batPower when battery discharges.
    battery_draining = battery_power_w is not None and battery_power_w < 0

    return SolaxSnapshot(
        inverter_sn=result.get("inverterSN") or result.get("inverterSn"),
        wifi_sn=result.get("sn"),
        upload_time=result.get("uploadTime"),
        utc_datetime=result.get("utcDateTime"),

        inverter_status_code=inverter_status_code,
        inverter_status=INVERTER_STATUS_MAP.get(inverter_status_code, "Unknown"),

        battery_status_code=battery_status_code,
        battery_status=BATTERY_STATUS_MAP.get(battery_status_code, "Unknown"),
        battery_soc_percent=to_float(result.get("soc")),
        battery_power_w=battery_power_w,
        battery_draining=battery_draining,

        ac_output_power_w=to_float(result.get("acpower")),
        pv_total_power_w=sum_optional(pv1, pv2, pv3, pv4),
        pv1_power_w=pv1,
        pv2_power_w=pv2,
        pv3_power_w=pv3,
        pv4_power_w=pv4,

        grid_feed_in_power_w=to_float(result.get("feedinpower")),
        meter2_power_w=to_float(result.get("feedinpowerM2")),

        eps1_power_w=eps1,
        eps2_power_w=eps2,
        eps3_power_w=eps3,
        eps_total_power_w=sum_optional(eps1, eps2, eps3),

        yield_today_kwh=to_float(result.get("yieldtoday")),
        yield_total_kwh=to_float(result.get("yieldtotal")),
        grid_import_energy_kwh=to_float(result.get("consumeenergy")),
        grid_export_energy_kwh=to_float(result.get("feedinenergy")),
    )


def optional_float_env(name: str) -> Optional[float]:
    value = os.getenv(name)
    if value is None or not value.strip():
        return None

    return to_float(value)


def float_env(name: str, default: str) -> float:
    value = optional_float_env(name)
    if value is not None:
        return value

    default_value = to_float(default)
    if default_value is None:
        raise ValueError(f"Invalid default float for {name}: {default}")

    return default_value


def decide_miner_action(
    snapshot: SolaxSnapshot,
    last_target_percent: Optional[float],
    miner_power_w: Optional[float],
    power_limits: MinerPowerLimits,
) -> MinerDecision:
    soc = snapshot.battery_soc_percent
    start_soc = float_env("MINER_START_SOC_PERCENT", "75")
    full_percent = float_env("MINER_FULL_PERCENT", "100")
    legacy_start_percent = os.getenv("MINER_75_PERCENT") or "50"
    start_percent = float_env(
        "MINER_START_PERCENT",
        legacy_start_percent,
    )
    ramp_up_step_percent = max(0.0, float_env("MINER_RAMP_UP_PERCENT_STEP", "10"))
    charge_reserve_w = max(0.0, float_env("MINER_BATTERY_CHARGE_RESERVE_W", "0"))

    if soc is None:
        return MinerDecision(
            action="hold",
            target_percent=None,
            reason="battery_soc_unknown",
        )

    if soc < start_soc:
        return MinerDecision(
            action="pause",
            target_percent=0,
            reason="battery_soc_below_start_threshold_pause_mining",
        )

    if snapshot.battery_power_w is None:
        return MinerDecision(
            action="hold",
            target_percent=None,
            reason="battery_power_unknown",
        )

    if snapshot.battery_power_w <= charge_reserve_w:
        return MinerDecision(
            action="pause",
            target_percent=0,
            reason="battery_not_charging_enough_pause_mining",
        )

    current_power_w = max(0.0, miner_power_w or 0.0)
    available_extra_power_w = max(0.0, snapshot.battery_power_w - charge_reserve_w)
    charge_safe_target_power_w = current_power_w + available_extra_power_w

    if (
        power_limits.min_power_w is not None
        and charge_safe_target_power_w < power_limits.min_power_w
    ):
        return MinerDecision(
            action="pause",
            target_percent=0,
            reason="charging_surplus_below_miner_minimum_pause_mining",
        )

    min_percent = power_limits.minimum_percent()
    if min_percent is not None:
        start_percent = max(start_percent, min_percent)

    if last_target_percent is None or last_target_percent <= 0:
        target_percent = start_percent
    else:
        target_percent = min(full_percent, last_target_percent + ramp_up_step_percent)

    target_percent = min(target_percent, full_percent)

    charge_safe_percent = power_limits.percent_for_power(charge_safe_target_power_w)
    if charge_safe_percent is not None:
        target_percent = min(target_percent, charge_safe_percent)

    if min_percent is not None and target_percent < min_percent:
        return MinerDecision(
            action="pause",
            target_percent=0,
            reason="charge_safe_target_below_miner_minimum_pause_mining",
        )

    target_percent = power_limits.clamp_percent(target_percent)

    return MinerDecision(
        action="set_percent",
        target_percent=target_percent,
        reason="battery_above_threshold_and_charging_ramp_power",
    )


def parse_braiins_power_range_error(error_text: str) -> tuple[Optional[int], Optional[int], Optional[int]]:
    """
    Parses Braiins error like:
      new power target '1750' is out-of-range (min: Some(2414), max: Some(6435))

    Returns:
      attempted_w, min_w, max_w
    """

    attempted_w = None
    min_w = None
    max_w = None

    attempted_match = re.search(r"new power target '(\d+)'", error_text)
    min_match = re.search(r"min:\s*Some\((\d+)\)", error_text)
    max_match = re.search(r"max:\s*Some\((\d+)\)", error_text)

    if attempted_match:
        attempted_w = int(attempted_match.group(1))

    if min_match:
        min_w = int(min_match.group(1))

    if max_match:
        max_w = int(max_match.group(1))

    return attempted_w, min_w, max_w


def extract_nested_number(data: Any, normalized_keys: list[str]) -> Optional[float]:
    if data is None:
        return None

    stack = [data]

    while stack:
        item = stack.pop()

        if isinstance(item, dict):
            for key, value in item.items():
                normalized = key.lower().replace("_", "").replace("-", "")

                if normalized in normalized_keys:
                    number = to_float(value)
                    if number is not None:
                        return number

                if isinstance(value, (dict, list)):
                    stack.append(value)

        elif isinstance(item, list):
            stack.extend(item)

    return None


def extract_nested_string(data: Any, normalized_keys: list[str]) -> Optional[str]:
    if data is None:
        return None

    stack = [data]

    while stack:
        item = stack.pop()

        if isinstance(item, dict):
            for key, value in item.items():
                normalized = key.lower().replace("_", "").replace("-", "")

                if normalized in normalized_keys and value is not None:
                    return str(value)

                if isinstance(value, (dict, list)):
                    stack.append(value)

        elif isinstance(item, list):
            stack.extend(item)

    return None


def normalize_hashrate_to_ths(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None

    # Best effort:
    # - H/s -> TH/s
    # - GH/s -> TH/s
    # - TH/s -> TH/s
    if value > 1_000_000_000_000:
        return round(value / 1_000_000_000_000, 3)

    if value > 1_000_000:
        return round(value / 1_000, 3)

    return round(value, 3)


def summarize_miner_status(stats: Any, mode: Any, details: Any) -> dict[str, Any]:
    hostname = None
    miner_status = None
    miner_status_text = None
    sticker_hashrate_ths = None

    if isinstance(details, dict):
        hostname = (
            details.get("hostname")
            or details.get("uid")
            or details.get("serial_number")
            or details.get("serialNumber")
        )

        miner_status = to_int(details.get("status"))
        if miner_status is not None:
            miner_status_text = MINER_STATUS_MAP.get(miner_status, f"Unknown({miner_status})")

        sticker = details.get("sticker_hashrate")
        if isinstance(sticker, dict):
            ghs = to_float(sticker.get("gigahash_per_second"))
            if ghs is not None:
                sticker_hashrate_ths = round(ghs / 1000, 3)

    if miner_status is None:
        status_string = extract_nested_string(
            details,
            [
                "status",
                "minerstatus",
                "state",
            ],
        )
        if status_string is not None:
            miner_status_text = status_string

    power_w = extract_nested_number(
        stats,
        [
            "power",
            "powerw",
            "watt",
            "watts",
            "powerconsumption",
            "powerconsumptionw",
            "powerusage",
            "powerusagew",
        ],
    )

    raw_hashrate = extract_nested_number(
        stats,
        [
            "terahashpersecond",
            "hashrateths",
            "hashrate",
            "mhsav",
            "ghsav",
            "hs",
        ],
    )

    hashrate_ths = normalize_hashrate_to_ths(raw_hashrate)

    return {
        "miner_hostname": hostname,
        "miner_status": miner_status,
        "miner_status_text": miner_status_text,
        "miner_power_w": power_w,
        "miner_hashrate_ths": hashrate_ths,
        "miner_sticker_hashrate_ths": sticker_hashrate_ths,
        "miner_performance_mode": mode,
    }


def apply_miner_decision(
    logger: logging.Logger,
    miner: BraiinsClient,
    decision: MinerDecision,
    reference: int,
    last_state: Optional[str],
    control_enabled: bool,
    power_limits: MinerPowerLimits,
) -> tuple[Optional[str], bool, Any]:
    target_percent = decision.target_percent
    if decision.action == "set_percent" and target_percent is not None:
        target_percent = power_limits.clamp_percent(target_percent)

    desired_target = 0 if decision.action == "pause" else target_percent
    desired_state = f"{decision.action}:{desired_target}"

    if decision.action == "hold":
        return last_state, False, {
            "held": True,
            "reason": decision.reason,
        }

    if desired_state == last_state:
        return last_state, False, {
            "skipped": True,
            "reason": "already_in_desired_state",
        }

    if not control_enabled:
        return desired_state, False, {
            "skipped": True,
            "reason": "miner_control_disabled",
        }

    if decision.action == "pause":
        result = miner.pause_mining()
        return desired_state, True, result

    if decision.action == "set_percent":
        if target_percent is None:
            return last_state, False, {
                "skipped": True,
                "reason": "target_percent_missing",
            }

        if target_percent <= 0:
            result = miner.pause_mining()
            return "pause:0", True, {
                "paused": True,
                "reason": "target_percent_zero_or_lower",
                "pause_result": result,
            }

        try:
            miner.resume_mining()
        except Exception as exc:
            logger.warning(
                "miner_resume_failed_before_power_set",
                extra={
                    "miner_action": decision.action,
                    "miner_target_percent": target_percent,
                    "resume_error": str(exc),
                },
            )

        try:
            miner.start_mining()
        except Exception as exc:
            logger.warning(
                "miner_start_failed_before_power_set",
                extra={
                    "miner_action": decision.action,
                    "miner_target_percent": target_percent,
                    "start_error": str(exc),
                },
            )

        try:
            result = miner.set_relative_power_target(
                percentage=target_percent,
                reference=reference,
            )

            return desired_state, True, result

        except RuntimeError as exc:
            error_text = str(exc)
            attempted_w, min_w, max_w = parse_braiins_power_range_error(error_text)

            if (
                "out-of-range" in error_text
                and attempted_w is not None
                and (min_w is not None or max_w is not None)
            ):
                power_limits.update_from_braiins_range_error(
                    requested_percent=target_percent,
                    attempted_w=attempted_w,
                    min_w=min_w,
                    max_w=max_w,
                )

                if min_w is not None and attempted_w < min_w:
                    logger.warning(
                        "miner_target_below_minimum_pausing_worker",
                        extra={
                            "miner_action": "pause",
                            "miner_target_percent": target_percent,
                            "miner_attempted_power_w": attempted_w,
                            "miner_min_power_w": min_w,
                            "miner_max_power_w": max_w,
                            "miner_decision_reason": "requested_power_below_braiins_minimum",
                        },
                    )

                    pause_result = miner.pause_mining()

                    return "pause:0", True, {
                        "paused": True,
                        "reason": "requested_power_below_braiins_minimum",
                        "attempted_power_w": attempted_w,
                        "minimum_power_w": min_w,
                        "maximum_power_w": max_w,
                        "original_target_percent": target_percent,
                        "pause_result": pause_result,
                    }

                adjusted_percent = power_limits.clamp_percent(target_percent)
                if max_w is not None and attempted_w > max_w and adjusted_percent < target_percent:
                    logger.warning(
                        "miner_target_above_maximum_clamping",
                        extra={
                            "miner_action": decision.action,
                            "miner_target_percent": adjusted_percent,
                            "miner_original_target_percent": target_percent,
                            "miner_attempted_power_w": attempted_w,
                            "miner_min_power_w": min_w,
                            "miner_max_power_w": max_w,
                            "miner_decision_reason": "requested_power_above_braiins_maximum",
                        },
                    )

                    result = miner.set_relative_power_target(
                        percentage=adjusted_percent,
                        reference=reference,
                    )

                    return f"set_percent:{adjusted_percent}", True, {
                        "clamped": True,
                        "reason": "requested_power_above_braiins_maximum",
                        "original_target_percent": target_percent,
                        "applied_target_percent": adjusted_percent,
                        "attempted_power_w": attempted_w,
                        "minimum_power_w": min_w,
                        "maximum_power_w": max_w,
                        "set_result": result,
                    }

            raise

    return last_state, False, {
        "skipped": True,
        "reason": f"unknown_action_{decision.action}",
    }


def bool_env(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }


def validate_config() -> None:
    missing = []

    if not os.getenv("SOLAX_TOKEN_ID"):
        missing.append("SOLAX_TOKEN_ID")

    if not os.getenv("SOLAX_WIFI_SN"):
        missing.append("SOLAX_WIFI_SN")

    if not os.getenv("BRAIINS_BASE_URL"):
        missing.append("BRAIINS_BASE_URL")

    if not os.getenv("BRAIINS_USERNAME"):
        missing.append("BRAIINS_USERNAME")

    if not os.getenv("BRAIINS_PASSWORD"):
        missing.append("BRAIINS_PASSWORD")

    if missing:
        raise SystemExit(
            f"Missing required environment variable(s): {', '.join(missing)}"
        )


def main() -> int:
    logger = setup_logging()
    validate_config()

    solax_token_id = os.getenv("SOLAX_TOKEN_ID", "")
    solax_wifi_sn = os.getenv("SOLAX_WIFI_SN", "")
    solax_api_url = os.getenv("SOLAX_API_URL", DEFAULT_SOLAX_API_URL)
    poll_interval = float(os.getenv("SOLAX_POLL_INTERVAL", "300"))
    solax_timeout = float(os.getenv("SOLAX_TIMEOUT", "600"))

    braiins_base_url = os.getenv("BRAIINS_BASE_URL", "").rstrip("/")
    braiins_username = os.getenv("BRAIINS_USERNAME", "")
    braiins_password = os.getenv("BRAIINS_PASSWORD", "")
    braiins_timeout = float(os.getenv("BRAIINS_TIMEOUT", "10"))

    miner_control_enabled = bool_env("MINER_ENABLE_CONTROL", "true")
    miner_dry_run = bool_env("MINER_DRY_RUN", "false")
    miner_reference = int(os.getenv("MINER_REFERENCE", "1"))
    miner_power_limits = MinerPowerLimits(
        min_power_w=optional_float_env("MINER_MIN_POWER_W"),
        max_power_w=optional_float_env("MINER_MAX_POWER_W"),
        rated_power_w=(
            optional_float_env("MINER_RATED_POWER_W")
            or optional_float_env("MINER_NOMINAL_POWER_W")
        ),
    )

    solax = SolaxClient(
        api_url=solax_api_url,
        token_id=solax_token_id,
        wifi_sn=solax_wifi_sn,
        timeout=solax_timeout,
    )

    miner = BraiinsClient(
        base_url=braiins_base_url,
        timeout=braiins_timeout,
        username=braiins_username,
        password=braiins_password,
        dry_run=miner_dry_run,
    )

    stop = False

    def handle_signal(signum: int, _frame: Any) -> None:
        nonlocal stop
        stop = True
        logger.info("shutdown_requested", extra={"signal": signum})

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    logger.info(
        "controller_started",
        extra={
            "solax_api_url": solax_api_url,
            "poll_interval_seconds": poll_interval,
            "solax_timeout_seconds": solax_timeout,
            "miner_host": braiins_base_url,
            "miner_control_enabled": miner_control_enabled,
            "miner_dry_run": miner_dry_run,
            "miner_reference": miner_reference,
            "miner_full_percent": float_env("MINER_FULL_PERCENT", "100"),
            "miner_start_soc_percent": float_env("MINER_START_SOC_PERCENT", "75"),
            "miner_start_percent": float_env(
                "MINER_START_PERCENT",
                os.getenv("MINER_75_PERCENT") or "50",
            ),
            "miner_ramp_up_percent_step": float_env("MINER_RAMP_UP_PERCENT_STEP", "10"),
            "miner_battery_charge_reserve_w": float_env(
                "MINER_BATTERY_CHARGE_RESERVE_W",
                "0",
            ),
            "miner_min_power_w": miner_power_limits.min_power_w,
            "miner_max_power_w": miner_power_limits.max_power_w,
            "miner_rated_power_w": miner_power_limits.rated_power_w,
            "log_file": os.getenv("SOLAX_LOG_FILE", "./solax-miner-controller.jsonl"),
            "wifi_sn_suffix": solax_wifi_sn[-4:] if solax_wifi_sn else None,
        },
    )

    try:
        miner.login()
        logger.info(
            "braiins_login_success",
            extra={
                "miner_host": braiins_base_url,
                "miner_dry_run": miner_dry_run,
            },
        )
    except Exception:
        logger.error(
            "braiins_login_failed",
            exc_info=True,
            extra={
                "miner_host": braiins_base_url,
            },
        )
        return 1

    last_upload_time: Optional[str] = None
    last_miner_state: Optional[str] = None
    last_miner_target_percent: Optional[float] = None

    while not stop:
        started = time.monotonic()

        try:
            solax_result = solax.fetch_realtime_info()
            snapshot = parse_snapshot(solax_result)
            snapshot_dict = asdict(snapshot)

            same_upload_time = (
                last_upload_time is not None
                and snapshot.upload_time is not None
                and snapshot.upload_time == last_upload_time
            )

            last_upload_time = snapshot.upload_time or last_upload_time

            miner_details = None
            miner_stats = None
            miner_mode = None
            miner_status_summary: dict[str, Any] = {}

            try:
                miner_details = miner.get_miner_details()
                miner_stats = miner.get_miner_stats()
                miner_mode = miner.get_performance_mode()

                miner_status_summary = summarize_miner_status(
                    stats=miner_stats,
                    mode=miner_mode,
                    details=miner_details,
                )

            except Exception:
                logger.warning(
                    "miner_status_fetch_failed",
                    exc_info=True,
                    extra={
                        **snapshot_dict,
                        "miner_host": braiins_base_url,
                    },
                )

            decision = decide_miner_action(
                snapshot=snapshot,
                last_target_percent=last_miner_target_percent,
                miner_power_w=miner_status_summary.get("miner_power_w"),
                power_limits=miner_power_limits,
            )

            last_miner_state, miner_applied, miner_apply_result = apply_miner_decision(
                logger=logger,
                miner=miner,
                decision=decision,
                reference=miner_reference,
                last_state=last_miner_state,
                control_enabled=miner_control_enabled,
                power_limits=miner_power_limits,
            )

            if last_miner_state and last_miner_state.startswith("set_percent:"):
                last_miner_target_percent = to_float(last_miner_state.split(":", 1)[1])
            elif last_miner_state and last_miner_state.startswith("pause:"):
                last_miner_target_percent = None

            log_level = logging.INFO
            event = "solax_miner_control_tick"

            if snapshot.battery_status_code in {"1", "2"}:
                log_level = logging.WARNING
                event = "solax_battery_not_normal"

            if snapshot.inverter_status_code in {"103", "104"}:
                log_level = logging.WARNING
                event = "solax_inverter_fault"

            if decision.action == "pause":
                log_level = logging.WARNING
                event = "miner_pause_requested_due_to_low_battery_or_low_charge"

            logger.log(
                log_level,
                event,
                extra={
                    **snapshot_dict,
                    "same_upload_time_as_previous_poll": same_upload_time,
                    "miner_host": braiins_base_url,
                    "miner_action": decision.action,
                    "miner_target_percent": decision.target_percent,
                    "miner_decision_reason": decision.reason,
                    "miner_last_state": last_miner_state,
                    "miner_applied": miner_applied,
                    "miner_apply_result": miner_apply_result,
                    "miner_control_enabled": miner_control_enabled,
                    "miner_dry_run": miner_dry_run,
                    "miner_min_power_w": miner_power_limits.min_power_w,
                    "miner_max_power_w": miner_power_limits.max_power_w,
                    "miner_rated_power_w": miner_power_limits.rated_power_w,
                    **miner_status_summary,
                },
            )

        except requests.Timeout:
            logger.warning("request_timeout", exc_info=True)

        except requests.HTTPError:
            logger.warning("http_error", exc_info=True)

        except requests.RequestException:
            logger.warning("request_failed", exc_info=True)

        except Exception:
            logger.error("controller_tick_failed", exc_info=True)

        elapsed = time.monotonic() - started
        sleep_for = max(0.0, poll_interval - elapsed)

        if sleep_for > 0:
            time.sleep(sleep_for)

    logger.info("controller_stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
