# -*- coding: utf-8 -*-
"""
VE Diagnostics V10.2 - 派工單式 AI 智慧車輛診斷網站（創意動畫 + 維修動畫整合版）

功能重點
--------
1. 保留 OBD → RAG/SQL → 專家會診 → 報告 的診斷流程。
2. 診斷中動畫改為 AI 總監機器人 / RAG 文件 / 專家機器人 / 報告書寫動畫。
3. 被呼叫的專家會亮起，並顯示 AI 總監到專家的閃爍連線；未被呼叫者維持暗色。
4. 維修派工單仍維持一項一行，點擊項目看詳細內容。
5. 若該故障項目有對應維修動畫，詳細內容裡會出現「查看維修動畫」按鈕。
6. 網站可直接播放 repair_videos 資料夾內的 mp4 檔。
7. 首頁只顯示實際讀到的 OBD 項目；不顯示歷史診斷列表；診斷完成跳轉派工單頁。
"""

from __future__ import annotations

import copy
import json
import mimetypes
import os
import re
import sqlite3
import subprocess
import shutil
import socket
import struct
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


HOST = os.environ.get("VE_HOST", "0.0.0.0")
PORT = int(os.environ.get("VE_PORT", "5000"))
OBD_API_BASE_URL = os.environ.get("OBD_API_BASE_URL", "http://127.0.0.1:8768").rstrip("/")
OBD_API_URL = os.environ.get("OBD_API_URL", f"{OBD_API_BASE_URL}/obd")
OBD_HEALTH_URL = os.environ.get("OBD_HEALTH_URL", f"{OBD_API_BASE_URL}/health")
N8N_WEBHOOK_URL = os.environ.get(
    "N8N_WEBHOOK_URL",
    "http://127.0.0.1:5678/webhook/REPLACE_WITH_N8N_CHAT_WEBHOOK",
).strip()
N8N_TIMEOUT = float(os.environ.get("N8N_TIMEOUT", "1800"))
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("VE_DB_PATH", str(BASE_DIR / "ai_diagnostic_reports.db"))).resolve()
REPAIR_VIDEOS_DIR = Path(os.environ.get("REPAIR_VIDEOS_DIR", str(BASE_DIR / "repair_videos"))).resolve()

# 實車目前已驗證：2009 Honda Civic 1.8L 使用 ISO 15765-4 CAN 29-bit / 500 kbit/s
# Mode 04 為 SAE OBD 清除排放相關故障碼；使用者需在網站上再次確認後才會送出。
CAN_INTERFACE = os.environ.get("VE_CAN_INTERFACE", "can0").strip() or "can0"
CLEAR_DTC_CAN_FRAME = os.environ.get(
    "VE_CLEAR_DTC_CAN_FRAME",
    "18DB33F1#0104000000000000",
).strip()

VEHICLE_PROFILE = {
    "model": os.environ.get("VE_VEHICLE_MODEL", "2009 Honda Civic 1.8L").strip(),
    "year": os.environ.get("VE_VEHICLE_YEAR", "2009").strip(),
    "engine": os.environ.get("VE_VEHICLE_ENGINE", "R18A 1.8L i-VTEC").strip(),
    "vin": os.environ.get("VE_VEHICLE_VIN", "").strip(),
}

REPAIR_VIDEO_MAP = {
    "P0351": "case01.mp4",  # 點火線圈
    "P0113": "case02.mp4",  # IAT
    "P0122": "case03.mp4",  # 油門踏板/節氣門位置低輸入
    "P0504": "case04.mp4",  # 煞車開關
    "P0171": "case05.mp4",  # 進氣漏氣/過稀
    "P0341": "case06.mp4",  # 凸輪軸感知器
}

DB_LOCK = threading.Lock()
PROGRESS_LOCK = threading.Lock()
DIAGNOSIS_PROGRESS: dict[str, Any] = {
    "case_id": "",
    "mode": "initial",
    "stage": "idle",
    "message": "等待建立診斷案件",
    "experts": {},
    "revision": 0,
    "updated_at": None,
}

# -----------------------------------------------------------------------------
# OBD SIGNAL / ECU 模擬控制台
# -----------------------------------------------------------------------------
# 用途：在桌上型測試環境中，讓外接 OBD HUD / 儀表主動詢問 PID，
# Raspberry Pi 透過 SocketCAN 回覆目前網站設定的模擬值。
# 支援標準 11-bit OBD (7DF/7E0 -> 7E8) 與 Honda 常見 29-bit
# (18DB33F1 / 18DA10F1 -> 18DAF110)。
#
# 這個模式只做 ECU 模擬；不會改動 LIVE / RECORD / REPLAY 原有流程。
OBD_SIGNAL_LOCK = threading.Lock()
OBD_SIGNAL_STOP = threading.Event()
OBD_SIGNAL_THREAD: threading.Thread | None = None
OBD_SIGNAL_LIMITS = {
    "throttle": (0.0, 100.0),
    "rpm": (0.0, 8000.0),
    "speed": (0.0, 220.0),
    "coolant": (-40.0, 150.0),
    "load": (0.0, 100.0),
    "iat": (-40.0, 120.0),
    "maf": (0.0, 200.0),
}
OBD_SIGNAL_STATE: dict[str, Any] = {
    "active": False,
    "interface": CAN_INTERFACE,
    "values": {
        "throttle": 12.0,
        "rpm": 800.0,
        "speed": 0.0,
        "coolant": 90.0,
        "load": 18.0,
        "iat": 30.0,
        "maf": 3.5,
    },
    "request_count": 0,
    "response_count": 0,
    "last_request": "",
    "last_response": "",
    "last_error": "",
    "started_at": None,
    "updated_at": None,
}

CAN_EFF_FLAG = 0x80000000
CAN_EFF_MASK = 0x1FFFFFFF
CAN_SFF_MASK = 0x000007FF
CAN_FRAME_STRUCT = struct.Struct("=IB3x8s")


def clamp_signal_value(name: str, value: Any) -> float:
    lo, hi = OBD_SIGNAL_LIMITS[name]
    try:
        n = float(value)
    except (TypeError, ValueError):
        n = float(OBD_SIGNAL_STATE["values"].get(name, lo))
    return max(lo, min(hi, n))


def get_obd_signal_status() -> dict[str, Any]:
    with OBD_SIGNAL_LOCK:
        return copy.deepcopy(OBD_SIGNAL_STATE)


def set_obd_signal_values(values: dict[str, Any]) -> dict[str, Any]:
    with OBD_SIGNAL_LOCK:
        current = OBD_SIGNAL_STATE.setdefault("values", {})
        for name in OBD_SIGNAL_LIMITS:
            if name in values:
                current[name] = clamp_signal_value(name, values[name])
        OBD_SIGNAL_STATE["updated_at"] = now_iso()
        return copy.deepcopy(OBD_SIGNAL_STATE)


def _supported_pid_mask(pids: set[int], base: int = 0x00) -> bytes:
    # Mode 01 PID 00 回覆 PID 01..20；PID 20 回覆 21..40，以此類推。
    mask = 0
    for pid in pids:
        if base < pid <= base + 0x20:
            bit = 0x20 - (pid - base)
            mask |= 1 << bit
    return mask.to_bytes(4, "big")


def _mode01_payload(pid: int, values: dict[str, Any]) -> bytes | None:
    supported = {0x04, 0x05, 0x0C, 0x0D, 0x0F, 0x10, 0x11}
    if pid == 0x00:
        return bytes([0x41, 0x00]) + _supported_pid_mask(supported, 0x00)
    if pid == 0x20:
        return bytes([0x41, 0x20, 0x00, 0x00, 0x00, 0x00])
    if pid == 0x40:
        return bytes([0x41, 0x40, 0x00, 0x00, 0x00, 0x00])
    if pid == 0x04:
        a = round(clamp_signal_value("load", values.get("load")) * 255 / 100)
        return bytes([0x41, pid, a & 0xFF])
    if pid == 0x05:
        a = round(clamp_signal_value("coolant", values.get("coolant")) + 40)
        return bytes([0x41, pid, a & 0xFF])
    if pid == 0x0C:
        raw = round(clamp_signal_value("rpm", values.get("rpm")) * 4)
        return bytes([0x41, pid, (raw >> 8) & 0xFF, raw & 0xFF])
    if pid == 0x0D:
        a = round(clamp_signal_value("speed", values.get("speed")))
        return bytes([0x41, pid, a & 0xFF])
    if pid == 0x0F:
        a = round(clamp_signal_value("iat", values.get("iat")) + 40)
        return bytes([0x41, pid, a & 0xFF])
    if pid == 0x10:
        raw = round(clamp_signal_value("maf", values.get("maf")) * 100)
        return bytes([0x41, pid, (raw >> 8) & 0xFF, raw & 0xFF])
    if pid == 0x11:
        a = round(clamp_signal_value("throttle", values.get("throttle")) * 255 / 100)
        return bytes([0x41, pid, a & 0xFF])
    return None


def _build_obd_response(request_data: bytes, values: dict[str, Any]) -> bytes | None:
    if len(request_data) < 2:
        return None
    payload_len = int(request_data[0])
    if payload_len <= 0 or payload_len > 7:
        return None
    mode = int(request_data[1])

    if mode == 0x01 and len(request_data) >= 3:
        payload = _mode01_payload(int(request_data[2]), values)
        if payload is None:
            return None
    elif mode == 0x03:
        # 無模擬 DTC：Mode 03 正回覆 43，後面補 00。
        payload = bytes([0x43, 0x00, 0x00])
    elif mode == 0x04:
        payload = bytes([0x44])
    else:
        return None

    if len(payload) > 7:
        return None
    return bytes([len(payload)]) + payload + bytes(7 - len(payload))


def _request_to_response_id(can_id: int, is_extended: bool) -> int | None:
    if not is_extended:
        if can_id in {0x7DF, 0x7E0}:
            return 0x7E8
        return None
    # 29-bit OBD functional request
    if can_id == 0x18DB33F1:
        return 0x18DAF110
    # 29-bit physical tester(F1) -> engine ECU(10) request
    if can_id == 0x18DA10F1:
        return 0x18DAF110
    return None


def _send_can_frame(sock: socket.socket, can_id: int, extended: bool, data: bytes) -> None:
    wire_id = can_id | (CAN_EFF_FLAG if extended else 0)
    frame = CAN_FRAME_STRUCT.pack(wire_id, min(len(data), 8), data[:8].ljust(8, b"\x00"))
    sock.send(frame)


def obd_signal_responder_loop() -> None:
    global OBD_SIGNAL_THREAD
    sock: socket.socket | None = None
    try:
        sock = socket.socket(socket.PF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        sock.settimeout(0.5)
        sock.bind((CAN_INTERFACE,))
        with OBD_SIGNAL_LOCK:
            OBD_SIGNAL_STATE["active"] = True
            OBD_SIGNAL_STATE["last_error"] = ""
            OBD_SIGNAL_STATE["started_at"] = now_iso()
            OBD_SIGNAL_STATE["updated_at"] = now_iso()

        while not OBD_SIGNAL_STOP.is_set():
            try:
                raw = sock.recv(CAN_FRAME_STRUCT.size)
            except socket.timeout:
                continue
            except OSError as exc:
                if not OBD_SIGNAL_STOP.is_set():
                    with OBD_SIGNAL_LOCK:
                        OBD_SIGNAL_STATE["last_error"] = str(exc)
                break

            if len(raw) < CAN_FRAME_STRUCT.size:
                continue
            wire_id, dlc, raw_data = CAN_FRAME_STRUCT.unpack(raw[:CAN_FRAME_STRUCT.size])
            is_extended = bool(wire_id & CAN_EFF_FLAG)
            can_id = wire_id & (CAN_EFF_MASK if is_extended else CAN_SFF_MASK)
            response_id = _request_to_response_id(can_id, is_extended)
            if response_id is None:
                continue

            request_data = bytes(raw_data[:dlc])
            with OBD_SIGNAL_LOCK:
                values = dict(OBD_SIGNAL_STATE.get("values") or {})
            response_data = _build_obd_response(request_data, values)
            if response_data is None:
                continue

            try:
                _send_can_frame(sock, response_id, is_extended, response_data)
            except OSError as exc:
                with OBD_SIGNAL_LOCK:
                    OBD_SIGNAL_STATE["last_error"] = str(exc)
                continue

            req_text = f"{can_id:08X}" if is_extended else f"{can_id:03X}"
            res_text = f"{response_id:08X}" if is_extended else f"{response_id:03X}"
            with OBD_SIGNAL_LOCK:
                OBD_SIGNAL_STATE["request_count"] = int(OBD_SIGNAL_STATE.get("request_count") or 0) + 1
                OBD_SIGNAL_STATE["response_count"] = int(OBD_SIGNAL_STATE.get("response_count") or 0) + 1
                OBD_SIGNAL_STATE["last_request"] = f"{req_text}  {request_data.hex(' ').upper()}"
                OBD_SIGNAL_STATE["last_response"] = f"{res_text}  {response_data.hex(' ').upper()}"
                OBD_SIGNAL_STATE["updated_at"] = now_iso()
    except Exception as exc:
        with OBD_SIGNAL_LOCK:
            OBD_SIGNAL_STATE["last_error"] = str(exc)
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        with OBD_SIGNAL_LOCK:
            OBD_SIGNAL_STATE["active"] = False
            OBD_SIGNAL_STATE["updated_at"] = now_iso()
        OBD_SIGNAL_THREAD = None


def start_obd_signal_responder() -> dict[str, Any]:
    global OBD_SIGNAL_THREAD
    if OBD_SIGNAL_THREAD is not None and OBD_SIGNAL_THREAD.is_alive():
        return get_obd_signal_status()
    OBD_SIGNAL_STOP.clear()
    with OBD_SIGNAL_LOCK:
        OBD_SIGNAL_STATE["request_count"] = 0
        OBD_SIGNAL_STATE["response_count"] = 0
        OBD_SIGNAL_STATE["last_request"] = ""
        OBD_SIGNAL_STATE["last_response"] = ""
        OBD_SIGNAL_STATE["last_error"] = ""
    OBD_SIGNAL_THREAD = threading.Thread(target=obd_signal_responder_loop, name="obd-signal-responder", daemon=True)
    OBD_SIGNAL_THREAD.start()
    time.sleep(0.08)
    return get_obd_signal_status()


def stop_obd_signal_responder() -> dict[str, Any]:
    global OBD_SIGNAL_THREAD
    OBD_SIGNAL_STOP.set()
    thread = OBD_SIGNAL_THREAD
    if thread is not None and thread.is_alive():
        thread.join(timeout=1.0)
    with OBD_SIGNAL_LOCK:
        OBD_SIGNAL_STATE["active"] = False
        OBD_SIGNAL_STATE["updated_at"] = now_iso()
    return get_obd_signal_status()

WORK_ORDER_OUTPUT_CONTRACT = r"""
【網站派工單輸出契約｜最高優先】
最終答案只能輸出一個合法 JSON object，不要 Markdown code fence，不要在 JSON 前後加解釋。

JSON 必須符合：
{
  "schema_version": "ve_work_order_v1",
  "vehicle": {
    "model": "2009 Honda Civic 1.8L",
    "odometer_km": 105230,
    "dtcs": ["P0101"]
  },
  "work_order_items": [
    {
      "id": "WO-01",
      "type": "maintenance|inspection|repair|safety",
      "title": "一行派工項目名稱",
      "priority": "urgent|high|normal|recommend",
      "status": "pending",
      "summary": "列表上可快速理解的一句話",
      "reason": "為什麼建立這一項",
      "evidence": ["OBD / RAG / 歷史資料庫 / 專家證據"],
      "detail": "點開後顯示的完整技術說明",
      "actions": ["檢查或維修步驟"],
      "verification": ["完成後如何確認"],
      "source": "maintenance_history|obd|rag|expert|safety"
    }
  ],
  "full_report": {
    "vehicle_basic": "車輛、里程、症狀與診斷條件",
    "dtc_analysis": "DTC 與意義；無 DTC 時明確寫無故障碼",
    "obd_analysis": "10 秒 OBD / PID 的重點與異常趨勢",
    "technical_evidence": "RAG、維修手冊、TSB、召回、案例、歷史維修資料比對",
    "expert_consultation": "實際呼叫專家的結論摘要",
    "safety_assessment": "安全專家判斷與能否繼續行駛",
    "final_diagnosis": "總 AI 綜合判斷，區分已確認與待確認",
    "repair_recommendation": "依優先順序整理檢查、維修與完成後驗證方式"
  }
}

派工單建立規則：
1. work_order_items 只放真正需要執行的保養、檢查、維修或安全處置，不把長篇診斷敘述拆成很多假項目。
2. 同一根因不要重複建立多項。
3. 如果歷史資料庫回傳 maintenance_due / maintenance package，且目前輸入里程已達該保養里程，並且資料庫沒有已完成紀錄，必須建立 type=maintenance 的派工項目，例如「100,000 公里定期保養」。
4. 不得自己發明定保里程或保養內容；只有工具 / 資料庫 / RAG 實際提供時才能加入。
5. 若只到 51,023 km，而資料庫確認 50,000 km 定保到期且尚未完成，建立「50,000 公里定期保養」。
6. 故障診斷項目放在定保項目之後或依 priority 排序；urgent/high 優先於一般建議。
7. 每一項 title 必須短，適合網站一行顯示；詳細內容全部放 detail/actions/verification，使用者點開才看。
8. full_report 是完整報告，網站只在使用者按「點我看詳細報告」時顯示。
""".strip()


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def db_connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init_database() -> None:
    with DB_LOCK, db_connect() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                vehicle TEXT NOT NULL DEFAULT '',
                complaint TEXT NOT NULL DEFAULT '',
                report TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS diagnostic_cases (
                case_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                vehicle TEXT NOT NULL DEFAULT '',
                odometer_km INTEGER NOT NULL DEFAULT 0,
                complaint TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'initial_running',
                initial_session_id TEXT NOT NULL DEFAULT '',
                initial_report_id INTEGER,
                repair_note TEXT NOT NULL DEFAULT '',
                recheck_session_id TEXT NOT NULL DEFAULT '',
                recheck_report_id INTEGER
            )
            """
        )
        cols = {r[1] for r in con.execute("PRAGMA table_info(diagnostic_cases)").fetchall()}
        if "odometer_km" not in cols:
            con.execute("ALTER TABLE diagnostic_cases ADD COLUMN odometer_km INTEGER NOT NULL DEFAULT 0")
        con.commit()


def fetch_json(url: str, timeout: float = 4.0) -> Any:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        raw = res.read().decode(res.headers.get_content_charset() or "utf-8")
    return json.loads(raw)


def post_json(url: str, payload: dict[str, Any] | None = None, timeout: float = 10.0) -> Any:
    body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as res:
        raw = res.read().decode(res.headers.get_content_charset() or "utf-8")
    return json.loads(raw)


def normalize_obd(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {"error": "OBD 服務回傳格式錯誤", "values": {}, "dtc_codes": []}
    out = copy.deepcopy(data)
    values = out.get("values")
    if not isinstance(values, dict):
        values = {}
    out["values"] = values
    dtcs = out.get("dtc_codes")
    if not isinstance(dtcs, list):
        dtcs = out.get("dtcs") if isinstance(out.get("dtcs"), list) else []
    out["dtc_codes"] = dtcs
    vi = out.get("vehicle_info") if isinstance(out.get("vehicle_info"), dict) else {}
    merged = dict(VEHICLE_PROFILE)
    merged.update({k: v for k, v in vi.items() if v not in (None, "")})
    out["vehicle_info"] = merged
    out["available_pid_count"] = len(values)
    out["obd_available"] = bool(values) or bool(dtcs)
    return out


def safe_parse_json_text(text: str) -> dict[str, Any] | None:
    if not isinstance(text, str) or not text.strip():
        return None
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            obj = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            return None
    if isinstance(obj, dict):
        if not obj.get("work_order_items"):
            for key in ("output", "text", "message", "report"):
                nested = obj.get(key)
                if isinstance(nested, str):
                    parsed = safe_parse_json_text(nested)
                    if parsed:
                        return parsed
        return obj
    return None


def normalize_work_order(ai_text: str, fallback_vehicle: str, fallback_complaint: str) -> dict[str, Any]:
    obj = safe_parse_json_text(ai_text) or {}
    vehicle = obj.get("vehicle") if isinstance(obj.get("vehicle"), dict) else {}
    if not vehicle.get("model"):
        vehicle["model"] = fallback_vehicle or VEHICLE_PROFILE["model"]
    try:
        vehicle["odometer_km"] = int(float(vehicle.get("odometer_km") or 0))
    except (TypeError, ValueError):
        vehicle["odometer_km"] = 0
    if not isinstance(vehicle.get("dtcs"), list):
        vehicle["dtcs"] = []

    items = obj.get("work_order_items")
    if not isinstance(items, list):
        items = []
    normalized_items: list[dict[str, Any]] = []
    for idx, raw in enumerate(items, 1):
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        if not title:
            continue
        normalized_items.append({
            "id": str(raw.get("id") or f"WO-{idx:02d}"),
            "type": str(raw.get("type") or "inspection"),
            "title": title,
            "priority": str(raw.get("priority") or "normal"),
            "status": str(raw.get("status") or "pending"),
            "summary": str(raw.get("summary") or ""),
            "reason": str(raw.get("reason") or ""),
            "evidence": raw.get("evidence") if isinstance(raw.get("evidence"), list) else [],
            "detail": str(raw.get("detail") or ""),
            "actions": raw.get("actions") if isinstance(raw.get("actions"), list) else [],
            "verification": raw.get("verification") if isinstance(raw.get("verification"), list) else [],
            "source": str(raw.get("source") or "expert"),
        })

    full = obj.get("full_report") if isinstance(obj.get("full_report"), dict) else {}
    if not full:
        full = {
            "vehicle_basic": f"車輛：{vehicle['model']}；里程：{vehicle['odometer_km']} km；症狀：{fallback_complaint or '未提供'}",
            "dtc_analysis": "請查看 AI 原始診斷內容。",
            "obd_analysis": "請查看 AI 原始診斷內容。",
            "technical_evidence": "請查看 AI 原始診斷內容。",
            "expert_consultation": "請查看 AI 原始診斷內容。",
            "safety_assessment": "請查看 AI 原始診斷內容。",
            "final_diagnosis": ai_text,
            "repair_recommendation": "依派工單項目執行。",
        }
    return {
        "schema_version": str(obj.get("schema_version") or "ve_work_order_v1"),
        "vehicle": vehicle,
        "work_order_items": normalized_items,
        "full_report": full,
        "raw_ai_output": ai_text,
    }


def reset_progress(case_id: str, mode: str) -> None:
    with PROGRESS_LOCK:
        DIAGNOSIS_PROGRESS.clear()
        DIAGNOSIS_PROGRESS.update({
            "case_id": case_id,
            "mode": mode,
            "stage": "obd",
            "message": "正在讀取 OBD-II 資料",
            "experts": {},
            "revision": 1,
            "updated_at": now_iso(),
        })


def update_progress(payload: dict[str, Any]) -> dict[str, Any]:
    aliases = {
        "powertrain_expert": "powertrain", "powertrain": "powertrain",
        "electrical_expert": "electrical", "electrical": "electrical",
        "brake_expert": "brake", "brake": "brake",
        "chassis_expert": "chassis", "chassis": "chassis",
        "cooling_hvac_expert": "cooling", "cooling_hvac": "cooling", "cooling": "cooling",
        "safety_expert": "safety", "safety": "safety",
    }
    stage_map = {
        "preparing": "obd", "obd": "obd", "data": "data",
        "experts": "experts", "report": "report", "complete": "complete", "error": "error",
    }
    with PROGRESS_LOCK:
        stage = stage_map.get(str(payload.get("stage") or "").lower())
        if stage:
            DIAGNOSIS_PROGRESS["stage"] = stage
        msg = str(payload.get("message") or "").strip()
        if msg:
            DIAGNOSIS_PROGRESS["message"] = msg[:300]
        expert = aliases.get(str(payload.get("expert") or "").strip().lower())
        if expert:
            status = str(payload.get("status") or "consulting").strip().lower()
            if status not in {"waiting", "consulting", "done", "skipped", "error"}:
                status = "consulting"
            DIAGNOSIS_PROGRESS.setdefault("experts", {})[expert] = {
                "status": status,
                "message": msg[:120],
                "updated_at": now_iso(),
            }
            DIAGNOSIS_PROGRESS["stage"] = "experts"
        if stage in {"report", "complete"}:
            for info in DIAGNOSIS_PROGRESS.setdefault("experts", {}).values():
                if info.get("status") in {"waiting", "consulting"}:
                    info["status"] = "done"
        DIAGNOSIS_PROGRESS["revision"] = int(DIAGNOSIS_PROGRESS.get("revision") or 0) + 1
        DIAGNOSIS_PROGRESS["updated_at"] = now_iso()
        return copy.deepcopy(DIAGNOSIS_PROGRESS)


def get_progress() -> dict[str, Any]:
    with PROGRESS_LOCK:
        return copy.deepcopy(DIAGNOSIS_PROGRESS)


def report_from_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    try:
        meta = json.loads(row["metadata_json"] or "{}")
    except json.JSONDecodeError:
        meta = {}
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "vehicle": row["vehicle"],
        "complaint": row["complaint"],
        "report": row["report"],
        "metadata": meta,
        "work_order": meta.get("work_order") if isinstance(meta, dict) else None,
    }


def save_report(vehicle: str, complaint: str, ai_text: str, metadata: dict[str, Any]) -> dict[str, Any]:
    work_order = normalize_work_order(ai_text, vehicle, complaint)
    merged = dict(metadata or {})
    merged["work_order"] = work_order
    full_report_text = json.dumps(work_order.get("full_report") or {}, ensure_ascii=False, indent=2)
    with DB_LOCK, db_connect() as con:
        cur = con.execute(
            "INSERT INTO reports(created_at,vehicle,complaint,report,metadata_json) VALUES(?,?,?,?,?)",
            (now_iso(), vehicle or VEHICLE_PROFILE["model"], complaint, full_report_text, json.dumps(merged, ensure_ascii=False)),
        )
        con.commit()
        row = con.execute("SELECT * FROM reports WHERE id=?", (cur.lastrowid,)).fetchone()
    result = report_from_row(row)
    assert result is not None
    return result


def list_reports() -> list[dict[str, Any]]:
    with DB_LOCK, db_connect() as con:
        rows = con.execute("SELECT * FROM reports ORDER BY id DESC LIMIT 100").fetchall()
    return [r for row in rows if (r := report_from_row(row))]


def get_report(report_id: int) -> dict[str, Any] | None:
    with DB_LOCK, db_connect() as con:
        return report_from_row(con.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone())


def latest_report() -> dict[str, Any] | None:
    with DB_LOCK, db_connect() as con:
        return report_from_row(con.execute("SELECT * FROM reports ORDER BY id DESC LIMIT 1").fetchone())


def extract_report_text(payload: dict[str, Any]) -> str:
    for key in ("report", "output", "text", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            for nested_key in ("output", "text", "report", "message"):
                nested = value.get(nested_key)
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
    return ""


def safe_video_path_from_name(name: str) -> Path | None:
    filename = os.path.basename(unquote(name))
    path = (REPAIR_VIDEOS_DIR / filename).resolve()
    try:
        path.relative_to(REPAIR_VIDEOS_DIR)
    except ValueError:
        return None
    if not path.exists() or not path.is_file():
        return None
    return path


HTML = r"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VE Diagnostics｜AI 智慧維修派工單</title>
<style>
:root{
  --bg:#071a15;--panel:#0e3128;--line:rgba(123,231,190,.18);
  --mint:#72edbd;--mint2:#c5f8e5;--gold:#f0c66b;--white:#f5fff9;
  --muted:#a3bfb3;--danger:#ff7d6e;--shadow:0 20px 60px rgba(0,0,0,.28);
  --blue:#67c8ff;--purple:#b68bff;--red:#ff8c8c;--orange:#ffb55d;--cyan:#62f5ff;--lime:#a8ff79
}
*{box-sizing:border-box}
html,body{width:100%;height:100%;overflow:hidden}
body{
  margin:0;
  background:radial-gradient(circle at 12% 5%,rgba(114,237,189,.08),transparent 28%),
             linear-gradient(160deg,#061712,#0a211b 60%,#061712);
  color:var(--white);
  font-family:Inter,"Noto Sans TC","Microsoft JhengHei",sans-serif
}
button,input,textarea{font:inherit}
.app-view{display:none;height:100dvh;overflow:hidden;padding:22px 26px}
.app-view.active{display:flex;flex-direction:column}
.page-shell{width:min(1320px,100%);height:100%;min-height:0;margin:0 auto;display:flex;flex-direction:column}
.top{flex:0 0 auto;display:flex;justify-content:space-between;align-items:center;gap:16px;margin-bottom:16px}
.brand h1{margin:0;font-size:26px}
.brand p{margin:5px 0 0;color:var(--muted)}
.status{padding:9px 13px;border:1px solid var(--line);border-radius:999px;background:rgba(255,255,255,.03);color:var(--mint);white-space:nowrap}
.workspace{flex:1;min-height:0;display:grid;grid-template-columns:380px 1fr;gap:18px}
.card{min-height:0;background:linear-gradient(180deg,rgba(18,59,49,.94),rgba(10,39,31,.94));border:1px solid var(--line);border-radius:20px;box-shadow:var(--shadow)}
.pad{padding:20px}
.card h2{margin:0 0 15px;font-size:18px}
.control-card{overflow:auto}
.obd-card{display:flex;flex-direction:column;overflow:hidden}
.obd-head{display:flex;align-items:center;justify-content:space-between;gap:10px;flex:0 0 auto}
.badge{font-size:12px;color:var(--muted)}
.meta{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.meta div{padding:12px;border:1px solid var(--line);border-radius:12px;background:rgba(0,0,0,.12)}
.meta small{display:block;color:var(--muted);font-size:11px}
.meta b{display:block;margin-top:4px;font-size:14px}
.field{margin-top:14px}
.field label{display:block;color:var(--muted);font-size:12px;margin-bottom:7px}
input,textarea{width:100%;border:1px solid var(--line);background:#071d17;color:var(--white);padding:12px 13px;border-radius:12px;outline:none}
textarea{min-height:105px;resize:vertical}
.btn{width:100%;margin-top:14px;border:0;border-radius:13px;padding:13px 16px;font-weight:800;cursor:pointer;background:var(--mint);color:#042219}
.btn:disabled{opacity:.45;cursor:not-allowed}
.btn.secondary{background:transparent;border:1px solid var(--line);color:var(--mint2)}
.btn.danger{background:#4a1717;border:1px solid #a84b4b;color:#ffd8d2}
.btn.danger:hover{background:#612020}
.btn.inline{width:auto;margin:0;padding:10px 15px}
.btn.video-btn{background:var(--mint);color:#042219}
.pid-grid{flex:1;min-height:0;overflow:auto;padding-right:5px;display:grid;grid-template-columns:repeat(auto-fill,minmax(165px,1fr));align-content:start;gap:10px}
.pid{border:1px solid var(--line);background:rgba(0,0,0,.14);border-radius:14px;padding:13px;min-height:92px}
.pid label{display:block;color:var(--muted);font-size:12px}
.pid strong{display:block;margin-top:6px;font-size:21px}
.pid em{font-style:normal;font-size:11px;color:var(--mint);margin-left:4px}
.pid small{display:block;margin-top:4px;color:#668d7d}
.empty{padding:30px;border:1px dashed var(--line);border-radius:14px;color:var(--muted);text-align:center}

.case-manager{margin-top:16px;padding:14px;border:1px solid var(--line);border-radius:16px;background:rgba(0,0,0,.13)}
.case-manager-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:10px}
.case-manager-head b{font-size:14px}
.source-badge{font-size:11px;padding:6px 9px;border-radius:999px;border:1px solid var(--line);color:var(--mint2);white-space:nowrap}
.source-badge.replay{color:#ffe8a6;border-color:rgba(240,198,107,.45);background:rgba(240,198,107,.07)}
.case-subtitle{font-size:11px;color:var(--muted);margin:12px 0 6px}
.case-manager input,.case-manager select{width:100%;border:1px solid var(--line);background:#071d17;color:var(--white);padding:10px 11px;border-radius:10px;outline:none;font-size:13px}
.case-record-grid{display:grid;grid-template-columns:1fr 105px;gap:8px}
.case-actions{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px}
.case-actions.three{grid-template-columns:1fr 1fr 1fr}
.case-actions .btn{margin:0;padding:10px 8px;font-size:12px}
.case-mini{margin-top:8px;font-size:11px;line-height:1.55;color:var(--muted);min-height:18px}
.case-progress{height:5px;margin-top:8px;border-radius:999px;background:#061611;overflow:hidden;display:none}
.case-progress.show{display:block}
.case-progress span{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--mint),var(--cyan));transition:width .25s}
.case-delete{background:#351719!important;color:#ffd9d5!important;border:1px solid rgba(255,125,110,.35)!important}
.case-live{background:transparent!important;color:var(--mint2)!important;border:1px solid var(--line)!important}
@media(max-width:460px){
  .case-record-grid,.case-actions,.case-actions.three{grid-template-columns:1fr}
}

/* V10.2 - LIVE / REPLAY / RECORD 三模式自由切換 */
.mode-nav{
  flex:0 0 auto;display:grid;grid-template-columns:repeat(4,1fr);gap:10px;
  margin:0 0 16px;padding:7px;border:1px solid var(--line);border-radius:17px;
  background:rgba(0,0,0,.14)
}
.mode-tab{
  border:1px solid transparent;border-radius:12px;padding:11px 12px;
  background:transparent;color:var(--muted);cursor:pointer;font-weight:800;
  transition:.18s
}
.mode-tab:hover{color:var(--white);background:rgba(255,255,255,.035)}
.mode-tab.active{
  color:#042219;background:var(--mint);box-shadow:0 8px 24px rgba(114,237,189,.13)
}
.mode-tab small{display:block;margin-top:3px;font-size:10px;font-weight:600;opacity:.72}
.mode-panel{display:none;flex:1;min-height:0}
.mode-panel.active{display:flex;flex-direction:column}
.mode-workspace{
  flex:1;min-height:0;display:grid;grid-template-columns:380px 1fr;gap:18px
}
.mode-card-title{display:flex;align-items:center;justify-content:space-between;gap:10px}
.mode-source{
  font-size:11px;padding:6px 10px;border-radius:999px;border:1px solid var(--line);
  color:var(--mint2);white-space:nowrap
}
.mode-source.replay{color:#ffe8a6;border-color:rgba(240,198,107,.45);background:rgba(240,198,107,.07)}
.mode-source.recording{color:#ffd2c8;border-color:rgba(255,125,110,.42);background:rgba(255,125,110,.07)}
.mode-help{
  margin:10px 0 0;padding:10px 12px;border:1px solid var(--line);border-radius:11px;
  background:rgba(0,0,0,.10);font-size:12px;line-height:1.65;color:var(--muted)
}
.replay-case-box{
  margin-top:14px;padding:14px;border:1px solid var(--line);border-radius:14px;
  background:rgba(0,0,0,.12)
}
.replay-case-box select,.record-control-card select{
  width:100%;border:1px solid var(--line);background:#071d17;color:var(--white);
  padding:11px 12px;border-radius:11px;outline:none
}
.record-layout{
  flex:1;min-height:0;display:grid;grid-template-columns:430px 1fr;gap:18px
}
.record-control-card{overflow:auto}
.record-status-big{
  margin-top:14px;padding:14px;border:1px solid var(--line);border-radius:13px;
  background:rgba(0,0,0,.12)
}
.record-case-list{
  margin-top:12px;display:flex;flex-direction:column;gap:7px
}
.record-case-row{
  padding:10px 11px;border:1px solid var(--line);border-radius:11px;
  background:rgba(0,0,0,.10);font-size:12px;color:var(--muted)
}
.record-case-row b{display:block;color:var(--white);margin-bottom:3px}
.panel-actions{display:grid;grid-template-columns:1fr 1fr;gap:9px}
.panel-actions .btn{margin-top:10px}
.panel-actions.three{grid-template-columns:1fr 1fr 1fr}
.mode-note{
  color:var(--muted);font-size:11px;line-height:1.55;margin-top:8px
}
@media(max-width:980px){
  .mode-workspace,.record-layout{grid-template-columns:1fr}
  .mode-panel{min-height:1000px}
}
@media(max-width:620px){
  .mode-nav{grid-template-columns:1fr}
  .panel-actions,.panel-actions.three{grid-template-columns:1fr}
}


/* OBD SIGNAL - 實體儀表 / HUD ECU 模擬控制台 */
.signal-layout{flex:1;min-height:0;display:grid;grid-template-columns:1.15fr .85fr;gap:18px}
.signal-controls,.signal-monitor{overflow:auto}
.signal-topbar{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px}
.signal-status{display:inline-flex;align-items:center;gap:8px;padding:7px 11px;border-radius:999px;border:1px solid var(--line);font-size:12px;color:var(--muted)}
.signal-status.on{color:var(--mint);border-color:rgba(114,237,189,.5)}
.signal-status::before{content:"";width:9px;height:9px;border-radius:50%;background:currentColor;box-shadow:0 0 10px currentColor}
.signal-actions{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin:10px 0 14px}
.signal-actions .btn{margin:0}
.signal-preset-row{display:grid;grid-template-columns:repeat(5,1fr);gap:7px;margin:10px 0 14px}
.signal-preset{border:1px solid var(--line);background:rgba(0,0,0,.11);color:var(--mint2);padding:9px 6px;border-radius:10px;cursor:pointer;font-size:11px}
.signal-preset:hover{background:rgba(114,237,189,.07)}
.signal-link-toggle{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:11px 12px;border:1px solid var(--line);border-radius:12px;background:rgba(0,0,0,.10);margin-bottom:12px}
.signal-link-toggle label{font-size:12px;color:var(--muted)}
.signal-link-toggle input{width:20px;height:20px;accent-color:var(--mint)}
.signal-control-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.signal-control{padding:13px;border:1px solid var(--line);border-radius:14px;background:rgba(0,0,0,.12)}
.signal-control-head{display:flex;align-items:flex-start;justify-content:space-between;gap:10px}
.signal-control-head label{font-size:12px;color:var(--muted)}
.signal-value{font-size:22px;font-weight:900;color:var(--white);white-space:nowrap}
.signal-value small{font-size:11px;color:var(--mint);font-weight:700;margin-left:4px}
.signal-stepper{display:grid;grid-template-columns:48px 1fr 48px;gap:8px;align-items:center;margin-top:10px}
.signal-stepper button{height:44px;border:1px solid var(--line);border-radius:11px;background:#08251d;color:var(--mint2);font-size:24px;font-weight:900;cursor:pointer}
.signal-stepper button:hover{background:#0d352a}
.signal-stepper input[type="range"]{padding:0;border:0;background:transparent;accent-color:var(--mint)}
.signal-monitor-box{padding:13px;border:1px solid var(--line);border-radius:13px;background:rgba(0,0,0,.12);margin-bottom:10px}
.signal-monitor-box small{display:block;color:var(--muted);margin-bottom:6px}
.signal-monitor-box code{display:block;color:#dffef0;white-space:pre-wrap;word-break:break-all;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;line-height:1.6}
.signal-counts{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin-bottom:10px}
.signal-counts div{padding:12px;border:1px solid var(--line);border-radius:12px;background:rgba(0,0,0,.10)}
.signal-counts small{display:block;color:var(--muted);font-size:11px}
.signal-counts b{display:block;font-size:22px;margin-top:4px}
.signal-warning{padding:11px 12px;border-radius:12px;border:1px solid rgba(240,198,107,.35);background:rgba(240,198,107,.06);color:#ffe8a6;font-size:12px;line-height:1.55}
@media(max-width:980px){.signal-layout{grid-template-columns:1fr}.signal-control-grid{grid-template-columns:1fr 1fr}}
@media(max-width:620px){.signal-control-grid{grid-template-columns:1fr}.signal-preset-row{grid-template-columns:1fr 1fr}.signal-actions{grid-template-columns:1fr}}

.workorder-card{flex:1;min-height:0;display:flex;flex-direction:column;overflow:hidden;padding:20px}
.wo-head{flex:0 0 auto;display:grid;grid-template-columns:1.15fr 1.6fr 1fr 1fr;gap:10px;margin-bottom:6px}
.wo-head>div{padding:13px;border-radius:12px;background:rgba(0,0,0,.16);border:1px solid var(--line)}
.wo-head small{display:block;color:var(--muted);font-size:11px}
.wo-head b{display:block;margin-top:4px}
.items{flex:1;min-height:0;overflow:auto;margin-top:10px;border-top:1px solid var(--line);padding-right:4px}
.item{width:100%;display:grid;grid-template-columns:90px 1fr 100px 24px;align-items:center;gap:12px;background:transparent;color:var(--white);border:0;border-bottom:1px solid var(--line);padding:16px 8px;text-align:left;cursor:pointer}
.item:hover{background:rgba(114,237,189,.05)}
.type{font-size:11px;padding:5px 8px;border:1px solid var(--line);border-radius:999px;text-align:center;color:var(--mint2)}
.priority{font-size:12px;text-align:center}
.priority.urgent,.priority.high{color:var(--danger)}
.priority.recommend{color:var(--gold)}
.item b{display:block;font-size:16px}
.item p{margin:5px 0 0;color:var(--muted);font-size:12px}
.arrow{color:var(--mint);font-size:20px}
.work-footer{flex:0 0 auto;display:flex;gap:10px;padding-top:14px}
.work-footer .btn{margin:0}
.detail-btn{background:var(--gold);color:#211706}
.back-btn{background:transparent!important;border:1px solid var(--line)!important;color:var(--mint2)!important}

/* dialogs */
dialog{width:min(820px,92vw);max-height:86vh;overflow:auto;border:1px solid var(--line);border-radius:18px;background:#0b2820;color:var(--white);box-shadow:var(--shadow);padding:0}
dialog::backdrop{background:rgba(0,10,7,.72);backdrop-filter:blur(5px)}
.dlg-head{position:sticky;top:0;display:flex;justify-content:space-between;align-items:center;padding:18px 20px;background:#0b2820;border-bottom:1px solid var(--line);z-index:2}
.dlg-body{padding:20px}
.close{border:1px solid var(--line);background:transparent;color:var(--white);border-radius:10px;padding:8px 10px;cursor:pointer}
.section{padding:15px 0;border-bottom:1px solid var(--line)}
.section h3{margin:0 0 8px;color:var(--mint)}
.section p{white-space:pre-wrap;line-height:1.7;color:#d9ebe4}
.section ul,.section ol{margin:8px 0 0;padding-left:22px;line-height:1.8}
.action-row{display:flex;gap:12px;flex-wrap:wrap;padding-top:8px}
.video-shell{padding:16px 20px 22px}
.video-shell video{width:100%;max-height:72vh;border-radius:14px;background:#000;display:block}
.video-note{margin-top:10px;color:var(--muted);font-size:13px}

/* creative cinema */
.cinema{position:fixed;inset:0;z-index:30;background:#020d0a;display:none;padding:0;overflow:hidden}
.cinema.show{display:block}
.cin-box{position:absolute;inset:0;width:100vw;height:100dvh;border:0;border-radius:0;background:#020d0a;padding:0;box-shadow:none;overflow:hidden}
.cin-top{
  position:absolute;left:0;right:0;top:26px;z-index:60;
  display:none;justify-content:center;align-items:center;
  margin:0;pointer-events:none;text-align:center
}
.cin-top.show-title{display:flex}
.cin-top h2{
  margin:0;padding:10px 22px;border-radius:999px;
  background:rgba(2,18,13,.46);backdrop-filter:blur(7px);
  border:1px solid rgba(150,255,217,.20);
  font-size:34px;letter-spacing:.04em;
  text-shadow:0 3px 18px rgba(0,0,0,.85)
}
.cin-msg{display:none!important}
.cin-pill{display:none!important}
.cin-pill::before{content:"";width:10px;height:10px;border-radius:50%;background:var(--mint);box-shadow:0 0 12px rgba(114,237,189,.8);animation:statusBlink 1.2s infinite}
@keyframes statusBlink{50%{opacity:.35;transform:scale(.82)}}
.visual-wrap{position:absolute;inset:0;margin:0;border:0;border-radius:0;background:#020d0a;min-height:0;width:100%;height:100%;padding:0;overflow:hidden}
.grid-bg{position:absolute;inset:0;background-image:linear-gradient(rgba(114,237,189,.06) 1px,transparent 1px),linear-gradient(90deg,rgba(114,237,189,.06) 1px,transparent 1px);background-size:26px 26px;mask-image:radial-gradient(circle at center,black 58%,transparent 96%);pointer-events:none}
.stage-visual{display:none;position:absolute;inset:0;width:100%;height:100%}
.stage-visual.active{display:block}
/* Cinema stage MP4 layers. Videos use the existing /repair-video/ streaming endpoint. */
.stage-video-layer{
  position:absolute;inset:0;z-index:20;background:#000;
  opacity:0;pointer-events:none;transition:opacity .18s ease;overflow:hidden
}
.stage-video-layer video{
  position:absolute;
  left:2%;top:2%;
  width:96%;height:96%;
  display:block;
  object-fit:contain;
  object-position:center center;
  background:#000
}
.stage-visual.active.has-stage-video .stage-video-layer{opacity:1}
.stage-fallback{position:absolute;inset:0;transition:opacity .18s ease}
.stage-visual.active.has-stage-video .stage-fallback{opacity:0;pointer-events:none}

/* RAG 第二階段固定使用影片，不再顯示舊 HTML/CSS fallback */
.stage-visual[data-stage="data"].active .stage-video-layer{
  opacity:1 !important;
}
.stage-visual[data-stage="data"].active .stage-fallback{
  opacity:0 !important;
  pointer-events:none !important;
}
.orb{position:absolute;width:190px;height:190px;border-radius:50%;filter:blur(30px);opacity:.18}
.orb.one{background:var(--cyan);top:-40px;left:-20px}
.orb.two{background:var(--purple);right:-10px;bottom:10px}
.cin-progress{position:absolute;left:0;right:0;bottom:0;z-index:70;margin:0;height:6px;border-radius:0;background:rgba(2,18,13,.72);overflow:hidden}
.cin-progress span{display:block;height:100%;width:8%;border-radius:inherit;background:linear-gradient(90deg,var(--mint),var(--cyan));transition:width .35s ease}

/* shared robots */
.ai-core,.hero-robot,.expert-unit{position:absolute;display:flex;flex-direction:column;align-items:center;gap:8px}
.robot-head{width:96px;height:74px;border-radius:24px;background:linear-gradient(180deg,#dbfff4,#7decc0);border:3px solid rgba(255,255,255,.55);position:relative;box-shadow:0 0 22px rgba(114,237,189,.24)}
.robot-head::before{content:"";position:absolute;left:50%;top:-18px;width:5px;height:18px;background:#9cf2d3;transform:translateX(-50%);border-radius:99px}
.robot-head::after{content:"";position:absolute;left:50%;top:-24px;width:13px;height:13px;background:#cffff0;border-radius:50%;transform:translateX(-50%);box-shadow:0 0 14px rgba(197,248,229,.7)}
.robot-face{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;gap:10px}
.robot-face span{width:12px;height:12px;border-radius:50%;background:#0e5f4d;box-shadow:0 0 10px rgba(14,95,77,.45)}
.robot-mouth{position:absolute;left:50%;bottom:14px;width:28px;height:8px;border-radius:999px;background:#0e5f4d;transform:translateX(-50%)}
.robot-body{width:104px;height:90px;border-radius:28px 28px 32px 32px;background:linear-gradient(180deg,#7ad9ff,#4db9dc);border:3px solid rgba(255,255,255,.34);position:relative}
.robot-body::before,.robot-body::after{content:"";position:absolute;top:24px;width:16px;height:42px;border-radius:14px;background:#81d1e9}
.robot-body::before{left:-12px;transform:rotate(14deg)}
.robot-body::after{right:-12px;transform:rotate(-14deg)}
.robot-body .core{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);width:26px;height:26px;border-radius:50%;background:radial-gradient(circle,#ecffff 0,#90ebff 45%,#50a8d5 100%);box-shadow:0 0 16px rgba(103,200,255,.85);animation:coreBeat 1.4s ease-in-out infinite}
@keyframes coreBeat{50%{transform:translate(-50%,-50%) scale(1.12)}}
.robot-leg{display:flex;gap:22px}
.robot-leg span{display:block;width:12px;height:38px;border-radius:999px;background:#89dced}
.hero-robot .robot-head{width:126px;height:96px;border-radius:30px}
.hero-robot .robot-face span{width:16px;height:16px}
.hero-robot .robot-mouth{width:38px;height:10px}
.hero-robot .robot-body{width:144px;height:122px;border-radius:34px}
.hero-robot .robot-body::before,.hero-robot .robot-body::after{top:34px;height:56px;width:18px}
.hero-robot .robot-leg span{height:50px;width:14px}
.hero-robot .robot-body .core{width:34px;height:34px}

/* OBD stage */
.obd-stage{display:grid;grid-template-columns:1.2fr .8fr;align-items:center;height:100%;padding:16px 8px}
.signal-area{position:relative;height:100%;min-height:360px}
.stream-lane{position:absolute;left:2%;right:10%;height:64px;border-radius:999px;background:linear-gradient(90deg,rgba(255,255,255,.02),rgba(103,200,255,.09),rgba(114,237,189,.03));overflow:hidden}
.stream-lane.l1{top:12%}.stream-lane.l2{top:30%}.stream-lane.l3{top:48%}.stream-lane.l4{top:66%}
.stream-lane::before{content:"";position:absolute;inset:0;background:linear-gradient(90deg,transparent,rgba(98,245,255,.28),transparent);animation:laneSweep 2.2s linear infinite}
@keyframes laneSweep{from{transform:translateX(-100%)}to{transform:translateX(100%)}}
.wave-group{position:absolute;left:0;top:50%;transform:translateY(-50%);display:flex;flex-direction:column;gap:20px}
.wave-bars{display:flex;align-items:flex-end;gap:10px;height:94px;margin-left:12px}
.wave-bars span{display:block;width:10px;border-radius:999px;background:linear-gradient(180deg,var(--cyan),rgba(98,245,255,.12));animation:waveBars 1.15s ease-in-out infinite}
.wave-bars span:nth-child(2){animation-delay:.12s}.wave-bars span:nth-child(3){animation-delay:.24s}.wave-bars span:nth-child(4){animation-delay:.36s}.wave-bars span:nth-child(5){animation-delay:.48s}.wave-bars span:nth-child(6){animation-delay:.6s}.wave-bars span:nth-child(7){animation-delay:.72s}
@keyframes waveBars{0%,100%{height:24px;opacity:.35}50%{height:88px;opacity:1}}
.flow-packets{position:absolute;left:8%;right:20%;top:14%;bottom:14%}
.packet{position:absolute;width:16px;height:16px;border-radius:50%;background:var(--cyan);box-shadow:0 0 14px rgba(98,245,255,.8);opacity:0;animation:packetRun 2.5s linear infinite}
.packet.p1{top:8%;animation-delay:0s}.packet.p2{top:26%;animation-delay:.4s}.packet.p3{top:44%;animation-delay:.8s}.packet.p4{top:62%;animation-delay:1.2s}.packet.p5{top:80%;animation-delay:1.6s}
@keyframes packetRun{0%{left:0;opacity:0;transform:scale(.8)}10%{opacity:1}85%{opacity:1}100%{left:100%;opacity:0;transform:scale(1.3)}}
.hero-robot.obd-hero{position:relative;justify-self:end;right:42px;filter:drop-shadow(0 0 26px rgba(114,237,189,.18))}
.robot-receive-ring{position:absolute;left:50%;top:52%;transform:translate(-50%,-50%);width:220px;height:220px;border-radius:50%;border:2px solid rgba(114,237,189,.14);box-shadow:0 0 0 18px rgba(114,237,189,.03),0 0 32px rgba(98,245,255,.12);animation:receivePulse 2.2s ease-in-out infinite}
@keyframes receivePulse{50%{transform:translate(-50%,-50%) scale(1.06)}}
.input-arrow{position:absolute;left:-260px;top:50%;width:220px;height:4px;background:linear-gradient(90deg,transparent,var(--cyan),transparent);transform:translateY(-50%);overflow:visible}
.input-arrow::after{content:"";position:absolute;right:-4px;top:50%;width:18px;height:18px;border-top:4px solid var(--cyan);border-right:4px solid var(--cyan);transform:translateY(-50%) rotate(45deg)}
.input-arrow::before{content:"";position:absolute;inset:0;background:linear-gradient(90deg,transparent,rgba(255,255,255,.9),transparent);animation:arrowFlow 1.2s linear infinite}
@keyframes arrowFlow{from{transform:translateX(-100%)}to{transform:translateX(100%)}}

/* RAG / SQL stage */
.data-stage{position:relative;height:100%}
.manual-cloud{position:absolute;left:50%;top:10%;transform:translateX(-50%);width:min(860px,90%);display:flex;justify-content:center;gap:26px;flex-wrap:wrap}
.doc-node{position:relative;width:138px;height:170px;border-radius:20px;background:linear-gradient(180deg,#fffefc,#d4efe2);border:2px solid rgba(255,255,255,.4);box-shadow:0 10px 26px rgba(0,0,0,.18);padding:18px 16px;animation:floatDoc 2.8s ease-in-out infinite}
.doc-node::before,.doc-node::after{content:"";display:block;height:11px;border-radius:999px;background:rgba(9,47,38,.12);margin-bottom:10px}
.doc-node::after{width:72%}
.doc-node span{display:block;height:8px;border-radius:999px;background:rgba(9,47,38,.08);margin-top:9px}
.doc-node.d2{animation-delay:.25s}.doc-node.d3{animation-delay:.5s}.doc-node.d4{animation-delay:.75s}
@keyframes floatDoc{50%{transform:translateY(-9px)}}
.doc-beam{position:absolute;left:50%;top:34%;width:2px;height:140px;background:linear-gradient(180deg,rgba(98,245,255,0),rgba(98,245,255,.9),rgba(98,245,255,0));transform-origin:center top;animation:beamDown 2s infinite}
.doc-beam.b1{transform:translateX(-180px)}.doc-beam.b2{transform:translateX(-60px);animation-delay:.3s}.doc-beam.b3{transform:translateX(60px);animation-delay:.6s}.doc-beam.b4{transform:translateX(180px);animation-delay:.9s}
@keyframes beamDown{0%{opacity:0;transform:translateY(-20px)}30%{opacity:1}100%{opacity:0;transform:translateY(20px)}}
.rag-focus{position:absolute;left:50%;top:46%;transform:translateX(-50%);width:420px;height:140px;border-radius:50%;background:radial-gradient(circle,rgba(114,237,189,.14),transparent 66%)}
.ai-core.data-core{left:50%;bottom:36px;transform:translateX(-50%)}
.ai-title{font-size:14px;color:#e4faf1;letter-spacing:.08em}
.scan-ring{position:absolute;left:50%;top:62%;transform:translate(-50%,-50%);width:150px;height:150px;border-radius:50%;border:2px solid rgba(98,245,255,.3);animation:ringPulse 1.9s ease-out infinite}
.scan-ring.r2{width:210px;height:210px;animation-delay:.55s}.scan-ring.r3{width:270px;height:270px;animation-delay:1.1s}
@keyframes ringPulse{0%{opacity:0;transform:translate(-50%,-50%) scale(.72)}35%{opacity:.8}100%{opacity:0;transform:translate(-50%,-50%) scale(1.08)}}

/* experts */
.experts-stage{position:relative;height:100%}
.expert-row{position:absolute;left:4%;right:4%;top:12%;display:grid;grid-template-columns:repeat(6,1fr);gap:10px;z-index:2}
.expert-unit{position:relative;align-items:center;justify-self:center;opacity:.28;transition:.28s;--color:var(--blue)}
.expert-unit .mini-head{width:66px;height:54px;border-radius:18px;background:linear-gradient(180deg,#eefdf7,#d8f6ea);border:2px solid rgba(255,255,255,.55);position:relative}
.expert-unit .mini-head::before{content:"";position:absolute;left:50%;top:-12px;width:4px;height:12px;background:#dbfff4;transform:translateX(-50%);border-radius:99px}
.expert-unit .mini-head::after{content:"";position:absolute;left:50%;top:-17px;width:10px;height:10px;background:#e6fff7;border-radius:50%;transform:translateX(-50%)}
.expert-unit .mini-face{position:absolute;inset:0;display:flex;justify-content:center;align-items:center;gap:8px}
.expert-unit .mini-face span{width:8px;height:8px;border-radius:50%;background:#144a3f}
.expert-unit .mini-body{width:74px;height:62px;border-radius:22px;background:linear-gradient(180deg,var(--color),rgba(255,255,255,.18));border:2px solid rgba(255,255,255,.26);position:relative}
.expert-unit .mini-body::after{content:"";position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);width:22px;height:22px;border-radius:50%;background:radial-gradient(circle,#fff 0,var(--color) 60%,rgba(255,255,255,.15) 100%);box-shadow:0 0 14px color-mix(in srgb,var(--color) 70%,white)}
.expert-unit .tag{font-size:12px;color:#ddf7ee;text-align:center;margin-top:2px}
.expert-unit .expert-check{position:absolute;right:-4px;top:-2px;width:26px;height:26px;border-radius:50%;background:rgba(114,237,189,.16);border:1px solid rgba(114,237,189,.45);display:grid;place-items:center;color:#dffef0;font-size:15px;opacity:0;transform:scale(.7);transition:.25s}
.expert-unit .expert-check.show{opacity:1;transform:scale(1)}
.expert-unit.active{opacity:1;filter:drop-shadow(0 0 16px color-mix(in srgb,var(--color) 65%,white))}
.expert-unit.active .mini-body::after{animation:nodeBlink 1s infinite}
.expert-unit.done{opacity:1}
.expert-unit.done .mini-body::after{box-shadow:0 0 18px rgba(114,237,189,.8)}
.expert-unit.error{opacity:1;filter:drop-shadow(0 0 12px rgba(255,125,110,.35))}
@keyframes nodeBlink{50%{transform:translate(-50%,-50%) scale(1.14);box-shadow:0 0 20px color-mix(in srgb,var(--color) 80%,white)}}
.expert-unit[data-expert="powertrain"]{--color:var(--blue)}
.expert-unit[data-expert="electrical"]{--color:var(--purple)}
.expert-unit[data-expert="brake"]{--color:var(--red)}
.expert-unit[data-expert="chassis"]{--color:var(--orange)}
.expert-unit[data-expert="cooling"]{--color:var(--cyan)}
.expert-unit[data-expert="safety"]{--color:var(--lime)}
.consult-svg{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;z-index:1}
.consult-line{stroke:rgba(255,255,255,.12);stroke-width:2.6;stroke-dasharray:10 10}
.consult-line.active{stroke:var(--gold);stroke-width:4;filter:drop-shadow(0 0 9px rgba(240,198,107,.8));animation:dashMove 1.1s linear infinite}
.consult-line.done{stroke:var(--mint);stroke-width:3.2;filter:drop-shadow(0 0 8px rgba(114,237,189,.5))}
@keyframes dashMove{to{stroke-dashoffset:-34}}
.hub-glow{position:absolute;left:50%;bottom:80px;transform:translateX(-50%);width:380px;height:220px;border-radius:50%;background:radial-gradient(circle,rgba(114,237,189,.12),transparent 70%)}
.ai-core.expert-core{left:50%;bottom:32px;transform:translateX(-50%);z-index:2}

/* report */
.report-stage{position:relative;height:100%}
.report-board{position:absolute;right:10%;top:14%;width:290px;height:340px;border-radius:24px;background:linear-gradient(180deg,#fdfbf4,#daeede);border:3px solid rgba(255,255,255,.45);box-shadow:0 12px 24px rgba(0,0,0,.22)}
.report-board::before{content:"";position:absolute;left:50%;top:-20px;transform:translateX(-50%);width:130px;height:38px;border-radius:18px;background:#7decc0}
.report-line{position:absolute;left:28px;right:28px;height:8px;border-radius:999px;background:rgba(7,26,21,.1)}
.report-line.l1{top:72px;width:58%}.report-line.l2{top:102px}.report-line.l3{top:136px;width:74%}.report-line.l4{top:170px}.report-line.l5{top:204px;width:65%}.report-line.l6{top:238px;width:78%}
.report-check{position:absolute;width:22px;height:22px;border:3px solid #56bf98;border-top:0;border-right:0;transform:rotate(-45deg);opacity:0;animation:tickIn 1.8s infinite}
.report-check.c1{left:216px;top:91px}.report-check.c2{left:216px;top:159px;animation-delay:.5s}.report-check.c3{left:216px;top:227px;animation-delay:1s}
@keyframes tickIn{20%{opacity:0;transform:rotate(-45deg) scale(.6)}45%{opacity:1;transform:rotate(-45deg) scale(1)}100%{opacity:1}}
.ai-core.report-core{left:22%;bottom:36px;transform:none}
.writer-arm{position:absolute;left:34%;top:59%;width:145px;height:20px;background:linear-gradient(90deg,#74d1e7,#bdeef6);border-radius:14px;transform-origin:left center;animation:writeArm 1.7s ease-in-out infinite}
.writer-pen{position:absolute;right:-18px;top:1px;width:30px;height:18px;border-radius:4px;background:linear-gradient(90deg,#ffe39c,#ffb85d);transform:rotate(8deg)}
@keyframes writeArm{0%,100%{transform:rotate(8deg)}50%{transform:rotate(-8deg) translateY(8px)}}
.scribble{position:absolute;left:44%;top:51%;width:170px;height:96px;border-radius:12px;overflow:hidden;opacity:.4}
.scribble span{display:block;height:8px;margin:12px 0;background:linear-gradient(90deg,rgba(10,44,36,0),rgba(10,44,36,.35),rgba(10,44,36,0));animation:scribble 1.7s linear infinite}
.scribble span:nth-child(2){animation-delay:.2s}.scribble span:nth-child(3){animation-delay:.4s}.scribble span:nth-child(4){animation-delay:.6s}
@keyframes scribble{from{transform:translateX(-35%)}to{transform:translateX(35%)}}

@media(max-width:980px){
  html,body{overflow:auto}
  .app-view{height:auto;min-height:100dvh;overflow:visible;padding:16px}
  .workspace{grid-template-columns:1fr;display:grid}
  .obd-card{min-height:520px}
  .pid-grid{max-height:520px}
  .wo-head{grid-template-columns:1fr 1fr}
  .workorder-card{min-height:calc(100dvh - 110px)}
  .visual-wrap{min-height:620px}
  .obd-stage{grid-template-columns:1fr;gap:12px}
  .hero-robot.obd-hero{justify-self:center;right:0}
  .input-arrow{display:none}
  .manual-cloud{gap:14px}
  .doc-node{width:120px;height:154px}
  .expert-row{grid-template-columns:repeat(3,1fr);top:8%;row-gap:18px}
  .report-board{right:50%;transform:translateX(50%);top:16%}
  .ai-core.report-core{left:50%;transform:translateX(-50%);bottom:28px}
  .writer-arm{left:50%;top:74%;transform-origin:center center}
  .scribble{left:50%;top:68%;transform:translateX(-10%)}
  .item{grid-template-columns:78px 1fr 80px 18px}
}

/* V9.4 fullscreen stage sizing */
.stage-visual[data-stage="experts"] .experts-stage{
  position:absolute;
  left:50%;
  top:50%;
  width:min(1500px,94vw);
  height:min(820px,82vh);
  transform:translate(-50%,-48%);
}
.stage-visual[data-stage="report"] .report-stage{
  position:absolute;
  left:50%;
  top:50%;
  width:min(1400px,94vw);
  height:min(820px,82vh);
  transform:translate(-50%,-48%);
}
.stage-visual[data-stage="obd"] .obd-stage,
.stage-visual[data-stage="data"] .data-stage{
  width:100%;
  height:100%;
}
@media(max-width:900px){
  .cin-top{left:0;right:0;top:18px}
  .cin-top h2{font-size:26px}
  .cin-msg{font-size:14px}
  .stage-video-layer video{object-fit:contain}
}

.toast{position:fixed;right:22px;bottom:22px;padding:12px 16px;border-radius:12px;background:#123b31;border:1px solid var(--line);display:none;z-index:50}
.toast.show{display:block}
.toast.error{border-color:var(--danger);color:#ffd5cf}
@media(max-width:900px){
  html,body{overflow:auto}
  .app-view{height:auto;min-height:100dvh;overflow:visible;padding:16px}
  .workspace{grid-template-columns:1fr;display:grid}
  .obd-card{min-height:520px}
  .pid-grid{max-height:520px}
  .wo-head{grid-template-columns:1fr 1fr}
  .workorder-card{min-height:calc(100dvh - 110px)}
  .visual-wrap{min-height:560px}
  .stage-dots{grid-template-columns:1fr 1fr}
  .small-experts{grid-template-columns:repeat(3,1fr)}
  .expert-visual[data-expert="powertrain"]{left:4%;top:8%}
  .expert-visual[data-expert="electrical"]{left:27%;top:1%}
  .expert-visual[data-expert="brake"]{right:27%;top:1%}
  .expert-visual[data-expert="chassis"]{right:4%;top:8%}
  .expert-visual[data-expert="cooling"]{left:13%;bottom:17%}
  .expert-visual[data-expert="safety"]{right:13%;bottom:17%}
  .item{grid-template-columns:78px 1fr 80px 18px}
}
</style>
</head>
<body>
<div id="diagnosis-view" class="app-view active">
  <div class="page-shell">
    <div class="top">
      <div class="brand">
        <h1>VE Diagnostics｜AI 智慧車輛診斷</h1>
        <p>OBD 即時資料 × RAG × 多專家 AI × SQL 車輛歷史資料</p>
      </div>
      <div id="obd-status" class="status">OBD 讀取中</div>
    </div>

    <div class="mode-nav" aria-label="診斷模式切換">
      <button class="mode-tab active" data-mode="live">即時診斷<small>LIVE 實車</small></button>
      <button class="mode-tab" data-mode="replay">案例回放<small>REPLAY 錄製資料</small></button>
      <button class="mode-tab" data-mode="record">實車錄製<small>RECORD 建立案例</small></button>
      <button class="mode-tab" data-mode="signal">訊號控制<small>OBD SIGNAL 儀表</small></button>
    </div>

    <!-- LIVE -->
    <section id="mode-live" class="mode-panel active" data-mode-panel="live">
      <div class="mode-workspace">
        <section class="card pad control-card">
          <div class="mode-card-title">
            <h2>即時實車診斷</h2>
            <span id="live-source-badge" class="mode-source">● LIVE</span>
          </div>
          <div class="meta">
            <div><small>車型</small><b id="car-model">2009 Honda Civic 1.8L</b></div>
            <div><small>引擎</small><b id="car-engine">R18A 1.8L</b></div>
          </div>
          <div class="mode-help">這一頁永遠使用目前實車 OBD。從 REPLAY 切回這一頁時，系統會自動切回 LIVE。</div>
          <div class="field"><label>目前公里數（必填）</label><input id="live-odometer" inputmode="numeric" placeholder="例如 105230"></div>
          <div class="field"><label>車主症狀 / 本次需求</label><textarea id="live-complaint" placeholder="例如：怠速抖動、加速無力；或只輸入『例行檢查』"></textarea></div>
          <button id="diagnose-live" class="btn">開始 LIVE AI 診斷</button>
          <button id="refresh-obd-live" class="btn secondary">重新讀取 OBD</button>
          <button id="clear-dtc" class="btn danger">清除故障碼</button>
        </section>
        <section class="card pad obd-card">
          <div class="obd-head"><h2>LIVE OBD 即時資料</h2><span id="pid-count-live" class="badge">0 項</span></div>
          <div id="pid-grid-live" class="pid-grid"><div class="empty">正在等待實車 OBD 資料</div></div>
        </section>
      </div>
    </section>

    <!-- REPLAY -->
    <section id="mode-replay" class="mode-panel" data-mode-panel="replay">
      <div class="mode-workspace">
        <section class="card pad control-card">
          <div class="mode-card-title">
            <h2>錄製案例回放</h2>
            <span id="case-mode-badge" class="mode-source replay">REPLAY 未啟用</span>
          </div>

          <div class="replay-case-box">
            <label class="case-subtitle" style="display:block;margin-top:0">選擇錄製案例</label>
            <select id="case-select">
              <option value="">目前沒有案例</option>
            </select>
            <div id="selected-case-info" class="case-mini">選擇案例後可重複送入 AI 診斷。</div>
            <div class="panel-actions three">
              <button id="case-use" class="btn secondary">▶ 使用此案例</button>
              <button id="case-live" class="btn case-live">↩ 停止 REPLAY</button>
              <button id="case-delete" class="btn case-delete">刪除案例</button>
            </div>
          </div>

          <div class="mode-help">使用案例後，AI 每次取得 OBD Window 都會從該案例開頭重新回放，方便重複做相同實驗。</div>
          <div class="field"><label>案例公里數（必填）</label><input id="replay-odometer" inputmode="numeric" placeholder="選案例後自動帶入"></div>
          <div class="field"><label>診斷需求 / 症狀</label><textarea id="replay-complaint" placeholder="選案例後會帶入原本症狀，也可以自行補充"></textarea></div>
          <button id="diagnose-replay" class="btn">開始 REPLAY AI 診斷</button>
          <button id="refresh-obd-replay" class="btn secondary">重新讀取案例 OBD</button>
        </section>
        <section class="card pad obd-card">
          <div class="obd-head"><h2>REPLAY 案例 OBD 資料</h2><span id="pid-count-replay" class="badge">0 項</span></div>
          <div id="pid-grid-replay" class="pid-grid"><div class="empty">請先選擇並啟用一個案例</div></div>
        </section>
      </div>
    </section>

    <!-- RECORD -->
    <section id="mode-record" class="mode-panel" data-mode-panel="record">
      <div class="record-layout">
        <section class="card pad record-control-card">
          <div class="mode-card-title">
            <h2>實車案例錄製</h2>
            <span id="record-source-badge" class="mode-source">● LIVE</span>
          </div>
          <div class="mode-help">錄製頁固定使用實車 CAN / OBD，不會把 REPLAY 資料再次錄成新案例。錄製開始後即使切換頁面，錄製仍會繼續到完成或手動停止。</div>

          <div class="field"><label>案例名稱</label><input id="record-case-name" placeholder="例如 P0354 怠速抖動"></div>
          <div class="field"><label>目前公里數</label><input id="record-odometer" inputmode="numeric" placeholder="例如 60000"></div>
          <div class="field"><label>車輛症狀 / 現象</label><textarea id="record-symptom" placeholder="例如：暖車怠速偶爾抖動"></textarea></div>
          <div class="field"><label>錄製條件</label><input id="record-condition" placeholder="例如 暖車怠速 / 冷氣 ON / 2000 RPM"></div>
          <div class="field"><label>錄製時間</label>
            <select id="record-seconds">
              <option value="10">10 秒</option>
              <option value="30" selected>30 秒</option>
              <option value="60">60 秒</option>
              <option value="120">120 秒</option>
            </select>
          </div>

          <div class="panel-actions">
            <button id="record-start" class="btn">● 開始錄製</button>
            <button id="record-stop" class="btn secondary" disabled>■ 停止錄製</button>
          </div>

          <div class="record-status-big">
            <div id="record-status" class="case-mini" style="margin-top:0">等待錄製</div>
            <div id="record-progress" class="case-progress"><span></span></div>
          </div>

          <div class="case-subtitle">已錄製案例</div>
          <div id="record-case-list" class="record-case-list">
            <div class="record-case-row">目前沒有案例</div>
          </div>
        </section>

        <section class="card pad obd-card">
          <div class="obd-head"><h2>錄製中的 LIVE OBD</h2><span id="pid-count-record" class="badge">0 項</span></div>
          <div id="pid-grid-record" class="pid-grid"><div class="empty">正在等待實車 OBD 資料</div></div>
        </section>
      </div>
    </section>

    <!-- OBD SIGNAL -->
    <section id="mode-signal" class="mode-panel" data-mode-panel="signal">
      <div class="signal-layout">
        <section class="card pad signal-controls">
          <div class="signal-topbar">
            <div>
              <h2 style="margin-bottom:5px">OBD SIGNAL｜實體儀表控制台</h2>
              <div class="badge">網站設定數值 → Raspberry Pi ECU 模擬 → Kvaser / SocketCAN → 外接 OBD HUD</div>
            </div>
            <span id="signal-status" class="signal-status">停止中</span>
          </div>

          <div class="signal-actions">
            <button id="signal-start" class="btn">▶ 開始送出 OBD 回覆</button>
            <button id="signal-stop" class="btn secondary">■ 停止 OBD 回覆</button>
          </div>

          <div class="signal-link-toggle">
            <div>
              <b>油門連動模式</b>
              <label>調整油門時，同步帶動 RPM、車速、引擎負載與 MAF，適合現場展示。</label>
            </div>
            <input id="signal-linked" type="checkbox" checked>
          </div>

          <div class="signal-preset-row">
            <button class="signal-preset" data-preset="idle">怠速</button>
            <button class="signal-preset" data-preset="city">市區</button>
            <button class="signal-preset" data-preset="accel">加速</button>
            <button class="signal-preset" data-preset="highway">高速</button>
            <button class="signal-preset" data-preset="hot">過熱</button>
          </div>

          <div id="signal-control-grid" class="signal-control-grid">
            <div class="signal-control" data-key="throttle" data-step="5" data-min="0" data-max="100">
              <div class="signal-control-head"><label>油門開度 / THROTTLE</label><div class="signal-value"><span>12</span><small>%</small></div></div>
              <div class="signal-stepper"><button type="button" data-dir="-1">−</button><input type="range" min="0" max="100" step="1" value="12"><button type="button" data-dir="1">＋</button></div>
            </div>
            <div class="signal-control" data-key="rpm" data-step="250" data-min="0" data-max="8000">
              <div class="signal-control-head"><label>引擎轉速 / RPM</label><div class="signal-value"><span>800</span><small>rpm</small></div></div>
              <div class="signal-stepper"><button type="button" data-dir="-1">−</button><input type="range" min="0" max="8000" step="50" value="800"><button type="button" data-dir="1">＋</button></div>
            </div>
            <div class="signal-control" data-key="speed" data-step="5" data-min="0" data-max="220">
              <div class="signal-control-head"><label>車速 / SPEED</label><div class="signal-value"><span>0</span><small>km/h</small></div></div>
              <div class="signal-stepper"><button type="button" data-dir="-1">−</button><input type="range" min="0" max="220" step="1" value="0"><button type="button" data-dir="1">＋</button></div>
            </div>
            <div class="signal-control" data-key="coolant" data-step="5" data-min="-40" data-max="150">
              <div class="signal-control-head"><label>冷卻水溫 / COOLANT</label><div class="signal-value"><span>90</span><small>°C</small></div></div>
              <div class="signal-stepper"><button type="button" data-dir="-1">−</button><input type="range" min="-40" max="150" step="1" value="90"><button type="button" data-dir="1">＋</button></div>
            </div>
            <div class="signal-control" data-key="load" data-step="5" data-min="0" data-max="100">
              <div class="signal-control-head"><label>引擎負載 / LOAD</label><div class="signal-value"><span>18</span><small>%</small></div></div>
              <div class="signal-stepper"><button type="button" data-dir="-1">−</button><input type="range" min="0" max="100" step="1" value="18"><button type="button" data-dir="1">＋</button></div>
            </div>
            <div class="signal-control" data-key="iat" data-step="5" data-min="-40" data-max="120">
              <div class="signal-control-head"><label>進氣溫度 / IAT</label><div class="signal-value"><span>30</span><small>°C</small></div></div>
              <div class="signal-stepper"><button type="button" data-dir="-1">−</button><input type="range" min="-40" max="120" step="1" value="30"><button type="button" data-dir="1">＋</button></div>
            </div>
            <div class="signal-control" data-key="maf" data-step="1" data-min="0" data-max="200">
              <div class="signal-control-head"><label>空氣流量 / MAF</label><div class="signal-value"><span>3.5</span><small>g/s</small></div></div>
              <div class="signal-stepper"><button type="button" data-dir="-1">−</button><input type="range" min="0" max="200" step="0.1" value="3.5"><button type="button" data-dir="1">＋</button></div>
            </div>
          </div>
        </section>

        <section class="card pad signal-monitor">
          <h2>OBD / CAN 即時監看</h2>
          <div class="signal-counts">
            <div><small>HUD OBD Requests</small><b id="signal-request-count">0</b></div>
            <div><small>Pi ECU Responses</small><b id="signal-response-count">0</b></div>
          </div>
          <div class="signal-monitor-box"><small>CAN 介面</small><code id="signal-interface">can0</code></div>
          <div class="signal-monitor-box"><small>最近收到的 OBD Request</small><code id="signal-last-request">尚未收到 HUD 查詢</code></div>
          <div class="signal-monitor-box"><small>最近送出的 ECU Response</small><code id="signal-last-response">尚未送出回覆</code></div>
          <div class="signal-monitor-box"><small>狀態 / 錯誤</small><code id="signal-error">等待啟動</code></div>
          <div class="signal-warning">桌上展示用 ECU 模擬模式。外接 HUD 需由 OBD 接頭 Pin 16 提供 12V、Pin 4/5 接地，CAN-H / CAN-L 接至測試 CAN 匯流排。請勿在實車 ECU 同時連線時啟用 ECU 模擬。</div>
        </section>
      </div>
    </section>
  </div>
</div>

<div id="workorder-view" class="app-view">
  <div class="page-shell">
    <div class="top">
      <div class="brand">
        <h1>維修派工單</h1>
        <p>AI 診斷完成｜點選派工項目查看技術內容</p>
      </div>
      <button id="back-home" class="btn inline back-btn">返回診斷首頁</button>
    </div>
    <section class="card workorder-card">
      <div class="wo-head">
        <div><small>派工單號</small><b id="wo-number">—</b></div>
        <div><small>車種</small><b id="wo-vehicle">—</b></div>
        <div><small>公里數</small><b id="wo-mileage">—</b></div>
        <div><small>故障碼</small><b id="wo-dtcs">—</b></div>
      </div>
      <div id="work-items" class="items"></div>
      <div class="work-footer">
        <button id="full-report-button" class="btn detail-btn">點我看詳細報告</button>
      </div>
    </section>
  </div>
</div>

<div id="cinema" class="cinema" aria-hidden="true">
  <div class="cin-box">
    <div class="cin-top">
      <div>
        <h2 id="cin-title">AI 診斷進行中</h2>
        <p id="cin-msg" class="cin-msg">正在讀取 OBD-II 資料</p>
      </div>
      <div class="cin-pill" id="cin-stage-pill">系統處理中</div>
    </div>

    <div class="visual-wrap">
      <div class="grid-bg"></div>
      <div class="orb one"></div>
      <div class="orb two"></div>

      <div class="stage-visual active" data-stage="obd">
        <div class="stage-video-layer" aria-hidden="true">
          <video id="obd-stage-video" playsinline preload="auto" src="/repair-video/obd_reading.mp4"></video>
        </div>
        <div class="obd-stage stage-fallback">
          <div class="signal-area">
            <div class="wave-group">
              <div class="wave-bars"><span></span><span></span><span></span><span></span><span></span><span></span><span></span></div>
              <div class="wave-bars"><span></span><span></span><span></span><span></span><span></span><span></span><span></span></div>
            </div>
            <div class="stream-lane l1"></div>
            <div class="stream-lane l2"></div>
            <div class="stream-lane l3"></div>
            <div class="stream-lane l4"></div>
            <div class="flow-packets">
              <div class="packet p1"></div><div class="packet p2"></div><div class="packet p3"></div><div class="packet p4"></div><div class="packet p5"></div>
            </div>
          </div>
          <div class="hero-robot obd-hero">
            <div class="robot-receive-ring"></div>
            <div class="input-arrow"></div>
            <div class="robot-head"><div class="robot-face"><span></span><span></span></div><div class="robot-mouth"></div></div>
            <div class="robot-body"><div class="core"></div></div>
            <div class="robot-leg"><span></span><span></span></div>
          </div>
        </div>
      </div>

      <div class="stage-visual" data-stage="data">
        <div class="stage-video-layer" aria-hidden="true">
          <video id="rag-stage-video" playsinline preload="auto" loop src="/repair-video/rag_loop.mp4"></video>
        </div>
        <div class="data-stage stage-fallback">
          <div class="manual-cloud">
            <div class="doc-node d1"><span></span><span></span><span></span></div>
            <div class="doc-node d2"><span></span><span></span><span></span></div>
            <div class="doc-node d3"><span></span><span></span><span></span></div>
            <div class="doc-node d4"><span></span><span></span><span></span></div>
          </div>
          <div class="doc-beam b1"></div><div class="doc-beam b2"></div><div class="doc-beam b3"></div><div class="doc-beam b4"></div>
          <div class="rag-focus"></div>
          <div class="scan-ring"></div><div class="scan-ring r2"></div><div class="scan-ring r3"></div>
          <div class="ai-core data-core">
            <div class="robot-head"><div class="robot-face"><span></span><span></span></div><div class="robot-mouth"></div></div>
            <div class="robot-body"><div class="core"></div></div>
            <div class="robot-leg"><span></span><span></span></div>
            <div class="ai-title">AI 總監</div>
          </div>
        </div>
      </div>

      <div class="stage-visual" data-stage="experts">
        <div class="experts-stage">
          <div class="hub-glow"></div>
          <svg class="consult-svg" viewBox="0 0 1000 500" preserveAspectRatio="none">
            <line class="consult-line" data-expert="powertrain" x1="500" y1="398" x2="105" y2="168"></line>
            <line class="consult-line" data-expert="electrical" x1="500" y1="398" x2="265" y2="168"></line>
            <line class="consult-line" data-expert="brake" x1="500" y1="398" x2="425" y2="168"></line>
            <line class="consult-line" data-expert="chassis" x1="500" y1="398" x2="585" y2="168"></line>
            <line class="consult-line" data-expert="cooling" x1="500" y1="398" x2="745" y2="168"></line>
            <line class="consult-line" data-expert="safety" x1="500" y1="398" x2="905" y2="168"></line>
          </svg>
          <div class="expert-row">
            <div class="expert-unit" data-expert="powertrain"><div class="expert-check">✓</div><div class="mini-head"><div class="mini-face"><span></span><span></span></div></div><div class="mini-body"></div><div class="tag">動力</div></div>
            <div class="expert-unit" data-expert="electrical"><div class="expert-check">✓</div><div class="mini-head"><div class="mini-face"><span></span><span></span></div></div><div class="mini-body"></div><div class="tag">電氣</div></div>
            <div class="expert-unit" data-expert="brake"><div class="expert-check">✓</div><div class="mini-head"><div class="mini-face"><span></span><span></span></div></div><div class="mini-body"></div><div class="tag">煞車</div></div>
            <div class="expert-unit" data-expert="chassis"><div class="expert-check">✓</div><div class="mini-head"><div class="mini-face"><span></span><span></span></div></div><div class="mini-body"></div><div class="tag">底盤</div></div>
            <div class="expert-unit" data-expert="cooling"><div class="expert-check">✓</div><div class="mini-head"><div class="mini-face"><span></span><span></span></div></div><div class="mini-body"></div><div class="tag">冷卻空調</div></div>
            <div class="expert-unit" data-expert="safety"><div class="expert-check">✓</div><div class="mini-head"><div class="mini-face"><span></span><span></span></div></div><div class="mini-body"></div><div class="tag">安全</div></div>
          </div>
          <div class="ai-core expert-core">
            <div class="robot-head"><div class="robot-face"><span></span><span></span></div><div class="robot-mouth"></div></div>
            <div class="robot-body"><div class="core"></div></div>
            <div class="robot-leg"><span></span><span></span></div>
            <div class="ai-title">AI 總監</div>
          </div>
        </div>
      </div>

      <div class="stage-visual" data-stage="report">
        <div class="report-stage">
          <div class="report-board">
            <div class="report-line l1"></div><div class="report-line l2"></div><div class="report-line l3"></div>
            <div class="report-line l4"></div><div class="report-line l5"></div><div class="report-line l6"></div>
            <div class="report-check c1"></div><div class="report-check c2"></div><div class="report-check c3"></div>
          </div>
          <div class="scan-ring"></div><div class="scan-ring r2"></div>
          <div class="ai-core report-core">
            <div class="robot-head"><div class="robot-face"><span></span><span></span></div><div class="robot-mouth"></div></div>
            <div class="robot-body"><div class="core"></div></div>
            <div class="robot-leg"><span></span><span></span></div>
            <div class="ai-title">AI 總監</div>
          </div>
          <div class="writer-arm"><div class="writer-pen"></div></div>
          <div class="scribble"><span></span><span></span><span></span><span></span></div>
        </div>
      </div>
    </div>

    <div class="cin-progress"><span id="cin-progress-bar"></span></div>
  </div>
</div>

<dialog id="item-dialog">
  <div class="dlg-head"><b id="item-title">項目詳細內容</b><button class="close" data-close="item-dialog">關閉</button></div>
  <div id="item-detail" class="dlg-body"></div>
</dialog>

<dialog id="report-dialog">
  <div class="dlg-head"><b>完整 AI 診斷報告</b><button class="close" data-close="report-dialog">關閉</button></div>
  <div id="report-detail" class="dlg-body"></div>
</dialog>

<dialog id="video-dialog">
  <div class="dlg-head"><b id="video-title">維修動畫</b><button class="close" data-close="video-dialog">關閉</button></div>
  <div class="video-shell">
    <video id="repair-video-player" controls preload="metadata"></video>
    <div class="video-note">若影片無法播放，請先確認 repair_videos 資料夾內有對應 mp4 檔案。</div>
  </div>
</dialog>

<div id="toast" class="toast"></div>

<script>
const $=s=>document.querySelector(s);
const $$=s=>Array.from(document.querySelectorAll(s));
const state={
  obd:null,report:null,caseId:'',diagnosing:false,revision:0,
  obdVideoStarted:false,obdVideoFinished:false,obdFallbackTimer:null,
  latestProgressStage:'obd',latestExperts:{},latestProgressMessage:'',
  obdMode:'live',activeReplay:null,cases:[],recording:false,uiMode:'live',
  signalValues:{throttle:12,rpm:800,speed:0,coolant:90,load:18,iat:30,maf:3.5},
  signalActive:false
};
const RAG_STAGE_MESSAGE='正在查詢相關故障與維修資訊';
const REPAIR_VIDEO_MAP={
  'P0351':'case01.mp4',
  'P0113':'case02.mp4',
  'P0122':'case03.mp4',
  'P0504':'case04.mp4',
  'P0171':'case05.mp4',
  'P0341':'case06.mp4'
};

function esc(v){return String(v==null?'':v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}

function displayText(v){
  let s=String(v==null?'':v);

  // 只處理網站顯示，不改 n8n / 專家節點真正名稱與原始資料。
  const replacements=[
    [/\bpowertrain_expert1?\b/gi,'動力系統專家'],
    [/\belectrical_expert1?\b/gi,'電氣系統專家'],
    [/\bbrake_expert1?\b/gi,'煞車系統專家'],
    [/\bchassis_expert1?\b/gi,'底盤系統專家'],
    [/\bcooling_hvac_expert1?\b/gi,'冷卻空調專家'],
    [/\bsafety_expert1?\b/gi,'行車安全專家'],
    [/\bdiagnostic_supervisor\b/gi,'AI 總監'],
    [/\bvehicle_history_search\b/gi,'車輛歷史資料庫查詢']
  ];
  replacements.forEach(([pattern,label])=>{s=s.replace(pattern,label)});

  // 清理 Gemma / 其他模型偶爾直接輸出的 LaTeX / 英文標記。
  s=s
    .replace(/\$?\s*\\+rightarrow\s*\$?/gi,'→')
    .replace(/\$?\s*\\+Rightarrow\s*\$?/g,'⇒')
    .replace(/\$?\s*\\+to\s*\$?/gi,'→')
    .replace(/\\\(|\\\)/g,'')
    .replace(/\(\s*simulated\s*\)/gi,'（模擬資料）')
    .replace(/\bsimulated\b/gi,'模擬資料');

  return s;
}
function toast(msg,error=false){const e=$('#toast');e.textContent=msg;e.className='toast show'+(error?' error':'');clearTimeout(e._t);e._t=setTimeout(()=>e.className='toast',3500)}
async function api(url,opt={}){const c=new AbortController(),t=setTimeout(()=>c.abort(),1810000);try{const r=await fetch(url,{...opt,signal:c.signal});const d=await r.json();if(!r.ok)throw new Error(d.error||d.detail||('HTTP '+r.status));return d}finally{clearTimeout(t)}}
function showView(id){document.querySelectorAll('.app-view').forEach(v=>v.classList.remove('active'));const target=document.getElementById(id);if(target)target.classList.add('active')}
function valueEntries(d){return Object.entries((d&&d.values)||{}).map(([key,x])=>{if(x&&typeof x==='object')return {key,label:x.label||key,value:x.value==null?'—':x.value,unit:x.unit||'',pid:x.pid||''};return {key,label:key,value:x,unit:'',pid:''}})}
function renderObd(d){
  state.obd=d;
  const vi=d.vehicle_info||{};
  const items=valueEntries(d);
  const replay=Boolean(d.replay)||d.data_mode==='replay'||d.capture_source==='recorded_real_vehicle_replay'||state.obdMode==='replay';

  const carModel=$('#car-model'),carEngine=$('#car-engine');
  if(carModel)carModel.textContent=vi.model||'2009 Honda Civic 1.8L';
  if(carEngine)carEngine.textContent=vi.engine||'R18A 1.8L';

  ['live','replay','record'].forEach(mode=>{
    const count=$('#pid-count-'+mode);
    if(count)count.textContent=items.length+' 項';
    const g=$('#pid-grid-'+mode);
    if(!g)return;
    const wrongSource=(mode==='replay'&&!replay)||(mode!=='replay'&&replay);
    if(wrongSource){
      g.innerHTML=`<div class="empty">${mode==='replay'?'請先選擇並啟用一個案例':'目前資料來源是 REPLAY，切換到此頁時會自動回到 LIVE'}</div>`;
      return;
    }
    g.innerHTML=items.length
      ?items.map(x=>`<div class="pid"><label>${esc(x.label)}</label><strong>${esc(x.value)}<em>${esc(x.unit)}</em></strong><small>${esc(x.pid)}</small></div>`).join('')
      :`<div class="empty">${esc(d.error||'目前沒有讀到 PID')}</div>`;
  });

  const status=$('#obd-status');
  if(status){
    if(replay){
      status.textContent='● REPLAY '+(state.activeReplay?.case_name||d.replay_info?.case_name||'案例');
      status.style.color='var(--gold)';
    }else{
      status.textContent=items.length?'● LIVE OBD 已連線':'● LIVE OBD 等待資料';
      status.style.color=items.length?'var(--mint)':'var(--danger)';
    }
  }

  const clear=$('#clear-dtc');
  if(clear){
    clear.disabled=replay||state.diagnosing;
    clear.title=replay?'REPLAY 模式不可清除實車 DTC':'';
  }
}
async function loadObd(){
  try{renderObd(await api('/api/obd'))}
  catch(e){renderObd({error:e.message,values:{}})}
}

function caseOptionLabel(c){
  const dtc=(c.dtc_codes||[]).join(',')||'無DTC';
  const km=Number(c.odometer_km||0);
  const condition=c.condition?('｜'+c.condition):'';
  return `${c.case_name||c.folder}｜${dtc}${km?('｜'+km+'km'):''}${condition}`;
}
function selectedCase(){
  const folder=$('#case-select')?.value||'';
  return state.cases.find(c=>c.folder===folder)||null;
}
function renderCaseList(cases){
  state.cases=Array.isArray(cases)?cases:[];
  const select=$('#case-select');
  const old=select?select.value:'';
  if(select){
    if(!state.cases.length){
      select.innerHTML='<option value="">目前沒有案例</option>';
    }else{
      select.innerHTML=state.cases.map(c=>`<option value="${esc(c.folder)}">${esc(caseOptionLabel(c))}</option>`).join('');
      if(state.cases.some(c=>c.folder===old))select.value=old;
      else if(state.activeReplay?.folder&&state.cases.some(c=>c.folder===state.activeReplay.folder))select.value=state.activeReplay.folder;
    }
  }

  const list=$('#record-case-list');
  if(list){
    if(!state.cases.length){
      list.innerHTML='<div class="record-case-row">目前沒有案例</div>';
    }else{
      list.innerHTML=state.cases.slice(0,12).map(c=>{
        const dtc=(c.dtc_codes||[]).join(', ')||'無';
        return `<div class="record-case-row"><b>${esc(c.case_name||c.folder)}</b>${esc(c.vehicle||'未填車型')}｜${Number(c.odometer_km||0).toLocaleString()} km｜DTC ${esc(dtc)}｜${esc(c.duration_seconds||0)} 秒</div>`;
      }).join('');
    }
  }
  renderSelectedCaseInfo();
}
function renderSelectedCaseInfo(){
  const c=selectedCase();
  const box=$('#selected-case-info');
  if(!box)return;
  if(!c){
    box.textContent='請選擇一個案例。';
    return;
  }
  const dtc=(c.dtc_codes||[]).join(', ')||'無';
  box.textContent=`${c.vehicle||'未填車型'}｜${Number(c.odometer_km||0).toLocaleString()} km｜DTC：${dtc}｜${c.duration_seconds||0} 秒｜${c.condition||'未填條件'}`;
  const km=$('#replay-odometer');
  const complaint=$('#replay-complaint');
  if(km&&Number(c.odometer_km||0)>0)km.value=String(c.odometer_km);
  if(complaint&&c.symptom&&!complaint.value.trim())complaint.value=c.symptom;
}
function renderCaseStatus(s){
  state.obdMode=s?.mode||'live';
  state.activeReplay=s?.active_case||null;
  const rec=s?.recording||{};
  state.recording=Boolean(rec.active);

  const replayBadge=$('#case-mode-badge');
  if(replayBadge){
    if(state.obdMode==='replay'){
      replayBadge.textContent='● REPLAY：'+(state.activeReplay?.case_name||'案例');
      replayBadge.classList.add('replay');
    }else{
      replayBadge.textContent='REPLAY 未啟用';
      replayBadge.classList.add('replay');
    }
  }

  const liveBadge=$('#live-source-badge');
  if(liveBadge){
    liveBadge.textContent=state.obdMode==='replay'?'等待切回 LIVE':'● LIVE';
    liveBadge.classList.toggle('replay',state.obdMode==='replay');
  }

  const recordBadge=$('#record-source-badge');
  if(recordBadge){
    if(state.recording){
      recordBadge.textContent='● 錄製中';
      recordBadge.classList.add('recording');
      recordBadge.classList.remove('replay');
    }else if(state.obdMode==='replay'){
      recordBadge.textContent='等待切回 LIVE';
      recordBadge.classList.add('replay');
      recordBadge.classList.remove('recording');
    }else{
      recordBadge.textContent='● LIVE';
      recordBadge.classList.remove('replay','recording');
    }
  }

  const start=$('#record-start'),stop=$('#record-stop');
  if(start)start.disabled=state.recording||state.obdMode==='replay';
  if(stop)stop.disabled=!state.recording;

  const use=$('#case-use'),live=$('#case-live'),del=$('#case-delete');
  if(use)use.disabled=state.recording||!selectedCase();
  if(live)live.disabled=state.recording||state.obdMode!=='replay';
  if(del)del.disabled=state.recording||!selectedCase()||(state.obdMode==='replay'&&state.activeReplay?.folder===selectedCase()?.folder);

  const replayDiagnose=$('#diagnose-replay');
  if(replayDiagnose)replayDiagnose.disabled=state.recording||state.obdMode!=='replay'||state.diagnosing;

  const txt=$('#record-status');
  const progress=$('#record-progress');
  if(txt){
    if(state.recording){
      const dtc=(rec.dtc_codes||[]).join(', ')||'無';
      txt.textContent=`錄製中 ${rec.elapsed_seconds||0} / ${rec.duration_seconds||0} 秒｜${rec.sample_count||0} 筆｜PID ${rec.pid_count||0}｜DTC ${dtc}`;
    }else if(rec.status==='complete'||rec.status==='complete_no_samples'){
      txt.textContent=`${rec.message||'錄製完成'}｜${rec.sample_count||0} 筆｜RAW CAN ${rec.raw_can?'有':'無'}`;
    }else{
      txt.textContent=rec.message||'等待錄製';
    }
  }
  if(progress){
    const pct=state.recording&&Number(rec.duration_seconds)>0
      ?Math.min(100,Number(rec.elapsed_seconds||0)/Number(rec.duration_seconds)*100)
      :0;
    progress.classList.toggle('show',state.recording);
    const bar=progress.querySelector('span');
    if(bar)bar.style.width=pct+'%';
  }
}
async function loadCaseManager(){
  try{
    const [status,list]=await Promise.all([
      api('/api/obd-case/status'),
      api('/api/obd-cases')
    ]);
    renderCaseList(list.cases||[]);
    renderCaseStatus(status);
  }catch(e){
    const txt=$('#record-status');
    if(txt)txt.textContent='案例管理服務尚未就緒：'+e.message;
  }
}
async function startRecording(){
  if(state.obdMode==='replay')return toast('請先回到 LIVE 實車模式',true);
  const km=Number($('#record-odometer').value.trim()||0);
  const symptom=$('#record-symptom').value.trim();
  const payload={
    case_name:$('#record-case-name').value.trim()||'實車案例',
    vehicle:state.obd?.vehicle_info?.model||'2009 Honda Civic 1.8L',
    odometer_km:km,
    symptom:symptom,
    condition:$('#record-condition').value.trim()||'暖車怠速',
    duration_seconds:Number($('#record-seconds').value||30)
  };
  try{
    await api('/api/obd-case/record/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    toast('開始錄製實車案例');
    await loadCaseManager();
  }catch(e){toast(e.message,true)}
}
async function stopRecording(){
  try{
    await api('/api/obd-case/record/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    toast('已要求停止錄製，正在保存案例');
    await loadCaseManager();
  }catch(e){toast(e.message,true)}
}
async function useSelectedCase(){
  const c=selectedCase();
  if(!c)return toast('請先選擇案例',true);
  if(state.recording)return toast('實車案例正在錄製，錄製完成後才能切換 REPLAY',true);
  try{
    const s=await api('/api/obd-case/replay/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({folder:c.folder})});
    state.activeReplay=s.active_case||c;
    const km=$('#replay-odometer');
    const complaint=$('#replay-complaint');
    if(km&&Number(c.odometer_km||0)>0)km.value=String(c.odometer_km);
    if(complaint&&c.symptom&&!complaint.value.trim())complaint.value=c.symptom;
    renderCaseStatus(s);
    await loadObd();
    toast('已切換 REPLAY：'+(c.case_name||c.folder));
  }catch(e){toast(e.message,true)}
}
async function backToLive(silent=false){
  try{
    const s=await api('/api/obd-case/replay/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    renderCaseStatus(s);
    await loadObd();
    if(!silent)toast('已回到 LIVE 實車模式');
    return true;
  }catch(e){
    if(!silent)toast(e.message,true);
    return false;
  }
}
async function deleteSelectedCase(){
  const c=selectedCase();
  if(!c)return toast('請先選擇案例',true);
  if(!window.confirm(`確定刪除案例「${c.case_name||c.folder}」？\n\n刪除後無法復原。`))return;
  try{
    await api('/api/obd-case/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({folder:c.folder})});
    toast('案例已刪除');
    await loadCaseManager();
  }catch(e){toast(e.message,true)}
}


const SIGNAL_UNITS={throttle:'%',rpm:'rpm',speed:'km/h',coolant:'°C',load:'%',iat:'°C',maf:'g/s'};
const SIGNAL_PRESETS={
  idle:{throttle:12,rpm:800,speed:0,coolant:90,load:18,iat:30,maf:3.5},
  city:{throttle:28,rpm:1800,speed:45,coolant:92,load:35,iat:32,maf:12},
  accel:{throttle:65,rpm:3500,speed:80,coolant:94,load:72,iat:34,maf:42},
  highway:{throttle:38,rpm:2600,speed:100,coolant:93,load:48,iat:33,maf:25},
  hot:{throttle:20,rpm:1200,speed:0,coolant:115,load:28,iat:45,maf:7}
};
function signalControl(key){return document.querySelector(`.signal-control[data-key="${key}"]`)}
function renderSignalValues(values){
  if(!values)return;
  state.signalValues={...state.signalValues,...values};
  Object.entries(state.signalValues).forEach(([key,val])=>{
    const box=signalControl(key);if(!box)return;
    const input=box.querySelector('input[type="range"]');
    const span=box.querySelector('.signal-value span');
    if(input)input.value=String(val);
    if(span)span.textContent=Number(val)%1===0?String(Math.round(Number(val))):Number(val).toFixed(1);
  });
}
function linkedThrottleValues(throttle){
  const t=Math.max(0,Math.min(100,Number(throttle)||0));
  if(t<=12)return {throttle:t,rpm:800,speed:0,load:18,maf:3.5};
  const x=(t-12)/88;
  return {
    throttle:t,
    rpm:Math.round((800+x*5200)/50)*50,
    speed:Math.round(x*130),
    load:Math.round(18+x*77),
    maf:Number((3.5+x*76.5).toFixed(1))
  };
}
async function pushSignalValues(values,{linked=true}={}){
  let next={...values};
  if(linked && $('#signal-linked')?.checked && Object.prototype.hasOwnProperty.call(next,'throttle')){
    next={...next,...linkedThrottleValues(next.throttle)};
  }
  renderSignalValues(next);
  try{
    const d=await api('/api/obd-signal/set',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({values:next})});
    renderSignalStatus(d);
  }catch(e){toast('OBD SIGNAL 設定失敗：'+e.message,true)}
}
function renderSignalStatus(d){
  if(!d)return;
  state.signalActive=Boolean(d.active);
  renderSignalValues(d.values||{});
  const st=$('#signal-status');
  if(st){st.textContent=state.signalActive?'● ECU 模擬中':'停止中';st.classList.toggle('on',state.signalActive)}
  const start=$('#signal-start'),stop=$('#signal-stop');
  if(start)start.disabled=state.signalActive;
  if(stop)stop.disabled=!state.signalActive;
  if($('#signal-request-count'))$('#signal-request-count').textContent=String(d.request_count||0);
  if($('#signal-response-count'))$('#signal-response-count').textContent=String(d.response_count||0);
  if($('#signal-interface'))$('#signal-interface').textContent=d.interface||'can0';
  if($('#signal-last-request'))$('#signal-last-request').textContent=d.last_request||'尚未收到 HUD 查詢';
  if($('#signal-last-response'))$('#signal-last-response').textContent=d.last_response||'尚未送出回覆';
  if($('#signal-error'))$('#signal-error').textContent=d.last_error||(state.signalActive?'CAN responder 正常運作':'等待啟動');
}
async function loadSignalStatus(){
  try{renderSignalStatus(await api('/api/obd-signal/status'))}
  catch(e){if($('#signal-error'))$('#signal-error').textContent=e.message}
}
async function startSignal(){
  try{
    renderSignalStatus(await api('/api/obd-signal/start',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}));
    toast('OBD SIGNAL ECU 模擬已啟動');
  }catch(e){toast('啟動 OBD SIGNAL 失敗：'+e.message,true)}
}
async function stopSignal(){
  try{
    renderSignalStatus(await api('/api/obd-signal/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}));
    toast('OBD SIGNAL 已停止');
  }catch(e){toast('停止 OBD SIGNAL 失敗：'+e.message,true)}
}
function bindSignalControls(){
  $$('.signal-control').forEach(box=>{
    const key=box.dataset.key;
    const input=box.querySelector('input[type="range"]');
    const min=Number(box.dataset.min),max=Number(box.dataset.max),step=Number(box.dataset.step||1);
    const apply=v=>{
      const n=Math.max(min,Math.min(max,Number(v)));
      pushSignalValues({[key]:n},{linked:key==='throttle'});
    };
    input?.addEventListener('change',()=>apply(input.value));
    input?.addEventListener('input',()=>{
      const span=box.querySelector('.signal-value span');
      if(span)span.textContent=Number(input.value)%1===0?String(Math.round(Number(input.value))):Number(input.value).toFixed(1);
    });
    box.querySelectorAll('button[data-dir]').forEach(btn=>btn.addEventListener('click',()=>{
      const current=Number(state.signalValues[key]??input?.value??0);
      apply(current+Number(btn.dataset.dir||0)*step);
    }));
  });
  $$('.signal-preset').forEach(btn=>btn.addEventListener('click',()=>{
    const preset=SIGNAL_PRESETS[btn.dataset.preset];
    if(preset)pushSignalValues(preset,{linked:false});
  }));
  $('#signal-start')?.addEventListener('click',startSignal);
  $('#signal-stop')?.addEventListener('click',stopSignal);
}


async function switchMode(mode){
  if(!['live','replay','record','signal'].includes(mode))return;
  if(state.diagnosing){
    toast('AI 診斷進行中，完成後才能切換模式',true);
    return;
  }

  // LIVE 與 RECORD 都必須使用實車資料來源。
  if((mode==='live'||mode==='record')&&state.obdMode==='replay'){
    const ok=await backToLive(true);
    if(!ok){
      toast('無法切回 LIVE，模式沒有切換',true);
      return;
    }
  }

  state.uiMode=mode;
  $$('.mode-tab').forEach(b=>b.classList.toggle('active',b.dataset.mode===mode));
  $$('[data-mode-panel]').forEach(p=>p.classList.toggle('active',p.dataset.modePanel===mode));

  if(mode==='record'&&state.recording){
    toast('實車案例仍在錄製中');
  }
  if(mode==='signal'){
    await loadSignalStatus();
    return;
  }
  await loadCaseManager();
  await loadObd();
}

async function clearDtc(){
  if(state.diagnosing)return toast('AI 診斷進行中，請完成後再清除故障碼',true);
  if(state.obdMode==='replay')return toast('目前是 REPLAY 案例模式，不會對實車送出清碼指令',true);
  const current=((state.obd&&state.obd.dtc_codes)||[]).map(String);
  const currentText=current.length?('目前故障碼：'+current.join(', ')+'\n\n'):'';
  const ok=window.confirm(
    currentText+
    '確定要送出清除故障碼指令嗎？\n\n此操作會清除排放相關 DTC，並可能重設 Freeze Frame 與部分 OBD readiness/監測狀態。'
  );
  if(!ok)return;
  const b=$('#clear-dtc');
  b.disabled=true;
  b.textContent='清除中…';
  try{
    const d=await api('/api/clear-dtc',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    toast(d.message||'已送出清除故障碼指令');
    await new Promise(r=>setTimeout(r,6500));
    await loadObd();
    const after=((state.obd&&state.obd.dtc_codes)||[]).map(String);
    if(after.length){
      toast('清除指令已送出；目前仍偵測到：'+after.join(', '),true);
    }else{
      toast('故障碼已清除，目前未偵測到 DTC');
    }
  }catch(e){
    toast('清除故障碼失敗：'+e.message,true);
  }finally{
    b.disabled=false;
    b.textContent='清除故障碼';
  }
}

function markStageVideoReady(video){
  if(!video)return;
  const stage=video.closest('.stage-visual');
  if(stage)stage.classList.add('has-stage-video');
}
function markStageVideoUnavailable(video){
  if(!video)return;
  const stage=video.closest('.stage-visual');
  if(stage)stage.classList.remove('has-stage-video');
}
function refreshStageVideoState(video){
  if(!video)return;
  if(video.readyState>=2 && video.videoWidth>0 && video.videoHeight>0){
    markStageVideoReady(video);
  }
}
function pauseStageVideo(video,reset=false){
  if(!video)return;
  try{video.pause();if(reset)video.currentTime=0}catch(e){}
}
function syncCinemaVideos(stage){
  const obd=$('#obd-stage-video');
  const rag=$('#rag-stage-video');

  if(stage==='obd'){
    pauseStageVideo(rag,true);
    refreshStageVideoState(obd);
    if(obd && !state.obdVideoStarted && !state.obdVideoFinished){
      state.obdVideoStarted=true;
      try{
        obd.currentTime=0;
        obd.muted=false;
        obd.volume=1.0;
      }catch(e){}
      const promise=obd.play();
      if(promise&&promise.catch){
        promise.catch(err=>{
          console.warn('OBD 影片含聲音播放被瀏覽器阻擋：',err);
          markStageVideoUnavailable(obd);
        });
      }
    }
  }else if(stage==='data'){
    pauseStageVideo(obd,false);

    if(rag){
      const ragStage=rag.closest('.stage-visual');

      // RAG 階段固定使用影片
      if(ragStage)ragStage.classList.add('has-stage-video');

      try{
        rag.loop=true;
        rag.muted=false;
        rag.volume=1.0;

        // 尚未載好時重新要求瀏覽器載入
        if(rag.readyState < 2){
          rag.load();
        }
      }catch(e){}

      const promise=rag.play();
      if(promise&&promise.catch){
        promise.catch(err=>{
          console.warn('RAG 影片播放失敗：',err);

          // 不再移除 has-stage-video，
          // 避免舊的 AI總監 fallback 畫面跑出來
        });
      }
    }
  }else{
    pauseStageVideo(obd,false);
    pauseStageVideo(rag,true);
  }
}
function setStage(stage,msg){
  // OBD 影片播完後，不允許 progress 把畫面倒退回 OBD。
  if(stage==='obd' && state.obdVideoFinished)return;

  const order=['obd','data','experts','report'];
  const idx=order.indexOf(stage);
  $$('.stage-visual').forEach(el=>el.classList.toggle('active',el.dataset.stage===stage));
  syncCinemaVideos(stage);

  const width=((idx+1)>0?((idx+1)/4*100):8)+'%';
  const bar=$('#cin-progress-bar');
  if(bar)bar.style.width=width;

  const top=$('.cin-top');
  const title=$('#cin-title');

  // OBD 與 RAG 影片本身已有文字，網站不再疊任何字。
  if(stage==='obd' || stage==='data'){
    if(top)top.classList.remove('show-title');
    return;
  }

  // 專家開始後才顯示網站標題。
  if(stage==='experts'){
    if(title)title.textContent='AI 專家會診中';
    if(top)top.classList.add('show-title');
    return;
  }

  if(stage==='report'){
    if(title)title.textContent='AI 診斷報告整理中';
    if(top)top.classList.add('show-title');
    return;
  }

  if(top)top.classList.remove('show-title');
}

function renderExperts(experts){
  ['powertrain','electrical','brake','chassis','cooling','safety'].forEach(name=>{
    const info=(experts||{})[name]||{};
    const st=info.status||'';
    const unit=document.querySelector(`.expert-unit[data-expert="${name}"]`);
    const line=document.querySelector(`.consult-line[data-expert="${name}"]`);
    const check=unit?unit.querySelector('.expert-check'):null;
    if(unit) unit.classList.remove('active','done','error');
    if(line) line.classList.remove('active','done');
    if(check) check.classList.remove('show');
    if(st==='consulting'||st==='waiting'){
      if(unit) unit.classList.add('active');
      if(line) line.classList.add('active');
    }else if(st==='done'){
      if(unit) unit.classList.add('done');
      if(line) line.classList.add('done');
      if(check) check.classList.add('show');
    }else if(st==='error'){
      if(unit) unit.classList.add('error');
      if(line) line.classList.add('active');
    }
  });
}

function expertConsultationStarted(experts){
  return Object.values(experts||{}).some(
    x=>x&&['consulting','done','error'].includes(String(x.status||''))
  );
}
function advanceAfterObdVideo(){
  if(!state.diagnosing)return;
  const experts=state.latestExperts||{};
  if(expertConsultationStarted(experts)||state.latestProgressStage==='experts'){
    setStage('experts',state.latestProgressMessage||'AI 總監正在進行專家會診');
  }else if(['report','complete'].includes(state.latestProgressStage)){
    setStage('report',state.latestProgressMessage||'AI 總監正在整理診斷報告');
  }else{
    setStage('data',RAG_STAGE_MESSAGE);
  }
}
function finishObdVideoStage(){
  if(state.obdVideoFinished)return;
  state.obdVideoFinished=true;
  if(state.obdFallbackTimer){
    clearTimeout(state.obdFallbackTimer);
    state.obdFallbackTimer=null;
  }
  advanceAfterObdVideo();
}
async function pollProgress(){
  if(!state.diagnosing)return;
  try{
    const p=await api('/api/diagnosis-progress');
    if(Number(p.revision||0)<=state.revision)return;
    state.revision=Number(p.revision||0);
    state.latestProgressStage=String(p.stage||state.latestProgressStage||'obd');
    state.latestExperts=p.experts||{};
    state.latestProgressMessage=String(p.message||'');

    renderExperts(state.latestExperts);

    // OBD 影片一定先完整播放一次；這段期間不讓 progress 提前切走畫面。
    if(!state.obdVideoFinished){
      setStage('obd','正在讀取 OBD-II 資料');
      return;
    }

    // OBD 播完後，RAG 影片持續 loop；第一位專家真正開始時才停止。
    if(expertConsultationStarted(state.latestExperts)||state.latestProgressStage==='experts'){
      setStage('experts',state.latestProgressMessage||'AI 總監正在進行專家會診');
    }else if(['report','complete'].includes(state.latestProgressStage)){
      setStage('report',state.latestProgressMessage||'AI 總監正在整理診斷報告');
    }else{
      setStage('data',RAG_STAGE_MESSAGE);
    }
  }catch(e){}
}
function showCinema(){
  state.revision=0;
  state.obdVideoStarted=false;
  state.obdVideoFinished=false;
  state.latestProgressStage='obd';
  state.latestExperts={};
  state.latestProgressMessage='';
  if(state.obdFallbackTimer)clearTimeout(state.obdFallbackTimer);

  const obd=$('#obd-stage-video');
  const rag=$('#rag-stage-video');
  pauseStageVideo(obd,true);
  pauseStageVideo(rag,true);
  try{
    if(obd){obd.muted=false;obd.volume=1.0}
    if(rag){rag.muted=false;rag.volume=1.0}
  }catch(e){}

  $('#cinema').classList.add('show');
  $('#cinema').setAttribute('aria-hidden','false');
  setStage('obd','正在讀取 OBD-II 資料');
  renderExperts({});
  const bar=$('#cin-progress-bar');if(bar)bar.style.width='25%';

  // 若影片檔遺失/瀏覽器無法播放，避免永遠卡在 OBD 畫面。
  state.obdFallbackTimer=setTimeout(()=>{
    if(state.diagnosing&&!state.obdVideoFinished)finishObdVideoStage();
  },11000);
}
function hideCinema(){
  if(state.obdFallbackTimer){
    clearTimeout(state.obdFallbackTimer);
    state.obdFallbackTimer=null;
  }
  pauseStageVideo($('#obd-stage-video'),true);
  pauseStageVideo($('#rag-stage-video'),true);
  const top=$('.cin-top');
  if(top)top.classList.remove('show-title');
  $('#cinema').classList.remove('show');
  $('#cinema').setAttribute('aria-hidden','true');
}
function labelType(t){return {maintenance:'定期保養',inspection:'檢查',repair:'維修',safety:'安全'}[t]||'項目'}
function labelPriority(p){return {urgent:'立即處理',high:'優先',normal:'一般',recommend:'建議'}[p]||p}

function itemCombinedText(item){
  return [
    item?.id||'', item?.title||'', item?.summary||'', item?.reason||'', item?.detail||'',
    ...(item?.evidence||[]), ...(item?.actions||[]), ...(item?.verification||[])
  ].join(' ').toUpperCase();
}

function detectVideoForItem(item){
  if(!item || item.type==='maintenance') return null;

  // 維修動畫只能綁定「這一個派工項目本身」明確提到的 DTC。
  // 不再因為整張派工單只有一個 DTC，就把同一支影片套到其他無關項目。
  const combined=itemCombinedText(item);

  for(const code of Object.keys(REPAIR_VIDEO_MAP)){
    if(combined.includes(code)){
      return {code,file:REPAIR_VIDEO_MAP[code]};
    }
  }

  return null;
}

function renderWorkOrder(report){
  state.report=report;
  const wo=report&&report.work_order;
  if(!wo)return false;
  const v=wo.vehicle||{};
  $('#wo-number').textContent='VE-'+String(report.id||0).padStart(6,'0');
  $('#wo-vehicle').textContent=v.model||report.vehicle||'—';
  $('#wo-mileage').textContent=(v.odometer_km||0)+' km';
  $('#wo-dtcs').textContent=(v.dtcs||[]).join(', ')||'無';
  const items=wo.work_order_items||[];
  $('#work-items').innerHTML=items.length?items.map((x,i)=>`<button class="item" data-index="${i}"><span class="type">${esc(labelType(x.type))}</span><span><b>${esc(displayText(x.title))}</b><p>${esc(displayText(x.summary||x.reason||'點擊查看詳細內容'))}</p></span><span class="priority ${esc(x.priority)}">${esc(labelPriority(x.priority))}</span><span class="arrow">›</span></button>`).join(''):'<div class="empty">AI 未建立需要執行的派工項目</div>';
  document.querySelectorAll('.item').forEach(b=>b.onclick=()=>openItem(items[Number(b.dataset.index)]));
  showView('workorder-view');
  return true
}

function openRepairVideo(item,meta){
  const player=$('#repair-video-player');
  $('#video-title').textContent=`維修動畫｜${displayText(item?.title||meta.code||'案例')}`;
  player.src='/repair-video/'+encodeURIComponent(meta.file);
  player.load();
  $('#video-dialog').showModal();
}

function openItem(x){
  $('#item-title').textContent=displayText(x.title||'項目詳細內容');
  const videoMeta=detectVideoForItem(x);
  let h=`<div class="section"><h3>建立原因</h3><p>${esc(displayText(x.reason||'—'))}</p></div><div class="section"><h3>技術說明</h3><p>${esc(displayText(x.detail||'—'))}</p></div>`;
  if((x.evidence||[]).length)h+=`<div class="section"><h3>診斷證據</h3><ul>${x.evidence.map(v=>`<li>${esc(displayText(v))}</li>`).join('')}</ul></div>`;
  if((x.actions||[]).length)h+=`<div class="section"><h3>檢查 / 維修步驟</h3><ol>${x.actions.map(v=>`<li>${esc(displayText(v))}</li>`).join('')}</ol></div>`;
  if((x.verification||[]).length)h+=`<div class="section"><h3>完成後驗證</h3><ul>${x.verification.map(v=>`<li>${esc(displayText(v))}</li>`).join('')}</ul></div>`;
  if(videoMeta){
    const model3DButton =
      videoMeta.code === 'P0351'
      ? `<button id="open-3d-button" class="btn inline secondary">查看 3D 模型</button>`
      : '';

    h+=`<div class="section"><h3>維修輔助</h3><div class="action-row"><button id="watch-video-button" class="btn inline video-btn">查看維修動畫</button>${model3DButton}</div></div>`;
  }
  $('#item-detail').innerHTML=h;
  $('#item-dialog').showModal();
  if(videoMeta){
    const btn=$('#watch-video-button');
    if(btn)btn.onclick=()=>openRepairVideo(x,videoMeta);

    if(videoMeta.code === 'P0351'){
      const modelBtn=$('#open-3d-button');
      if(modelBtn){
        modelBtn.onclick=()=>{
          window.open(
            'http://' + window.location.hostname + ':8765/preview.html?dtc=P0351',
            '_blank'
          );
        };
      }
    }
  }
}

function openFullReport(){
  const full=state.report?.work_order?.full_report||{};
  const order=[
    ['vehicle_basic','1. 車輛基本資料'],
    ['dtc_analysis','2. 故障碼與意義'],
    ['obd_analysis','3. OBD / PID 數據整理'],
    ['technical_evidence','4. 技術與歷史資料比對'],
    ['expert_consultation','5. 專家會診'],
    ['safety_assessment','6. 安全評估'],
    ['final_diagnosis','7. 總 AI 診斷'],
    ['repair_recommendation','8. 維修與完成後驗證建議']
  ];
  $('#report-detail').innerHTML=order.map(([k,t])=>`<div class="section"><h3>${t}</h3><p>${esc(displayText(full[k]||'—'))}</p></div>`).join('');
  $('#report-dialog').showModal()
}

async function diagnose(requestMode){
  if(state.recording)return toast('實車案例正在錄製，請等錄製完成後再開始 AI 診斷',true);

  const uiMode=requestMode||state.uiMode||'live';
  if(uiMode==='record')return toast('實車錄製頁不執行 AI 診斷，請切到 LIVE 或 REPLAY',true);
  if(uiMode==='replay'&&state.obdMode!=='replay'){
    return toast('請先在「案例回放」頁按「使用此案例」',true);
  }
  if(uiMode==='live'&&state.obdMode==='replay'){
    const ok=await backToLive(true);
    if(!ok)return toast('目前無法切回 LIVE 實車資料',true);
  }

  const kmEl=uiMode==='replay'?$('#replay-odometer'):$('#live-odometer');
  const complaintEl=uiMode==='replay'?$('#replay-complaint'):$('#live-complaint');
  const km=(kmEl?.value||'').trim();
  const complaint=(complaintEl?.value||'').trim()||'例行檢查，請依本次 OBD、技術資料、歷史資料與專家會診進行診斷。';

  if(!/^\d{1,7}$/.test(km)||Number(km)<=0)return toast('請輸入正確的目前公里數，例如 105230',true);

  state.diagnosing=true;
  showCinema();
  const timer=setInterval(pollProgress,800);
  const buttons=[$('#diagnose-live'),$('#diagnose-replay')].filter(Boolean);
  buttons.forEach(b=>{b.disabled=true});
  const activeButton=uiMode==='replay'?$('#diagnose-replay'):$('#diagnose-live');
  if(activeButton)activeButton.textContent='診斷中…';

  try{
    const d=await api('/api/diagnose',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        vehicle:state.obd?.vehicle_info?.model||'2009 Honda Civic 1.8L',
        odometer_km:Number(km),
        complaint,
        mode:'initial'
      })
    });
    state.caseId=d.caseId||d.case?.case_id||'';
    let shown=false;
    if(d.report)shown=renderWorkOrder(d.report);
    if(!shown){
      const latest=await api('/api/reports/latest');
      if(latest.report)shown=renderWorkOrder(latest.report);
    }
    if(!shown)throw new Error('診斷完成，但網站尚未取得派工單資料');
    setStage('report','維修派工單已建立');
    await new Promise(r=>setTimeout(r,850));
    hideCinema();
    toast((uiMode==='replay'?'REPLAY':'LIVE')+' 診斷完成，已切換到維修派工單');
  }catch(e){
    hideCinema();
    showView('diagnosis-view');
    toast(e.message,true);
  }finally{
    clearInterval(timer);
    state.diagnosing=false;
    buttons.forEach(b=>{b.disabled=false});
    if($('#diagnose-live'))$('#diagnose-live').textContent='開始 LIVE AI 診斷';
    if($('#diagnose-replay'))$('#diagnose-replay').textContent='開始 REPLAY AI 診斷';
    await loadCaseManager();
  }
}

document.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>document.getElementById(b.dataset.close).close());
$('#video-dialog').addEventListener('close',()=>{const p=$('#repair-video-player');p.pause();p.removeAttribute('src');p.load();});

// Cinema video setup:
// 1) obd_reading.mp4 播放一次。
// 2) 播完後 rag_loop.mp4 無限循環。
// 3) 第一位專家真正開始後，RAG 立即停止並切專家畫面。
(()=>{
  const obd=$('#obd-stage-video');
  const rag=$('#rag-stage-video');
  [obd,rag].forEach(v=>{
    if(!v)return;
    v.addEventListener('loadedmetadata',()=>refreshStageVideoState(v));
    v.addEventListener('loadeddata',()=>refreshStageVideoState(v));
    v.addEventListener('canplay',()=>refreshStageVideoState(v));
    v.addEventListener('playing',()=>refreshStageVideoState(v));
    v.addEventListener('error',()=>markStageVideoUnavailable(v));
    // 防止 preload 很快完成，事件在 listener 建立前就已發生。
    refreshStageVideoState(v);
    try{v.load()}catch(e){}
  });
  if(obd){
    obd.addEventListener('ended',finishObdVideoStage);
  }
})();
$('#full-report-button').onclick=openFullReport;
$('#diagnose-live').onclick=()=>diagnose('live');
$('#diagnose-replay').onclick=()=>diagnose('replay');
$('#refresh-obd-live').onclick=loadObd;
$('#refresh-obd-replay').onclick=loadObd;
$('#clear-dtc').onclick=clearDtc;
$('#record-start').onclick=startRecording;
$('#record-stop').onclick=stopRecording;
$('#case-use').onclick=useSelectedCase;
$('#case-live').onclick=()=>backToLive(false);
$('#case-delete').onclick=deleteSelectedCase;
$('#case-select').addEventListener('change',()=>{renderSelectedCaseInfo();renderCaseStatus({mode:state.obdMode,active_case:state.activeReplay,recording:{active:state.recording}})});
$$('.mode-tab').forEach(b=>b.onclick=()=>switchMode(b.dataset.mode));
$('#back-home').onclick=()=>{showView('diagnosis-view');switchMode(state.uiMode||'live')};
bindSignalControls();
showView('diagnosis-view');
switchMode('live');
setInterval(()=>{if(document.getElementById('diagnosis-view').classList.contains('active')&&state.uiMode!=='signal')loadObd()},2000);
setInterval(()=>{if(document.getElementById('diagnosis-view').classList.contains('active')&&state.uiMode!=='signal')loadCaseManager()},1500);
setInterval(()=>{if(document.getElementById('diagnosis-view').classList.contains('active')&&state.uiMode==='signal')loadSignalStatus()},800);
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "VEWorkOrder/10.2"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def send_bytes(self, data: bytes, content_type: str, status: int = 200, extra_headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, data: Any, status: int = 200) -> None:
        self.send_bytes(json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("JSON body 必須是 object")
        return data

    def serve_video_file(self, path: Path) -> None:
        file_size = path.stat().st_size
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        range_header = self.headers.get("Range", "").strip()
        start = 0
        end = file_size - 1
        status = 200
        extra = {"Accept-Ranges": "bytes"}

        if range_header.startswith("bytes="):
            status = 206
            spec = range_header.split("=", 1)[1].split(",", 1)[0].strip()
            if "-" in spec:
                start_s, end_s = spec.split("-", 1)
                if start_s:
                    start = int(start_s)
                if end_s:
                    end = int(end_s)
            if start < 0:
                start = 0
            if end >= file_size:
                end = file_size - 1
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{file_size}")
                self.end_headers()
                return
            extra["Content-Range"] = f"bytes {start}-{end}/{file_size}"

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Accept-Ranges", "bytes")
        for k, v in extra.items():
            if k != "Accept-Ranges":
                self.send_header(k, v)
        self.end_headers()
        with path.open("rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        norm = path.rstrip("/") or "/"

        if norm == "/":
            self.send_bytes(HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if norm.startswith("/repair-video/"):
            file_name = norm.split("/repair-video/", 1)[1]
            video_path = safe_video_path_from_name(file_name)
            if video_path is None:
                self.send_json({"error": "找不到維修動畫檔案"}, 404)
                return
            self.serve_video_file(video_path)
            return
        if norm == "/api/repair-videos":
            existing = {code: file for code, file in REPAIR_VIDEO_MAP.items() if (REPAIR_VIDEOS_DIR / file).exists()}
            self.send_json({"videos_dir": str(REPAIR_VIDEOS_DIR), "mapping": REPAIR_VIDEO_MAP, "existing": existing})
            return
        if norm == "/api/health":
            try:
                obd_health = fetch_json(OBD_HEALTH_URL, 2.0)
            except Exception as exc:
                obd_health = {"ok": False, "detail": str(exc)}
            self.send_json({
                "status": "ok",
                "obd_health": obd_health,
                "n8n_configured": bool(N8N_WEBHOOK_URL),
                "report_count": len(list_reports()),
                "repair_videos_dir": str(REPAIR_VIDEOS_DIR),
            })
            return
        if norm == "/api/obd":
            try:
                self.send_json(normalize_obd(fetch_json(OBD_API_URL, 3.0)))
            except Exception as exc:
                self.send_json({"error": f"無法讀取 OBD：{exc}", "values": {}, "dtc_codes": [], "vehicle_info": VEHICLE_PROFILE}, 502)
            return
        if norm == "/api/obd-case/status":
            try:
                self.send_json(fetch_json(f"{OBD_API_BASE_URL}/case/status", 3.0))
            except Exception as exc:
                self.send_json({"error": f"無法取得案例模式狀態：{exc}"}, 502)
            return
        if norm == "/api/obd-cases":
            try:
                self.send_json(fetch_json(f"{OBD_API_BASE_URL}/case/list", 4.0))
            except Exception as exc:
                self.send_json({"error": f"無法取得 OBD 案例清單：{exc}", "cases": []}, 502)
            return
        if norm == "/api/obd-signal/status":
            self.send_json(get_obd_signal_status())
            return
        if norm == "/api/diagnosis-progress":
            self.send_json(get_progress())
            return
        if norm == "/api/reports":
            self.send_json({"reports": list_reports()})
            return
        if norm == "/api/reports/latest":
            self.send_json({"report": latest_report()})
            return
        if norm.startswith("/api/reports/"):
            try:
                rid = int(norm.rsplit("/", 1)[-1])
            except ValueError:
                self.send_json({"error": "報告編號格式錯誤"}, 400)
                return
            rpt = get_report(rid)
            self.send_json({"report": rpt}, 200 if rpt else 404)
            return
        self.send_json({"error": "找不到此網址"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/")
        try:
            payload = self.read_json()
        except Exception as exc:
            self.send_json({"error": f"無效 JSON：{exc}"}, 400)
            return

        if path == "/api/obd-signal/start":
            # 桌上 ECU 模擬啟動前，確認 CAN 介面存在且 UP。
            try:
                link_check = subprocess.run(
                    ["ip", "link", "show", CAN_INTERFACE],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                if link_check.returncode != 0 or "UP" not in link_check.stdout:
                    self.send_json({"error": f"CAN 介面 {CAN_INTERFACE} 目前未啟用"}, 503)
                    return
                status = start_obd_signal_responder()
                if status.get("last_error") and not status.get("active"):
                    self.send_json({"error": status.get("last_error"), **status}, 500)
                    return
                self.send_json(status)
            except Exception as exc:
                self.send_json({"error": f"啟動 OBD SIGNAL 失敗：{exc}"}, 500)
            return

        if path == "/api/obd-signal/stop":
            self.send_json(stop_obd_signal_responder())
            return

        if path == "/api/obd-signal/set":
            values = payload.get("values") if isinstance(payload.get("values"), dict) else payload
            self.send_json(set_obd_signal_values(values))
            return

        if path == "/api/diagnosis-progress":
            self.send_json(update_progress(payload))
            return

        case_proxy_map = {
            "/api/obd-case/record/start": "/case/record/start",
            "/api/obd-case/record/stop": "/case/record/stop",
            "/api/obd-case/replay/start": "/case/replay/start",
            "/api/obd-case/replay/stop": "/case/replay/stop",
            "/api/obd-case/replay/restart": "/case/replay/restart",
            "/api/obd-case/delete": "/case/delete",
        }
        if path in case_proxy_map:
            try:
                result = post_json(
                    f"{OBD_API_BASE_URL}{case_proxy_map[path]}",
                    payload,
                    timeout=12.0,
                )
                self.send_json(result)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                try:
                    parsed = json.loads(detail)
                except Exception:
                    parsed = {"error": detail or f"OBD API HTTP {exc.code}"}
                self.send_json(parsed, exc.code)
            except Exception as exc:
                self.send_json({"error": f"OBD 案例管理失敗：{exc}"}, 502)
            return

        if path == "/api/clear-dtc":
            try:
                case_status = fetch_json(f"{OBD_API_BASE_URL}/case/status", 2.0)
            except Exception:
                case_status = {"mode": "live"}
            if case_status.get("mode") == "replay":
                self.send_json({"error": "REPLAY 案例模式不可對實車送出 Mode 04 清除故障碼"}, 409)
                return

            cansend = shutil.which("cansend")
            if not cansend:
                self.send_json({"error": "找不到 cansend，請先安裝 can-utils"}, 500)
                return
            try:
                link_check = subprocess.run(
                    ["ip", "link", "show", CAN_INTERFACE],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                if link_check.returncode != 0 or "UP" not in link_check.stdout:
                    self.send_json({"error": f"CAN 介面 {CAN_INTERFACE} 目前未啟用"}, 503)
                    return

                result = subprocess.run(
                    [cansend, CAN_INTERFACE, CLEAR_DTC_CAN_FRAME],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                if result.returncode != 0:
                    detail = (result.stderr or result.stdout or "cansend 執行失敗").strip()
                    self.send_json({"error": f"清除故障碼指令送出失敗：{detail}"}, 500)
                    return

                # OBD Reader 的 DTC 掃描有快取，前端會等待約 6.5 秒後再重新讀取。
                self.send_json({
                    "success": True,
                    "message": "已送出 Mode 04 清除故障碼指令，正在等待 ECU 重新掃描",
                    "interface": CAN_INTERFACE,
                    "request_frame": CLEAR_DTC_CAN_FRAME,
                })
            except subprocess.TimeoutExpired:
                self.send_json({"error": "清除故障碼指令逾時"}, 504)
            except Exception as exc:
                self.send_json({"error": f"清除故障碼失敗：{exc}"}, 500)
            return

        if path == "/api/report":
            ai_text = extract_report_text(payload)
            if not ai_text:
                self.send_json({"error": "缺少 report 或 output 欄位"}, 400)
                return
            vehicle = str(payload.get("vehicle") or VEHICLE_PROFILE["model"])
            complaint = str(payload.get("complaint") or "")
            metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
            rpt = save_report(vehicle, complaint, ai_text, metadata)
            self.send_json({"success": True, "message": "診斷派工單已儲存至網站", "report": rpt, "output": ai_text})
            return

        if path == "/api/diagnose":
            if not N8N_WEBHOOK_URL:
                self.send_json({"error": "尚未設定 N8N_WEBHOOK_URL"}, 503)
                return
            mode = str(payload.get("mode") or "initial").strip().lower()
            if mode not in {"initial", "recheck"}:
                self.send_json({"error": "mode 必須是 initial 或 recheck"}, 400)
                return
            try:
                case_status = fetch_json(f"{OBD_API_BASE_URL}/case/status", 2.0)
            except Exception:
                case_status = {"mode": "live"}

            if case_status.get("mode") == "replay":
                try:
                    post_json(f"{OBD_API_BASE_URL}/case/replay/restart", {}, 3.0)
                    # 讓第 0 秒案例資料就緒，再開始本次診斷。
                    time.sleep(0.08)
                except Exception as exc:
                    self.send_json({"error": f"無法從案例第 0 秒開始 REPLAY：{exc}"}, 503)
                    return

            try:
                live_obd = normalize_obd(fetch_json(OBD_API_URL, 4.0))
            except Exception as exc:
                self.send_json({"error": f"OBD 目前不可用：{exc}"}, 503)
                return
            if not live_obd.get("obd_available"):
                self.send_json({"error": "目前 OBD 沒有讀到任何 PID 或 DTC，診斷未開始"}, 503)
                return
            try:
                odometer_km = int(float(payload.get("odometer_km") or 0))
            except (TypeError, ValueError):
                odometer_km = 0
            if mode == "initial" and odometer_km <= 0:
                self.send_json({"error": "請輸入目前公里數"}, 400)
                return

            if mode == "initial":
                vehicle = str(payload.get("vehicle") or live_obd.get("vehicle_info", {}).get("model") or VEHICLE_PROFILE["model"])
                complaint = str(payload.get("complaint") or "例行檢查").strip()
                case_id = uuid.uuid4().hex[:12]
                session_id = f"website-initial-{uuid.uuid4().hex}"
                with DB_LOCK, db_connect() as con:
                    con.execute(
                        "INSERT INTO diagnostic_cases(case_id,created_at,updated_at,vehicle,odometer_km,complaint,status,initial_session_id) VALUES(?,?,?,?,?,?,?,?)",
                        (case_id, now_iso(), now_iso(), vehicle, odometer_km, complaint, "initial_running", session_id),
                    )
                    con.commit()
                reset_progress(case_id, mode)
                chat_input = (
                    "【網站初次診斷】\n"
                    f"案件編號：{case_id}\n"
                    f"固定車型：{vehicle}\n"
                    f"【目前里程公里數】：{odometer_km}\n"
                    f"車主症狀：{complaint}\n\n"
                    "請依既有 diagnostic_supervisor 規則執行完整流程：先取得本次 OBD，再搜尋 RAG，AI總監自行決定需要哪些專家，實際呼叫相關專家；所有被呼叫的專家必須使用各自子工作流強制搜尋歷史資料庫。\n"
                    "請把『目前里程公里數』原樣傳給每一個被呼叫專家的 current_odometer_km，不得改寫、估算或填 0。\n"
                    "當任一專家歷史資料庫結果包含 maintenance_due / 定期保養到期 / 缺少已完成保養紀錄時，總 AI 必須把它列入派工單。\n\n"
                    + WORK_ORDER_OUTPUT_CONTRACT
                )
            else:
                case_id = str(payload.get("caseId") or "").strip()
                repair_note = str(payload.get("repairNote") or "").strip()
                if not case_id or not repair_note:
                    self.send_json({"error": "複診需要 caseId 與 repairNote"}, 400)
                    return
                with DB_LOCK, db_connect() as con:
                    row = con.execute("SELECT * FROM diagnostic_cases WHERE case_id=?", (case_id,)).fetchone()
                if row is None:
                    self.send_json({"error": "找不到初診案件"}, 404)
                    return
                vehicle = row["vehicle"]
                complaint = row["complaint"]
                if odometer_km <= 0:
                    odometer_km = int(row["odometer_km"] or 0)
                session_id = f"website-recheck-{uuid.uuid4().hex}"
                reset_progress(case_id, mode)
                chat_input = (
                    "【完修複診／維修後驗證】\n"
                    f"案件編號：{case_id}\n固定車型：{vehicle}\n【目前里程公里數】：{odometer_km}\n"
                    f"原始症狀：{complaint}\n師傅實際維修內容：{repair_note}\n\n"
                    "必須重新呼叫 OBD、RAG 與相關專家，不得直接沿用初診結論。請比較維修前後證據，並重新建立派工單；已完成且驗證正常的項目可不再列入 pending。\n\n"
                    + WORK_ORDER_OUTPUT_CONTRACT
                )

            n8n_payload = {"action": "sendMessage", "sessionId": session_id, "chatInput": chat_input}
            try:
                body = json.dumps(n8n_payload, ensure_ascii=False).encode("utf-8")
                req = urllib.request.Request(
                    N8N_WEBHOOK_URL,
                    data=body,
                    method="POST",
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=N8N_TIMEOUT) as res:
                    raw = res.read().decode(res.headers.get_content_charset() or "utf-8")
                try:
                    result = json.loads(raw)
                except json.JSONDecodeError:
                    result = {"message": raw}
                report_obj = result.get("report") if isinstance(result, dict) and isinstance(result.get("report"), dict) else None
                if report_obj:
                    with DB_LOCK, db_connect() as con:
                        if mode == "initial":
                            con.execute(
                                "UPDATE diagnostic_cases SET status='initial_complete',initial_report_id=?,updated_at=? WHERE case_id=?",
                                (report_obj.get("id"), now_iso(), case_id),
                            )
                        else:
                            con.execute(
                                "UPDATE diagnostic_cases SET status='recheck_complete',repair_note=?,recheck_session_id=?,recheck_report_id=?,updated_at=? WHERE case_id=?",
                                (repair_note, session_id, report_obj.get("id"), now_iso(), case_id),
                            )
                        con.commit()
                update_progress({"stage": "complete", "message": "診斷與派工單建立完成"})
                if isinstance(result, dict):
                    result["caseId"] = case_id
                    result["sessionId"] = session_id
                self.send_json(result)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:3000]
                update_progress({"stage": "error", "message": f"n8n HTTP {exc.code}"})
                self.send_json({"error": f"n8n 回傳 HTTP {exc.code}", "detail": detail}, 502)
            except Exception as exc:
                update_progress({"stage": "error", "message": "n8n 連線失敗"})
                self.send_json({"error": f"無法連線 n8n：{exc}"}, 502)
            return

        self.send_json({"error": "找不到此 API"}, 404)


def main() -> None:
    init_database()
    REPAIR_VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"VE Diagnostics V10.2 + OBD SIGNAL TEST：http://{HOST}:{PORT}")
    print(f"OBD API: {OBD_API_URL}")
    print(f"n8n: {N8N_WEBHOOK_URL}")
    print(f"Repair videos dir: {REPAIR_VIDEOS_DIR}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_obd_signal_responder()
        server.server_close()


if __name__ == "__main__":
    main()
