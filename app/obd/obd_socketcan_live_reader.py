#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Raspberry Pi + Kvaser OBD-II 即時讀取器（29-bit/11-bit 自動適應版）。

本版針對實車已確認的 Honda 29-bit OBD 通訊修正：
- 29-bit functional request: 0x18DB33F1
- 29-bit ECU response:      0x18DAF1xx（實車已看到 18DAF110 / 18DAF111）
- 500 kbit/s
- 保留 11-bit 7DF -> 7E8~7EF fallback，方便原本模擬器/其他車使用
- 固定只把既有 10 個 Mode 01 PID 成功回覆的資料送給網站 / AI
- 保留 Mode 03 / 07 / 0A 標準 DTC 讀取
- 修正 ISO 15765-4 CAN DTC 回覆的 count byte 解析，避免 P0354 被錯解成 P0103
- 支援 ISO-TP 單框與多框 DTC 回覆
- API 維持不變：
    GET http://127.0.0.1:8768/health
    GET http://127.0.0.1:8768/obd
    GET http://127.0.0.1:8768/obd/dtc
    GET http://127.0.0.1:8768/obd/window
    GET http://127.0.0.1:8768/case/status
    GET http://127.0.0.1:8768/case/list
    POST /case/record/start|stop /case/replay/start|stop|restart /case/delete

安全性：
- 本程式只執行讀取型 OBD 服務（Mode 01 / 03 / 07 / 0A）
- 不送 Mode 04 清碼，不執行 ECU 寫入/致動控制
"""

from __future__ import annotations

import argparse
import bisect
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

try:
    import can
except ImportError as exc:
    can = None  # type: ignore[assignment]
    CAN_IMPORT_ERROR: Optional[BaseException] = exc
else:
    CAN_IMPORT_ERROR = None


# ============================================================
# 基本設定
# ============================================================
DEFAULT_INTERFACE = "can0"
DEFAULT_BITRATE = 500_000
DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8768
DEFAULT_CYCLE_INTERVAL = 0.5
DEFAULT_RECONNECT_DELAY = 2.0
DEFAULT_WINDOW_DURATION = 10.0
WINDOW_POLL_INTERVAL = 0.05
RESPONSE_TIMEOUT_SECONDS = 0.40
ECU_DISCOVERY_TIMEOUT_SECONDS = 0.70
ISOTP_RESPONSE_TIMEOUT_SECONDS = 0.80
DTC_SCAN_INTERVAL_SECONDS = 5.0
OUTPUT_JSON = Path(__file__).with_name("obd_live_data.json")
CASES_DIR = Path(os.environ.get(
    "VE_OBD_CASES_DIR",
    str(Path(__file__).resolve().parent.parent / "obd_cases"),
)).resolve()
DEFAULT_RECORD_SECONDS = 30.0
CASE_RECORD_POLL_INTERVAL = 0.05

# 29-bit ISO 15765-4 Normal Fixed Addressing
OBD29_FUNCTIONAL_REQUEST_ID = 0x18DB33F1
OBD29_RESPONSE_PREFIX = 0x18DAF100
OBD29_RESPONSE_MASK = 0x1FFFFF00
OBD29_TESTER_ADDRESS = 0xF1

# 11-bit fallback
OBD11_FUNCTIONAL_REQUEST_ID = 0x7DF
OBD11_RESPONSE_ID_MIN = 0x7E8
OBD11_RESPONSE_ID_MAX = 0x7EF


@dataclass(frozen=True)
class ProtocolSpec:
    key: str
    label: str
    functional_request_id: int
    is_extended_id: bool


PROTOCOL_29 = ProtocolSpec(
    key="can29_500k",
    label="ISO 15765-4 CAN 29-bit / 500 kbit/s",
    functional_request_id=OBD29_FUNCTIONAL_REQUEST_ID,
    is_extended_id=True,
)
PROTOCOL_11 = ProtocolSpec(
    key="can11_500k",
    label="ISO 15765-4 CAN 11-bit / 500 kbit/s",
    functional_request_id=OBD11_FUNCTIONAL_REQUEST_ID,
    is_extended_id=False,
)
PROTOCOL_CANDIDATES = (PROTOCOL_29, PROTOCOL_11)


@dataclass(frozen=True)
class PidDefinition:
    pid: int
    key: str
    label: str
    unit: str
    payload_length: int
    decoder: Callable[[bytes], Any]


@dataclass(frozen=True)
class DtcModeDefinition:
    service: int
    key: str
    label: str


def u16(payload: bytes) -> int:
    return (payload[0] << 8) | payload[1]


def percent_a(payload: bytes) -> float:
    return payload[0] * 100 / 255


def temperature_a(payload: bytes) -> float:
    return float(payload[0] - 40)


# ============================================================
# 既有固定 10 個 PID（網站 / AI 邏輯保持一致）
# ============================================================
PID_DEFINITIONS = (
    PidDefinition(0x04, "calculated_engine_load", "計算負荷", "%", 1, percent_a),
    PidDefinition(0x05, "coolant_temperature", "冷卻液溫度", "°C", 1, temperature_a),
    PidDefinition(0x0B, "intake_manifold_pressure", "進氣歧管絕對壓力 MAP", "kPa", 1, lambda p: float(p[0])),
    PidDefinition(0x0C, "engine_rpm", "引擎轉速", "rpm", 2, lambda p: u16(p) / 4),
    PidDefinition(0x0D, "vehicle_speed", "車速", "km/h", 1, lambda p: float(p[0])),
    PidDefinition(0x0E, "ignition_timing_advance", "點火提前角", "°", 1, lambda p: (p[0] / 2) - 64),
    PidDefinition(0x0F, "intake_air_temperature", "進氣溫度", "°C", 1, temperature_a),
    PidDefinition(0x10, "mass_air_flow", "空氣流量 MAF", "g/s", 2, lambda p: u16(p) / 100),
    PidDefinition(0x11, "throttle_position", "節氣門位置", "%", 1, percent_a),
    PidDefinition(0x2F, "fuel_level", "燃油液位", "%", 1, percent_a),
)
PID_BY_ID = {item.pid: item for item in PID_DEFINITIONS}
FIXED_PID_IDS = tuple(item.pid for item in PID_DEFINITIONS)

DTC_MODE_DEFINITIONS = (
    DtcModeDefinition(0x03, "stored", "已儲存故障碼"),
    DtcModeDefinition(0x07, "pending", "待確認故障碼"),
    DtcModeDefinition(0x0A, "permanent", "永久故障碼"),
)

DTC_SYSTEM_LABELS = {
    "P": "動力系統",
    "C": "底盤系統",
    "B": "車身系統",
    "U": "網路通訊",
}


STATE_LOCK = threading.Lock()
LIVE_STATE: dict[str, Any] = {
    "status": "starting",
    "timestamp": None,
    "connection": {},
    "values": {},
    "dtc_codes": [],
}

DTC_CACHE_LOCK = threading.Lock()
DTC_CACHE: dict[str, Any] = {
    "scanned_monotonic": 0.0,
    "signature": None,
    "result": None,
}

PROTOCOL_CACHE_LOCK = threading.Lock()
PROTOCOL_CACHE: dict[str, Any] = {
    "protocol": None,
    "ecu_ids": [],
    "last_success": 0.0,
}

BUS_LOCK = threading.RLock()


def now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")



# ============================================================
# 實車案例錄製 / REPLAY 管理
# ============================================================
CASE_MODE_LOCK = threading.RLock()
ACTIVE_REPLAY: Optional["ReplaySession"] = None

RECORD_LOCK = threading.RLock()
RECORD_STATE: dict[str, Any] = {
    "active": False,
    "status": "idle",
    "case_folder": "",
    "case_name": "",
    "started_at": None,
    "duration_seconds": 0.0,
    "elapsed_seconds": 0.0,
    "sample_count": 0,
    "pid_count": 0,
    "dtc_codes": [],
    "raw_can": False,
    "message": "等待錄製",
}
RECORD_STOP_EVENT = threading.Event()
RECORD_THREAD: Optional[threading.Thread] = None


def safe_case_text(value: str, fallback: str = "case") -> str:
    value = str(value or "").strip()
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", value)
    value = re.sub(r"\s+", "_", value).strip("._-")
    return value[:80] or fallback


def list_case_dirs() -> list[Path]:
    CASES_DIR.mkdir(parents=True, exist_ok=True)
    return sorted(
        [
            path for path in CASES_DIR.iterdir()
            if path.is_dir() and (path / "obd_frames.jsonl").is_file()
        ],
        key=lambda path: path.name,
        reverse=True,
    )


def read_case_meta(case_dir: Path) -> dict[str, Any]:
    meta_path = case_dir / "meta.json"
    if not meta_path.is_file():
        return {}
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def case_summary(case_dir: Path) -> dict[str, Any]:
    meta = read_case_meta(case_dir)
    dtcs = meta.get("unique_dtc_codes")
    if not isinstance(dtcs, list):
        dtcs = meta.get("dtc_end") if isinstance(meta.get("dtc_end"), list) else []
    return {
        "folder": case_dir.name,
        "case_name": str(meta.get("case_name") or case_dir.name),
        "vehicle": str(meta.get("vehicle") or ""),
        "odometer_km": safe_int(meta.get("odometer_km")),
        "symptom": str(meta.get("symptom") or ""),
        "condition": str(meta.get("condition") or ""),
        "note": str(meta.get("note") or ""),
        "duration_seconds": safe_float(
            meta.get("actual_duration_seconds")
            or meta.get("requested_duration_seconds")
        ),
        "sample_count": safe_int(meta.get("sample_count")),
        "pid_count": safe_int(meta.get("pid_count")),
        "dtc_codes": [str(code) for code in dtcs],
        "raw_can_recorded": bool(meta.get("raw_can_recorded")),
        "started_at": meta.get("started_at"),
        "source": str(meta.get("source") or "recorded_real_vehicle"),
    }


def resolve_case_folder(folder: str) -> Path:
    folder_name = os.path.basename(str(folder or "").strip())
    if not folder_name:
        raise ValueError("缺少案例名稱")
    path = (CASES_DIR / folder_name).resolve()
    try:
        path.relative_to(CASES_DIR)
    except ValueError as exc:
        raise ValueError("案例路徑不合法") from exc
    if not path.is_dir() or not (path / "obd_frames.jsonl").is_file():
        raise FileNotFoundError(f"找不到案例：{folder_name}")
    return path


class ReplaySession:
    def __init__(self, case_dir: Path):
        self.case_dir = case_dir
        self.meta = read_case_meta(case_dir)
        self.records: list[dict[str, Any]] = []
        frames_path = case_dir / "obd_frames.jsonl"

        with frames_path.open("r", encoding="utf-8") as file:
            for line_number, line in enumerate(file, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        f"{frames_path.name} 第 {line_number} 行 JSON 錯誤"
                    ) from exc
                if not isinstance(item, dict) or not isinstance(item.get("obd"), dict):
                    continue
                try:
                    elapsed = float(item.get("elapsed_seconds") or 0.0)
                except (TypeError, ValueError):
                    elapsed = 0.0
                item["elapsed_seconds"] = max(0.0, elapsed)
                self.records.append(item)

        self.records.sort(key=lambda item: item["elapsed_seconds"])
        if not self.records:
            raise RuntimeError("案例沒有可回放的 OBD 時序資料")

        self.elapsed_points = [float(item["elapsed_seconds"]) for item in self.records]
        self.duration = max(
            0.001,
            self.elapsed_points[-1],
            float(
                self.meta.get("actual_duration_seconds")
                or self.meta.get("requested_duration_seconds")
                or 0.001
            ),
        )
        self.started_monotonic = time.monotonic()
        self.lock = threading.RLock()

    def restart(self) -> None:
        with self.lock:
            self.started_monotonic = time.monotonic()

    def elapsed(self) -> float:
        with self.lock:
            value = time.monotonic() - self.started_monotonic
        return value % self.duration if self.duration > 0 else 0.0

    def current_state(self) -> dict[str, Any]:
        elapsed = self.elapsed()
        index = bisect.bisect_right(self.elapsed_points, elapsed) - 1
        index = max(0, min(index, len(self.records) - 1))
        record = self.records[index]
        state = copy.deepcopy(record["obd"])

        recorded_timestamp = state.get("timestamp")
        state["timestamp"] = now_text()
        state["capture_source"] = "recorded_real_vehicle_replay"
        state["simulated"] = False
        state["replay"] = True
        state["data_mode"] = "replay"
        state["replay_info"] = {
            "folder": self.case_dir.name,
            "case_name": str(self.meta.get("case_name") or self.case_dir.name),
            "vehicle": str(self.meta.get("vehicle") or ""),
            "odometer_km": int(self.meta.get("odometer_km") or 0),
            "symptom": str(self.meta.get("symptom") or ""),
            "condition": str(self.meta.get("condition") or ""),
            "recorded_timestamp": recorded_timestamp,
            "recorded_elapsed_seconds": float(record["elapsed_seconds"]),
            "replay_elapsed_seconds": round(elapsed, 3),
            "duration_seconds": round(self.duration, 3),
        }
        return state


def get_replay_session() -> Optional[ReplaySession]:
    with CASE_MODE_LOCK:
        return ACTIVE_REPLAY


def get_output_state() -> dict[str, Any]:
    session = get_replay_session()
    if session is not None:
        return session.current_state()
    state = get_live_state()
    state["data_mode"] = "live"
    state["replay"] = False
    return state


def get_case_mode_status() -> dict[str, Any]:
    session = get_replay_session()
    with RECORD_LOCK:
        record = copy.deepcopy(RECORD_STATE)

    if session is not None:
        mode = "replay"
        active_case = case_summary(session.case_dir)
        replay_elapsed = round(session.elapsed(), 3)
    else:
        mode = "live"
        active_case = None
        replay_elapsed = None

    return {
        "mode": mode,
        "label": "REPLAY 案例回放" if mode == "replay" else "LIVE 實車",
        "active_case": active_case,
        "replay_elapsed_seconds": replay_elapsed,
        "recording": record,
        "cases_dir": str(CASES_DIR),
    }


def count_raw_can_lines(path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as file:
            return sum(1 for _ in file)
    except Exception:
        return 0


def start_candump(interface: str, path: Path) -> tuple[Optional[subprocess.Popen], Optional[Any], str]:
    candump = shutil.which("candump")
    if not candump:
        return None, None, "找不到 candump，僅保存解析後 OBD 資料"

    file_handle = path.open("w", encoding="utf-8", buffering=1)
    try:
        process = subprocess.Popen(
            [candump, "-L", interface],
            stdout=file_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except Exception:
        file_handle.close()
        raise

    time.sleep(0.1)
    if process.poll() is not None:
        file_handle.close()
        return None, None, "candump 啟動失敗，僅保存解析後 OBD 資料"
    return process, file_handle, "RAW CAN 錄製中"


def stop_candump(process: Optional[subprocess.Popen], file_handle: Optional[Any]) -> None:
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
    if file_handle is not None:
        try:
            file_handle.flush()
            file_handle.close()
        except Exception:
            pass


def update_record_state(**kwargs: Any) -> None:
    with RECORD_LOCK:
        RECORD_STATE.update(kwargs)


def recording_worker(
    case_dir: Path,
    payload: dict[str, Any],
    duration: float,
    interface: str,
) -> None:
    frames_path = case_dir / "obd_frames.jsonl"
    raw_can_path = case_dir / "raw_can.log"
    meta_path = case_dir / "meta.json"
    latest_path = case_dir / "latest_obd.json"
    dtc_start_path = case_dir / "dtc_start.json"
    dtc_end_path = case_dir / "dtc_end.json"
    notes_path = case_dir / "notes.txt"

    started_wall = time.time()
    started_monotonic = time.monotonic()
    start_state = get_live_state()
    start_dtc = {
        "status": start_state.get("dtc_status"),
        "timestamp": start_state.get("timestamp"),
        "dtc_codes": copy.deepcopy(start_state.get("dtc_codes") or []),
        "dtc_codes_detailed": copy.deepcopy(start_state.get("dtc_codes_detailed") or []),
        "dtc_scan": copy.deepcopy(start_state.get("dtc_scan") or {}),
    }
    dtc_start_path.write_text(
        json.dumps(start_dtc, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    candump_process = None
    candump_file = None
    raw_message = ""
    try:
        candump_process, candump_file, raw_message = start_candump(interface, raw_can_path)
    except Exception as exc:
        raw_message = f"RAW CAN 啟動失敗：{exc}"

    update_record_state(message=raw_message or "正在錄製")

    samples: list[dict[str, Any]] = []
    unique_dtcs: list[str] = []
    pid_keys: set[str] = set()
    last_timestamp: Optional[str] = None

    try:
        with frames_path.open("w", encoding="utf-8", buffering=1) as frames_file:
            while not RECORD_STOP_EVENT.is_set():
                elapsed = time.monotonic() - started_monotonic
                if elapsed >= duration:
                    break

                state = get_live_state()
                timestamp = state.get("timestamp")
                if (
                    isinstance(timestamp, str)
                    and timestamp
                    and timestamp != last_timestamp
                    and state.get("status") == "ok"
                ):
                    item = {
                        "recorded_at": now_text(),
                        "elapsed_seconds": round(elapsed, 3),
                        "obd": copy.deepcopy(state),
                    }
                    frames_file.write(
                        json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
                    )
                    samples.append(item)
                    last_timestamp = timestamp

                    values = state.get("values")
                    if isinstance(values, dict):
                        pid_keys.update(values.keys())
                    for code in state.get("dtc_codes") or []:
                        code = str(code)
                        if code and code not in unique_dtcs:
                            unique_dtcs.append(code)

                update_record_state(
                    elapsed_seconds=round(min(elapsed, duration), 1),
                    sample_count=len(samples),
                    pid_count=len(pid_keys),
                    dtc_codes=list(unique_dtcs),
                )
                time.sleep(CASE_RECORD_POLL_INTERVAL)
    finally:
        stop_candump(candump_process, candump_file)

    ended_wall = time.time()
    end_state = get_live_state()
    end_dtc = {
        "status": end_state.get("dtc_status"),
        "timestamp": end_state.get("timestamp"),
        "dtc_codes": copy.deepcopy(end_state.get("dtc_codes") or []),
        "dtc_codes_detailed": copy.deepcopy(end_state.get("dtc_codes_detailed") or []),
        "dtc_scan": copy.deepcopy(end_state.get("dtc_scan") or {}),
    }
    dtc_end_path.write_text(
        json.dumps(end_dtc, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if samples:
        latest_path.write_text(
            json.dumps(samples[-1]["obd"], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    for code in end_dtc.get("dtc_codes") or []:
        code = str(code)
        if code and code not in unique_dtcs:
            unique_dtcs.append(code)

    raw_can_recorded = raw_can_path.is_file() and raw_can_path.stat().st_size > 0
    raw_can_frame_count = count_raw_can_lines(raw_can_path)

    meta = {
        "schema_version": "ve_obd_case_v1",
        "case_name": str(payload.get("case_name") or case_dir.name),
        "case_folder": case_dir.name,
        "vehicle": str(payload.get("vehicle") or ""),
        "odometer_km": int(payload.get("odometer_km") or 0),
        "symptom": str(payload.get("symptom") or ""),
        "condition": str(payload.get("condition") or ""),
        "note": str(payload.get("note") or ""),
        "can_interface": interface,
        "source": "recorded_real_vehicle",
        "started_at": datetime.fromtimestamp(started_wall).astimezone().isoformat(timespec="seconds"),
        "ended_at": datetime.fromtimestamp(ended_wall).astimezone().isoformat(timespec="seconds"),
        "requested_duration_seconds": duration,
        "actual_duration_seconds": round(ended_wall - started_wall, 3),
        "sample_count": len(samples),
        "pid_keys": sorted(pid_keys),
        "pid_count": len(pid_keys),
        "unique_dtc_codes": unique_dtcs,
        "dtc_start": [str(code) for code in start_dtc.get("dtc_codes") or []],
        "dtc_end": [str(code) for code in end_dtc.get("dtc_codes") or []],
        "raw_can_recorded": raw_can_recorded,
        "raw_can_frame_count": raw_can_frame_count,
        "files": {
            "meta": "meta.json",
            "obd_frames": "obd_frames.jsonl",
            "latest_obd": "latest_obd.json" if samples else None,
            "dtc_start": "dtc_start.json",
            "dtc_end": "dtc_end.json",
            "raw_can": "raw_can.log" if raw_can_path.exists() else None,
            "notes": "notes.txt",
        },
    }
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    notes_path.write_text(
        "\n".join([
            f"案例名稱：{meta['case_name']}",
            f"車型：{meta['vehicle']}",
            f"里程：{meta['odometer_km']} km",
            f"症狀：{meta['symptom']}",
            f"錄製條件：{meta['condition']}",
            f"備註：{meta['note']}",
            f"DTC：{', '.join(meta['unique_dtc_codes']) if meta['unique_dtc_codes'] else '無'}",
            f"PID：{', '.join(meta['pid_keys']) if meta['pid_keys'] else '無'}",
            f"RAW CAN frames：{raw_can_frame_count}",
        ]) + "\n",
        encoding="utf-8",
    )

    final_status = "complete" if samples else "complete_no_samples"
    final_message = (
        "錄製完成"
        if samples
        else "錄製結束，但期間沒有取得新的完整 OBD 資料"
    )
    update_record_state(
        active=False,
        status=final_status,
        elapsed_seconds=round(min(time.monotonic() - started_monotonic, duration), 1),
        sample_count=len(samples),
        pid_count=len(pid_keys),
        dtc_codes=list(unique_dtcs),
        raw_can=raw_can_recorded,
        raw_can_frame_count=raw_can_frame_count,
        message=final_message,
    )


def start_case_recording(payload: dict[str, Any], interface: str) -> dict[str, Any]:
    global RECORD_THREAD

    with CASE_MODE_LOCK:
        if ACTIVE_REPLAY is not None:
            raise RuntimeError("目前是 REPLAY 模式，請先回到 LIVE 實車模式")

    with RECORD_LOCK:
        if RECORD_STATE.get("active"):
            raise RuntimeError("目前已經有案例正在錄製")

    live = get_live_state()
    if live.get("status") != "ok":
        raise RuntimeError("目前沒有正常的 LIVE 實車 OBD 資料，無法開始錄製")

    case_name = str(payload.get("case_name") or "實車案例").strip()
    vehicle = str(payload.get("vehicle") or "").strip()
    symptom = str(payload.get("symptom") or "").strip()
    condition = str(payload.get("condition") or "暖車怠速").strip()
    note = str(payload.get("note") or "").strip()

    try:
        odometer_km = max(0, int(float(payload.get("odometer_km") or 0)))
    except (TypeError, ValueError):
        odometer_km = 0
    try:
        duration = float(payload.get("duration_seconds") or DEFAULT_RECORD_SECONDS)
    except (TypeError, ValueError):
        duration = DEFAULT_RECORD_SECONDS
    duration = min(600.0, max(3.0, duration))

    CASES_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    folder_base = f"{timestamp}_{safe_case_text(case_name)}"
    case_dir = CASES_DIR / folder_base
    suffix = 2
    while case_dir.exists():
        case_dir = CASES_DIR / f"{folder_base}_{suffix}"
        suffix += 1
    case_dir.mkdir(parents=True)

    clean_payload = {
        "case_name": case_name,
        "vehicle": vehicle,
        "odometer_km": odometer_km,
        "symptom": symptom,
        "condition": condition,
        "note": note,
    }

    RECORD_STOP_EVENT.clear()
    update_record_state(
        active=True,
        status="recording",
        case_folder=case_dir.name,
        case_name=case_name,
        started_at=now_text(),
        duration_seconds=duration,
        elapsed_seconds=0.0,
        sample_count=0,
        pid_count=0,
        dtc_codes=[],
        raw_can=False,
        raw_can_frame_count=0,
        message="正在啟動錄製",
    )

    RECORD_THREAD = threading.Thread(
        target=recording_worker,
        args=(case_dir, clean_payload, duration, interface),
        daemon=True,
        name="ve-obd-case-recorder",
    )
    RECORD_THREAD.start()
    return get_case_mode_status()


def stop_case_recording() -> dict[str, Any]:
    with RECORD_LOCK:
        active = bool(RECORD_STATE.get("active"))
    if active:
        RECORD_STOP_EVENT.set()
    return get_case_mode_status()


def start_replay(folder: str) -> dict[str, Any]:
    global ACTIVE_REPLAY

    with RECORD_LOCK:
        if RECORD_STATE.get("active"):
            raise RuntimeError("實車案例正在錄製，請先停止錄製")

    case_dir = resolve_case_folder(folder)
    session = ReplaySession(case_dir)
    with CASE_MODE_LOCK:
        ACTIVE_REPLAY = session
    return get_case_mode_status()


def stop_replay() -> dict[str, Any]:
    global ACTIVE_REPLAY
    with CASE_MODE_LOCK:
        ACTIVE_REPLAY = None
    return get_case_mode_status()


def restart_replay() -> dict[str, Any]:
    session = get_replay_session()
    if session is None:
        raise RuntimeError("目前不是 REPLAY 模式")
    session.restart()
    return get_case_mode_status()


def delete_case(folder: str) -> dict[str, Any]:
    with RECORD_LOCK:
        if RECORD_STATE.get("active") and RECORD_STATE.get("case_folder") == folder:
            raise RuntimeError("此案例正在錄製，無法刪除")

    session = get_replay_session()
    if session is not None and session.case_dir.name == folder:
        raise RuntimeError("此案例目前正在回放，請先回到 LIVE 實車模式")

    case_dir = resolve_case_folder(folder)
    shutil.rmtree(case_dir)
    return {
        "success": True,
        "message": f"已刪除案例：{folder}",
        "cases": [case_summary(path) for path in list_case_dirs()],
    }


def set_live_state(data: dict[str, Any]) -> None:
    with STATE_LOCK:
        LIVE_STATE.clear()
        LIVE_STATE.update(copy.deepcopy(data))


def get_live_state() -> dict[str, Any]:
    with STATE_LOCK:
        return copy.deepcopy(LIVE_STATE)


def fmt_can_id(can_id: int) -> str:
    return f"{can_id:08X}" if can_id > 0x7FF else f"{can_id:03X}"


def is_response_id(protocol: ProtocolSpec, frame_id: int, is_extended: bool) -> bool:
    if protocol.is_extended_id:
        return is_extended and (frame_id & OBD29_RESPONSE_MASK) == OBD29_RESPONSE_PREFIX
    return (not is_extended) and OBD11_RESPONSE_ID_MIN <= frame_id <= OBD11_RESPONSE_ID_MAX


def physical_request_id(protocol: ProtocolSpec, response_id: int) -> int:
    """由 ECU 回覆 ID 算出該 ECU 的 physical request ID。"""
    if protocol.is_extended_id:
        ecu_source_address = response_id & 0xFF
        # response 18DAF110 -> request 18DA10F1
        return 0x18DA0000 | (ecu_source_address << 8) | OBD29_TESTER_ADDRESS
    return response_id - 8


def ecu_label(protocol: ProtocolSpec, response_id: int) -> str:
    if protocol.is_extended_id:
        return f"通用 OBD ECU 0x{response_id & 0xFF:02X}"
    return f"通用 OBD ECU {response_id - OBD11_RESPONSE_ID_MIN + 1}"


def drain_receive_queue(bus: Any) -> None:
    while bus.recv(timeout=0.0) is not None:
        pass


def send_can(bus: Any, protocol: ProtocolSpec, arbitration_id: int, data: bytes) -> None:
    if can is None:
        raise RuntimeError("python-can 尚未載入")
    bus.send(
        can.Message(
            arbitration_id=arbitration_id,
            data=data,
            is_extended_id=protocol.is_extended_id,
        ),
        timeout=0.2,
    )


def wait_for_exact_can_id(
    bus: Any,
    protocol: ProtocolSpec,
    response_id: int,
    timeout: float,
) -> Optional[bytes]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = bus.recv(timeout=max(0.001, deadline - time.monotonic()))
        if message is None:
            return None
        if bool(message.is_extended_id) != protocol.is_extended_id:
            continue
        if int(message.arbitration_id) != response_id:
            continue
        return bytes(message.data)
    return None


def discover_ecus_for_protocol(bus: Any, protocol: ProtocolSpec) -> list[int]:
    """用 Mode 01 PID 00 找出會回應的 ECU。"""
    request = bytes([0x02, 0x01, 0x00, 0, 0, 0, 0, 0])
    drain_receive_queue(bus)
    send_can(bus, protocol, protocol.functional_request_id, request)

    deadline = time.monotonic() + ECU_DISCOVERY_TIMEOUT_SECONDS
    response_ids: set[int] = set()

    while time.monotonic() < deadline:
        message = bus.recv(timeout=max(0.001, deadline - time.monotonic()))
        if message is None:
            break
        frame_id = int(message.arbitration_id)
        data = bytes(message.data)
        if not is_response_id(protocol, frame_id, bool(message.is_extended_id)):
            continue
        # 單框：06 41 00 xx xx xx xx padding
        if len(data) >= 3 and data[1] == 0x41 and data[2] == 0x00:
            response_ids.add(frame_id)

    return sorted(response_ids)


def detect_protocol_and_ecus(bus: Any) -> tuple[Optional[ProtocolSpec], list[int]]:
    """優先偵測實車已確認的 29-bit，失敗再退回原本 11-bit。"""
    for protocol in PROTOCOL_CANDIDATES:
        ecu_ids = discover_ecus_for_protocol(bus, protocol)
        if ecu_ids:
            with PROTOCOL_CACHE_LOCK:
                PROTOCOL_CACHE["protocol"] = protocol
                PROTOCOL_CACHE["ecu_ids"] = list(ecu_ids)
                PROTOCOL_CACHE["last_success"] = time.monotonic()
            return protocol, ecu_ids
    return None, []


def get_protocol_and_ecus(bus: Any) -> tuple[Optional[ProtocolSpec], list[int]]:
    # 每輪重新確認，可在模擬器 / 實車切換後自動恢復。
    return detect_protocol_and_ecus(bus)


def send_isotp_request(
    bus: Any,
    protocol: ProtocolSpec,
    request_id: int,
    response_id: int,
    payload: bytes,
    flow_control_id: Optional[int] = None,
) -> dict[str, Any]:
    """送出 1~7 bytes ISO-TP 請求，接收單框或多框回覆。"""
    if not 1 <= len(payload) <= 7:
        raise ValueError("唯讀 OBD 查詢只接受 1～7 bytes ISO-TP payload")

    request_frame = (bytes([len(payload)]) + payload).ljust(8, b"\x00")
    drain_receive_queue(bus)
    send_can(bus, protocol, request_id, request_frame)

    first = wait_for_exact_can_id(bus, protocol, response_id, ISOTP_RESPONSE_TIMEOUT_SECONDS)
    if first is None:
        return {"status": "no_response", "payload": b"", "raw_frames": []}

    raw_frames = [first]
    if not first:
        return {"status": "invalid_response", "payload": b"", "raw_frames": raw_frames}

    frame_type = first[0] >> 4

    # Single Frame
    if frame_type == 0x0:
        payload_length = first[0] & 0x0F
        if payload_length > 7 or len(first) < payload_length + 1:
            return {
                "status": "invalid_response",
                "payload": b"",
                "raw_frames": raw_frames,
                "error": "ISO-TP 單框長度不正確",
            }
        return {
            "status": "ok",
            "payload": first[1:1 + payload_length],
            "raw_frames": raw_frames,
        }

    # First Frame
    if frame_type != 0x1 or len(first) < 3:
        return {
            "status": "invalid_response",
            "payload": b"",
            "raw_frames": raw_frames,
            "error": "未識別的 ISO-TP 起始框",
        }

    total_length = ((first[0] & 0x0F) << 8) | first[1]
    collected = bytearray(first[2:])

    # Flow Control 必須回 physical request ID。
    fc_id = flow_control_id if flow_control_id is not None else request_id
    flow_control = bytes([0x30, 0x00, 0x00, 0, 0, 0, 0, 0])
    send_can(bus, protocol, fc_id, flow_control)

    expected_sequence = 1
    while len(collected) < total_length:
        frame = wait_for_exact_can_id(bus, protocol, response_id, ISOTP_RESPONSE_TIMEOUT_SECONDS)
        if frame is None:
            return {
                "status": "timeout",
                "payload": bytes(collected[:total_length]),
                "raw_frames": raw_frames,
                "error": "ISO-TP 多框回覆逾時",
            }
        raw_frames.append(frame)
        if not frame or (frame[0] >> 4) != 0x2:
            continue
        sequence = frame[0] & 0x0F
        if sequence != expected_sequence:
            return {
                "status": "invalid_response",
                "payload": bytes(collected[:total_length]),
                "raw_frames": raw_frames,
                "error": f"ISO-TP 序號錯誤：預期 {expected_sequence:X}，收到 {sequence:X}",
            }
        collected.extend(frame[1:])
        expected_sequence = (expected_sequence + 1) & 0x0F

    return {
        "status": "ok",
        "payload": bytes(collected[:total_length]),
        "raw_frames": raw_frames,
    }


def request_mode01_pid_from_ecu(
    bus: Any,
    protocol: ProtocolSpec,
    response_id: int,
    pid: int,
) -> dict[str, Any]:
    """讀取一個 Mode 01 PID。

    實車已確認 29-bit functional request 18DB33F1 可正常取得
    18DAF110 / 18DAF111，因此本版優先使用 functional request，
    若該 ECU 沒回再嘗試 physical request。
    """
    physical_id = physical_request_id(protocol, response_id)

    response = send_isotp_request(
        bus,
        protocol=protocol,
        request_id=protocol.functional_request_id,
        response_id=response_id,
        payload=bytes([0x01, pid]),
        flow_control_id=physical_id,
    )
    query_method = "functional"

    if response.get("status") == "no_response":
        response = send_isotp_request(
            bus,
            protocol=protocol,
            request_id=physical_id,
            response_id=response_id,
            payload=bytes([0x01, pid]),
        )
        query_method = "physical_fallback"

    payload = response.get("payload", b"")
    result: dict[str, Any] = {
        "status": response.get("status"),
        "query_method": query_method,
        "data": b"",
        "raw_frames": response.get("raw_frames", []),
    }
    if response.get("error"):
        result["error"] = response["error"]

    if response.get("status") != "ok" or not isinstance(payload, bytes):
        return result

    if payload and payload[0] == 0x7F:
        result["status"] = "negative_response"
        result["negative_response_code"] = f"{payload[2]:02X}" if len(payload) >= 3 else None
        return result

    if len(payload) < 2 or payload[0] != 0x41 or payload[1] != pid:
        result["status"] = "unexpected_response"
        result["error"] = f"PID {pid:02X} 回覆格式不正確"
        return result

    result["data"] = payload[2:]
    return result


def decode_pid(pid: int, data: bytes, response_id: int) -> dict[str, Any]:
    definition = PID_BY_ID[pid]
    if len(data) < definition.payload_length:
        raise ValueError(
            f"PID {pid:02X} 資料長度不足：需要 {definition.payload_length} bytes，收到 {len(data)}"
        )
    payload = data[:definition.payload_length]
    value = definition.decoder(payload)
    if isinstance(value, float):
        value = round(value, 3)
    return {
        "pid": f"01 {pid:02X}",
        "ecu_response_id": fmt_can_id(response_id),
        "label": definition.label,
        "value": value,
        "unit": definition.unit,
        "raw_data": data.hex(" ").upper(),
    }


def decode_dtc_pair(first: int, second: int) -> Optional[str]:
    if first == 0 and second == 0:
        return None
    system = "PCBU"[(first >> 6) & 0x03]
    return f"{system}{(first >> 4) & 0x03}{first & 0x0F:X}{(second >> 4) & 0x0F:X}{second & 0x0F:X}"


def decode_dtc_payload(payload: bytes, expected_service: int) -> list[str]:
    """解碼 ISO 15765-4 CAN 的 Mode 03 / 07 / 0A DTC 回覆。

    CAN 回覆 payload 格式：
        [positive_service, dtc_count, DTC1_HI, DTC1_LO, DTC2_HI, DTC2_LO, ...]

    實車範例：
        47 01 03 54  -> 1 個 pending DTC -> P0354
        47 01 01 34  -> 1 個 pending DTC -> P0134

    舊版從 payload[1:] 直接兩兩解碼，會把 dtc_count 誤當成
    DTC 的高位，因此 47 01 03 54 會被錯解成 P0103。
    """
    if len(payload) < 2 or payload[0] != expected_service + 0x40:
        return []

    dtc_count = int(payload[1])
    if dtc_count <= 0:
        return []

    dtc_data = payload[2:]
    available_pairs = len(dtc_data) // 2
    pair_count = min(dtc_count, available_pairs)

    codes: list[str] = []
    for index in range(pair_count):
        offset = index * 2
        code = decode_dtc_pair(dtc_data[offset], dtc_data[offset + 1])
        if code and code not in codes:
            codes.append(code)

    return codes


def raw_frames_text(frames: list[bytes]) -> list[str]:
    return [" ".join(f"{byte:02X}" for byte in frame) for frame in frames]


def make_dtc_detail(
    code: str,
    mode: DtcModeDefinition,
    protocol: ProtocolSpec,
    response_id: int,
) -> dict[str, Any]:
    system_letter = code[0] if code else "?"
    return {
        "code": code,
        "system": system_letter,
        "system_label": DTC_SYSTEM_LABELS.get(system_letter, "未知系統"),
        "state": mode.key,
        "state_label": mode.label,
        "service": f"{mode.service:02X}",
        "ecu_response_id": fmt_can_id(response_id),
        "ecu_label": ecu_label(protocol, response_id),
    }


def read_all_standard_dtcs(
    bus: Any,
    protocol: ProtocolSpec,
    ecu_ids: list[int],
) -> dict[str, Any]:
    """讀取標準 Mode 03 / 07 / 0A；結果快取 5 秒。"""
    current = time.monotonic()
    signature = (protocol.key, tuple(sorted(ecu_ids)))

    with DTC_CACHE_LOCK:
        cached = DTC_CACHE.get("result")
        cached_signature = DTC_CACHE.get("signature")
        cache_age = current - float(DTC_CACHE.get("scanned_monotonic", 0.0))
        if (
            isinstance(cached, dict)
            and cached_signature == signature
            and cache_age < DTC_SCAN_INTERVAL_SECONDS
        ):
            result = copy.deepcopy(cached)
            result["from_cache"] = True
            result["cache_age_seconds"] = round(max(0.0, cache_age), 3)
            return result

    category_results: dict[str, Any] = {
        mode.key: {
            "service": f"{mode.service:02X}",
            "label": mode.label,
            "codes": [],
        }
        for mode in DTC_MODE_DEFINITIONS
    }
    ecu_results: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []
    all_codes: list[str] = []
    successful_queries = 0

    for response_id in sorted(ecu_ids):
        physical_id = physical_request_id(protocol, response_id)
        ecu_item: dict[str, Any] = {
            "label": ecu_label(protocol, response_id),
            "request_id": fmt_can_id(physical_id),
            "response_id": fmt_can_id(response_id),
            "modes": {},
            "codes": [],
        }

        for mode in DTC_MODE_DEFINITIONS:
            # 先 functional，這是實車已確認可回應的方式。
            response = send_isotp_request(
                bus,
                protocol=protocol,
                request_id=protocol.functional_request_id,
                response_id=response_id,
                payload=bytes([mode.service]),
                flow_control_id=physical_id,
            )
            query_method = "functional"

            if response.get("status") == "no_response":
                response = send_isotp_request(
                    bus,
                    protocol=protocol,
                    request_id=physical_id,
                    response_id=response_id,
                    payload=bytes([mode.service]),
                )
                query_method = "physical_fallback"

            mode_item: dict[str, Any] = {
                "service": f"{mode.service:02X}",
                "label": mode.label,
                "status": response.get("status"),
                "query_method": query_method,
                "codes": [],
                "raw_frames": raw_frames_text(response.get("raw_frames", [])),
            }

            payload = response.get("payload", b"")
            if response.get("status") == "ok" and isinstance(payload, bytes) and payload:
                if payload[0] == 0x7F:
                    mode_item["status"] = "negative_response"
                    mode_item["negative_response_code"] = (
                        f"{payload[2]:02X}" if len(payload) >= 3 else None
                    )
                elif payload[0] == mode.service + 0x40:
                    codes = decode_dtc_payload(payload, mode.service)
                    mode_item["status"] = "ok"
                    mode_item["codes"] = codes
                    successful_queries += 1
                    for code in codes:
                        if code not in category_results[mode.key]["codes"]:
                            category_results[mode.key]["codes"].append(code)
                        if code not in ecu_item["codes"]:
                            ecu_item["codes"].append(code)
                        if code not in all_codes:
                            all_codes.append(code)
                        details.append(make_dtc_detail(code, mode, protocol, response_id))
                else:
                    mode_item["status"] = "unexpected_service"
                    mode_item["error"] = (
                        f"預期 {mode.service + 0x40:02X}，收到 {payload[0]:02X}"
                    )

            if response.get("error"):
                mode_item["error"] = response["error"]
            ecu_item["modes"][mode.key] = mode_item
            time.sleep(0.02)

        ecu_results.append(ecu_item)

    by_system = {letter: [] for letter in DTC_SYSTEM_LABELS}
    for code in all_codes:
        if code and code[0] in by_system:
            by_system[code[0]].append(code)

    if not ecu_ids:
        status = "no_ecu_detected"
    elif successful_queries:
        status = "ok"
    else:
        status = "no_supported_dtc_mode"

    result = {
        "status": status,
        "scope": f"SAE_OBD_over_{'29bit' if protocol.is_extended_id else '11bit'}_CAN_read_only",
        "protocol": protocol.label,
        "read_only": True,
        "scanned_at": now_text(),
        "from_cache": False,
        "detected_ecu_response_ids": [fmt_can_id(item) for item in sorted(ecu_ids)],
        "ecu_count": len(ecu_ids),
        "categories": category_results,
        "ecus": ecu_results,
        "codes": all_codes,
        "details": details,
        "by_system": {
            letter: {
                "label": DTC_SYSTEM_LABELS[letter],
                "codes": codes,
            }
            for letter, codes in by_system.items()
        },
        "limitations": [
            "本次僅讀取標準 SAE OBD Mode 03、07、0A。",
            "不執行清碼、寫入或致動控制。",
            "ABS、SRS、EPS、車身等 Honda 專用模組若不提供標準 OBD DTC，仍需另外加入原廠診斷服務。",
        ],
    }

    with DTC_CACHE_LOCK:
        DTC_CACHE["scanned_monotonic"] = current
        DTC_CACHE["signature"] = signature
        DTC_CACHE["result"] = copy.deepcopy(result)

    return result


def compact_window_sample(state: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    raw_values = state.get("values") if isinstance(state.get("values"), dict) else {}

    for key, item in raw_values.items():
        if not isinstance(item, dict):
            continue
        if item.get("value") is None or item.get("error"):
            continue
        values[key] = {
            field: item.get(field)
            for field in ("pid", "ecu_response_id", "label", "value", "unit")
            if field in item
        }

    connection = state.get("connection") if isinstance(state.get("connection"), dict) else {}
    return {
        "status": state.get("status"),
        "timestamp": state.get("timestamp"),
        "read_scope": "fixed_10_mode01_pids_plus_standard_DTCs",
        "connection": {
            "backend": connection.get("backend"),
            "interface": connection.get("interface"),
            "device": connection.get("device"),
            "bitrate": connection.get("bitrate"),
            "protocol": connection.get("protocol"),
            "request_id": connection.get("request_id"),
            "ecu_response_ids": connection.get("ecu_response_ids") or [],
        },
        "pid_success_count": len(values),
        "pid_total_count": len(values),
        "values": values,
        "dtc_status": state.get("dtc_status"),
        "dtc_codes": copy.deepcopy(state.get("dtc_codes") or []),
        "dtc_codes_detailed": copy.deepcopy(state.get("dtc_codes_detailed") or []),
    }


def capture_obd_window(
    duration_seconds: float = DEFAULT_WINDOW_DURATION,
    poll_interval_seconds: float = WINDOW_POLL_INTERVAL,
) -> dict[str, Any]:
    duration_seconds = min(60.0, max(0.1, float(duration_seconds)))
    poll_interval_seconds = max(0.01, float(poll_interval_seconds))
    session_at_start = get_replay_session()
    if session_at_start is not None:
        # REPLAY 的每一次時間窗查詢都從案例第 0 秒開始，方便重複實驗公平比較。
        session_at_start.restart()

    started_at = now_text()
    deadline = time.monotonic() + duration_seconds
    samples: list[dict[str, Any]] = []
    last_sample_key: Any = None

    while True:
        state = get_output_state()
        timestamp = state.get("timestamp")
        replay_info = state.get("replay_info") if isinstance(state.get("replay_info"), dict) else {}
        if state.get("replay"):
            sample_key = ("replay", replay_info.get("recorded_elapsed_seconds"))
        else:
            sample_key = ("live", timestamp)

        if (
            state.get("status") == "ok"
            and sample_key != last_sample_key
            and sample_key[1] is not None
        ):
            samples.append(compact_window_sample(state))
            last_sample_key = sample_key

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(poll_interval_seconds, remaining))

    ended_at = now_text()
    latest = copy.deepcopy(samples[-1]) if samples else {
        "status": "no_samples",
        "timestamp": ended_at,
        "connection": {},
        "values": {},
        "dtc_codes": [],
        "dtc_scan": {},
        "error": "OBD 連續資料取得失敗：期間內沒有新的完整資料",
    }
    session = get_replay_session()
    latest.update({
        "capture_mode": "obd_reader_time_window",
        "capture_source": (
            "recorded_real_vehicle_replay"
            if session is not None
            else "external_socketcan_obd"
        ),
        "simulated": False,
        "replay": session is not None,
        "data_mode": "replay" if session is not None else "live",
        "duration_seconds": duration_seconds,
        "started_at": started_at,
        "ended_at": ended_at,
        "sample_count": len(samples),
        "samples": samples,
    })
    if session is not None:
        latest["replay_case"] = case_summary(session.case_dir)
    return latest


def read_obd_cycle(bus: Any, interface: str) -> dict[str, Any]:
    """讀取一輪固定 10 PID + 標準 DTC。"""
    with BUS_LOCK:
        protocol, ecu_ids = get_protocol_and_ecus(bus)

        values: dict[str, Any] = {}
        successful = 0
        read_errors = 0

        if protocol is not None and ecu_ids:
            # 實車 29-bit 回了 18DAF110/111；以排序第一個 ECU 當主要數據來源。
            primary_ecu = sorted(ecu_ids)[0]

            for pid in FIXED_PID_IDS:
                response = request_mode01_pid_from_ecu(bus, protocol, primary_ecu, pid)
                data = response.get("data", b"")

                if response.get("status") != "ok" or not isinstance(data, bytes):
                    read_errors += 1
                    time.sleep(0.01)
                    continue

                try:
                    item = decode_pid(pid, data, primary_ecu)
                except Exception:
                    read_errors += 1
                    time.sleep(0.01)
                    continue

                item["query_method"] = response.get("query_method")
                values[PID_BY_ID[pid].key] = item
                successful += 1
                time.sleep(0.01)

            dtc_scan = read_all_standard_dtcs(bus, protocol, ecu_ids)
        else:
            dtc_scan = {
                "status": "skipped_no_obd_response",
                "scope": "SAE_OBD_CAN_read_only",
                "read_only": True,
                "codes": [],
                "details": [],
                "categories": {},
                "ecus": [],
                "detected_ecu_response_ids": [],
            }

        dtc_codes = list(dtc_scan.get("codes", []))

        if successful:
            status = "ok"
        elif protocol is not None and ecu_ids:
            status = "no_fixed_pid_response"
        else:
            status = "no_obd_response"

        return {
            "status": status,
            "capture_source": "external_socketcan_obd",
            "simulated": False,
            "read_scope": "fixed_10_mode01_pids_plus_standard_DTCs",
            "timestamp": now_text(),
            "connection": {
                "backend": "SocketCAN",
                "interface": interface,
                "device": "Kvaser Leaf Light HS v2",
                "bitrate": DEFAULT_BITRATE,
                "protocol": protocol.label if protocol else None,
                "protocol_key": protocol.key if protocol else None,
                "extended_id": protocol.is_extended_id if protocol else None,
                "request_id": fmt_can_id(protocol.functional_request_id) if protocol else None,
                "ecu_response_ids": [fmt_can_id(item) for item in sorted(ecu_ids)],
            },
            "pid_success_count": successful,
            "pid_total_count": successful,
            "pid_error_count": read_errors,
            "configured_pid_count": len(FIXED_PID_IDS),
            "values": values,
            "dtc_status": dtc_scan.get("status"),
            "dtc_codes": dtc_codes,
            "dtc_codes_detailed": dtc_scan.get("details", []),
            "dtc_scan": dtc_scan,
        }


def write_json_file(data: dict[str, Any]) -> None:
    temporary = OUTPUT_JSON.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, OUTPUT_JSON)


def format_value(item: dict[str, Any]) -> str:
    value = item.get("value")
    if value is None:
        return "無回覆"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{value} {item.get('unit', '')}".strip()


def display_state(data: dict[str, Any]) -> None:
    print("\033[2J\033[H", end="")
    print("Raspberry Pi SocketCAN OBD-II 即時讀取器（29-bit/11-bit Auto）")
    print("=" * 72)
    connection = data.get("connection", {})
    print(f"介面：{connection.get('interface', '-')}")
    print(f"協定：{connection.get('protocol') or '尚未偵測'}")
    print(f"Request ID：{connection.get('request_id') or '-'}")
    print("ECU：" + (", ".join(connection.get("ecu_response_ids") or []) or "未偵測"))
    print(f"時間：{data.get('timestamp', '-')}")
    print(f"本輪實際讀到：{data.get('pid_success_count', 0)}/{len(FIXED_PID_IDS)} 個固定 PID")
    if data.get("error"):
        print(f"狀態：{data['error']}")
    print("-" * 72)

    values = data.get("values") if isinstance(data.get("values"), dict) else {}
    for item in values.values():
        label = str(item.get("label") or item.get("pid") or "未知 PID")
        pid = str(item.get("pid") or "-")
        ecu = str(item.get("ecu_response_id") or "-")
        print(f"{label:<25} {format_value(item):>22}   {pid} ECU {ecu}")

    print("-" * 72)
    dtc_scan = data.get("dtc_scan", {})
    categories = dtc_scan.get("categories", {}) if isinstance(dtc_scan, dict) else {}
    for mode in DTC_MODE_DEFINITIONS:
        codes = categories.get(mode.key, {}).get("codes", [])
        print(f"{mode.label}：" + (", ".join(codes) if codes else "無"))

    print(f"最新資料：http://127.0.0.1:{DEFAULT_API_PORT}/obd")
    print(f"故障碼資料：http://127.0.0.1:{DEFAULT_API_PORT}/obd/dtc")
    print(f"AI 10秒資料：http://127.0.0.1:{DEFAULT_API_PORT}/obd/window")
    print("按 Ctrl+C 停止")


class ObdApiHandler(BaseHTTPRequestHandler):
    def send_json(self, status_code: int, data: dict[str, Any]) -> None:
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/health":
            state = get_output_state()
            connection = state.get("connection") if isinstance(state.get("connection"), dict) else {}
            mode_status = get_case_mode_status()
            self.send_json(200, {
                "ok": state.get("status") == "ok",
                "status": state.get("status"),
                "timestamp": state.get("timestamp"),
                "capture_source": state.get("capture_source"),
                "simulated": False,
                "replay": bool(state.get("replay")),
                "data_mode": mode_status.get("mode"),
                "active_case": mode_status.get("active_case"),
                "interface": connection.get("interface"),
                "device": connection.get("device"),
                "protocol": connection.get("protocol"),
                "request_id": connection.get("request_id"),
                "ecu_response_ids": connection.get("ecu_response_ids") or [],
                "pid_success_count": state.get("pid_success_count", 0),
                "pid_total_count": state.get("pid_total_count", 0),
                "error": state.get("error"),
            })
            return

        if path == "/obd":
            self.send_json(200, get_output_state())
            return

        if path == "/obd/dtc":
            state = get_output_state()
            self.send_json(200, {
                "status": state.get("dtc_status"),
                "timestamp": state.get("timestamp"),
                "dtc_codes": state.get("dtc_codes", []),
                "dtc_codes_detailed": state.get("dtc_codes_detailed", []),
                "dtc_scan": state.get("dtc_scan", {}),
                "capture_source": state.get("capture_source"),
                "replay": bool(state.get("replay")),
                "data_mode": state.get("data_mode"),
            })
            return

        if path == "/case/status":
            self.send_json(200, get_case_mode_status())
            return

        if path == "/case/list":
            self.send_json(200, {
                "cases_dir": str(CASES_DIR),
                "cases": [case_summary(item) for item in list_case_dirs()],
            })
            return

        if path == "/obd/window":
            query = parse_qs(parsed.query)
            raw_duration = query.get("seconds", [str(DEFAULT_WINDOW_DURATION)])[0]
            try:
                duration = float(raw_duration)
            except ValueError:
                self.send_json(400, {"error": "seconds 必須是數字，範圍 0.1～60"})
                return
            self.send_json(200, capture_obd_window(duration_seconds=duration))
            return

        self.send_json(404, {
            "error": "not_found",
            "paths": [
                "/health", "/obd", "/obd/dtc", "/obd/window",
                "/case/status", "/case/list",
            ],
        })

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("JSON body 必須是 object")
        return data

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/")
        try:
            payload = self.read_json()
        except Exception as exc:
            self.send_json(400, {"error": f"無效 JSON：{exc}"})
            return

        try:
            if path == "/case/record/start":
                live_state = get_live_state()
                connection = live_state.get("connection") if isinstance(live_state.get("connection"), dict) else {}
                interface = str(connection.get("interface") or DEFAULT_INTERFACE)
                self.send_json(200, start_case_recording(payload, interface))
                return
            if path == "/case/record/stop":
                self.send_json(200, stop_case_recording())
                return
            if path == "/case/replay/start":
                self.send_json(200, start_replay(str(payload.get("folder") or "")))
                return
            if path == "/case/replay/stop":
                self.send_json(200, stop_replay())
                return
            if path == "/case/replay/restart":
                self.send_json(200, restart_replay())
                return
            if path == "/case/delete":
                self.send_json(200, delete_case(str(payload.get("folder") or "")))
                return
        except FileNotFoundError as exc:
            self.send_json(404, {"error": str(exc)})
            return
        except (ValueError, RuntimeError) as exc:
            self.send_json(409, {"error": str(exc)})
            return
        except Exception as exc:
            self.send_json(500, {"error": f"案例管理失敗：{exc}"})
            return

        self.send_json(404, {
            "error": "not_found",
            "paths": [
                "/case/record/start", "/case/record/stop",
                "/case/replay/start", "/case/replay/stop",
                "/case/replay/restart", "/case/delete",
            ],
        })

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def start_api_server(host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), ObdApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def run_self_test() -> None:
    rpm = decode_pid(0x0C, bytes.fromhex("0C 40"), 0x18DAF110)
    assert rpm["value"] == 784.0, rpm
    assert physical_request_id(PROTOCOL_29, 0x18DAF110) == 0x18DA10F1
    assert physical_request_id(PROTOCOL_29, 0x18DAF111) == 0x18DA11F1
    assert is_response_id(PROTOCOL_29, 0x18DAF110, True)
    assert is_response_id(PROTOCOL_29, 0x18DAF111, True)
    assert not is_response_id(PROTOCOL_29, 0x7E8, False)
    assert is_response_id(PROTOCOL_11, 0x7E8, False)
    assert decode_dtc_pair(0x01, 0x71) == "P0171"
    assert decode_dtc_pair(0x41, 0x23) == "C0123"
    assert decode_dtc_pair(0x81, 0x50) == "B0150"
    assert decode_dtc_pair(0xC1, 0x00) == "U0100"
    assert decode_dtc_pair(0x00, 0x00) is None

    # ISO 15765-4 CAN positive response 後第一個 byte 是 DTC 數量。
    assert decode_dtc_payload(bytes.fromhex("43 01 01 71"), 0x03) == ["P0171"]
    assert decode_dtc_payload(bytes.fromhex("43 02 03 01 C1 01"), 0x03) == ["P0301", "U0101"]
    assert decode_dtc_payload(bytes.fromhex("47 01 03 54"), 0x07) == ["P0354"]
    assert decode_dtc_payload(bytes.fromhex("47 01 01 34"), 0x07) == ["P0134"]
    assert decode_dtc_payload(bytes.fromhex("4A 01 01 71"), 0x0A) == ["P0171"]
    assert decode_dtc_payload(bytes.fromhex("4A 00"), 0x0A) == []

    print("自我測試成功：29-bit addressing、784 RPM、CAN DTC count、P0354/P0134 解碼正常。")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Raspberry Pi SocketCAN OBD-II 29-bit/11-bit 自動讀取器")
    parser.add_argument("--interface", default=DEFAULT_INTERFACE, help="SocketCAN 介面，預設 can0")
    parser.add_argument("--interval", type=float, default=DEFAULT_CYCLE_INTERVAL, help="每輪完成後等待秒數")
    parser.add_argument("--reconnect-delay", type=float, default=DEFAULT_RECONNECT_DELAY, help="CAN 中斷後重連秒數")
    parser.add_argument("--host", default=DEFAULT_API_HOST, help="HTTP API 綁定位址")
    parser.add_argument("--port", type=int, default=DEFAULT_API_PORT, help="HTTP API Port")
    parser.add_argument("--no-api", action="store_true", help="不啟動 HTTP API")
    parser.add_argument("--once", action="store_true", help="只讀一輪後結束")
    parser.add_argument("--self-test", action="store_true", help="只執行內建測試")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.self_test:
        run_self_test()
        return 0

    if CAN_IMPORT_ERROR is not None or can is None:
        print("缺少 python-can，請執行：")
        print("~/ve-diagnostics/venv/bin/pip install python-can")
        print(f"原始錯誤：{CAN_IMPORT_ERROR}")
        return 1

    api_server: Optional[ThreadingHTTPServer] = None
    bus = None
    exit_code = 0

    try:
        if not args.no_api:
            api_server = start_api_server(args.host, args.port)

        while True:
            if bus is None:
                connecting_state = {
                    "status": "connecting_can",
                    "capture_source": "external_socketcan_obd",
                    "simulated": False,
                    "timestamp": now_text(),
                    "connection": {
                        "backend": "SocketCAN",
                        "interface": args.interface,
                        "device": "Kvaser Leaf Light HS v2",
                        "bitrate": DEFAULT_BITRATE,
                        "protocol": None,
                        "request_id": None,
                        "ecu_response_ids": [],
                    },
                    "pid_success_count": 0,
                    "pid_total_count": 0,
                    "values": {},
                    "dtc_codes": [],
                }
                set_live_state(connecting_state)
                write_json_file(connecting_state)

                try:
                    bus = can.Bus(interface="socketcan", channel=args.interface)
                    with DTC_CACHE_LOCK:
                        DTC_CACHE["scanned_monotonic"] = 0.0
                        DTC_CACHE["signature"] = None
                        DTC_CACHE["result"] = None
                except Exception as exc:
                    unavailable = copy.deepcopy(connecting_state)
                    unavailable.update({
                        "status": "can_unavailable",
                        "timestamp": now_text(),
                        "error": f"無法開啟 {args.interface}：{exc}",
                    })
                    set_live_state(unavailable)
                    write_json_file(unavailable)
                    display_state(unavailable)
                    if args.once:
                        return 1
                    time.sleep(max(0.2, args.reconnect_delay))
                    continue

            try:
                data = read_obd_cycle(bus, args.interface)
                set_live_state(data)
                write_json_file(data)
                display_state(data)

                if args.once:
                    return 0 if data.get("status") == "ok" else 2
                time.sleep(max(0.05, args.interval))

            except Exception as exc:
                interrupted = {
                    "status": "can_disconnected",
                    "capture_source": "external_socketcan_obd",
                    "simulated": False,
                    "timestamp": now_text(),
                    "error": f"外接 OBD CAN 通訊中斷：{exc}",
                    "connection": {
                        "backend": "SocketCAN",
                        "interface": args.interface,
                        "device": "Kvaser Leaf Light HS v2",
                        "bitrate": DEFAULT_BITRATE,
                        "protocol": None,
                        "request_id": None,
                        "ecu_response_ids": [],
                    },
                    "pid_success_count": 0,
                    "pid_total_count": 0,
                    "values": {},
                    "dtc_codes": [],
                }
                set_live_state(interrupted)
                write_json_file(interrupted)
                display_state(interrupted)
                try:
                    bus.shutdown()
                except Exception:
                    pass
                bus = None
                if args.once:
                    return 1
                time.sleep(max(0.2, args.reconnect_delay))

    except KeyboardInterrupt:
        print("\n已停止讀取。")
    except OSError as exc:
        print(f"\nHTTP API 啟動失敗：{exc}")
        exit_code = 1
    finally:
        if api_server is not None:
            api_server.shutdown()
            api_server.server_close()
        if bus is not None:
            try:
                bus.shutdown()
            except Exception:
                pass

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
