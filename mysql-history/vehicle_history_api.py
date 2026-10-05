#!/usr/bin/env python3
"""2009 Honda Civic 1.8L 車輛歷史 MySQL 唯讀查詢 API。"""

from __future__ import annotations

import math
import os
import re
from typing import Any

import pymysql
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from pymysql.cursors import DictCursor
from waitress import serve


load_dotenv(os.getenv("VE_HISTORY_ENV_FILE", "vehicle_history_api.env"))

APP_HOST = os.getenv("VE_HISTORY_API_HOST", "127.0.0.1")
APP_PORT = int(os.getenv("VE_HISTORY_API_PORT", "8770"))
API_KEY = os.getenv("VE_HISTORY_API_KEY", "").strip()

DB_CONFIG = {
    "host": os.getenv("VE_HISTORY_DB_HOST", "127.0.0.1"),
    "port": int(os.getenv("VE_HISTORY_DB_PORT", "3306")),
    "user": os.getenv("VE_HISTORY_DB_USER", "ve_history_reader"),
    "password": os.getenv("VE_HISTORY_DB_PASSWORD", ""),
    "database": os.getenv("VE_HISTORY_DB_NAME", "vehicle_diagnostics"),
    "charset": "utf8mb4",
    "cursorclass": DictCursor,
    "autocommit": True,
    "connect_timeout": 5,
    "read_timeout": 10,
    "write_timeout": 10,
}

FIXED_VEHICLE_KEY = "CIVIC2009_FIXED"
FIXED_VEHICLE_NAME = "2009 Honda Civic 1.8L"
VALID_DOMAINS = {
    "powertrain",
    "electrical",
    "brake",
    "chassis",
    "cooling_hvac",
    "safety",
    "multi",
}
DOMAIN_ALIASES = {
    "動力": "powertrain",
    "動力系統": "powertrain",
    "電氣": "electrical",
    "電氣系統": "electrical",
    "煞車": "brake",
    "煞車系統": "brake",
    "底盤": "chassis",
    "底盤系統": "chassis",
    "冷卻空調": "cooling_hvac",
    "冷卻與空調": "cooling_hvac",
    "安全": "safety",
    "安全風險": "safety",
}

app = Flask(__name__)
app.json.ensure_ascii = False


def db_connection():
    return pymysql.connect(**DB_CONFIG)


def is_authorized() -> bool:
    if not API_KEY:
        return True
    return request.headers.get("X-API-Key", "") == API_KEY


def clean_domain(value: Any) -> str | None:
    if value is None:
        return None
    domain = str(value).strip().lower()
    domain = DOMAIN_ALIASES.get(domain, domain)
    return domain if domain in VALID_DOMAINS else None


def clean_dtcs(value: Any) -> list[str]:
    if isinstance(value, str):
        candidates = re.split(r"[,，\s]+", value)
    elif isinstance(value, list):
        candidates = value
    else:
        candidates = []

    result: list[str] = []
    for item in candidates:
        dtc = str(item).strip().upper()
        if re.fullmatch(r"[PCBU][0-9A-F]{4}", dtc) and dtc not in result:
            result.append(dtc)
    return result[:10]


def clean_keywords(value: Any) -> list[str]:
    if isinstance(value, str):
        candidates = re.split(r"[,，、]+", value)
    elif isinstance(value, list):
        candidates = value
    else:
        candidates = []

    result: list[str] = []
    for item in candidates:
        keyword = re.sub(r"\s+", " ", str(item)).strip()
        if 1 < len(keyword) <= 40 and keyword not in result:
            result.append(keyword)
    return result[:8]


def bounded_limit(value: Any) -> int:
    try:
        return max(1, min(int(value), 10))
    except (TypeError, ValueError):
        return 5


def build_relevance_filter(
    text_columns: list[str], dtc_column: str | None, dtcs: list[str], keywords: list[str]
) -> tuple[str, list[Any]]:
    groups: list[str] = []
    params: list[Any] = []

    if dtc_column and dtcs:
        dtc_parts = []
        for dtc in dtcs:
            dtc_parts.append(f"UPPER(COALESCE({dtc_column}, '')) LIKE %s")
            params.append(f"%{dtc}%")
        groups.append("(" + " OR ".join(dtc_parts) + ")")

    if keywords:
        keyword_groups = []
        combined = "CONCAT_WS(' ', " + ", ".join(text_columns) + ")"
        for keyword in keywords:
            keyword_groups.append(f"{combined} LIKE %s")
            params.append(f"%{keyword}%")
        groups.append("(" + " OR ".join(keyword_groups) + ")")

    if not groups:
        return "", []
    return " AND (" + " OR ".join(groups) + ")", params


def get_fixed_vehicle(cursor: DictCursor) -> dict[str, Any]:
    cursor.execute(
        """
        SELECT vehicle_id, vehicle_key, make, model, model_year, engine,
               transmission, vin_masked, plate_masked, current_odometer_km,
               data_origin, notes
        FROM vehicles
        WHERE vehicle_key = %s AND active = 1
        """,
        (FIXED_VEHICLE_KEY,),
    )
    vehicle = cursor.fetchone()
    if not vehicle:
        raise RuntimeError("找不到固定車輛資料")
    return vehicle


def query_service_history(
    cursor: DictCursor,
    vehicle_id: int,
    domain: str | None,
    keywords: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    domain_sql = ""
    params: list[Any] = [vehicle_id]
    if domain:
        domain_sql = " AND (s.system_domain = %s OR s.system_domain = 'multi')"
        params.append(domain)

    relevance_sql, relevance_params = build_relevance_filter(
        [
            "COALESCE(s.work_summary, '')",
            "COALESCE(s.inspection_findings, '')",
            "COALESCE(s.parts_or_fluids, '')",
        ],
        None,
        [],
        keywords,
    )
    params.extend(relevance_params)
    params.append(limit)

    cursor.execute(
        f"""
        SELECT s.service_date, s.odometer_km, s.record_type, s.system_domain,
               s.package_code, s.work_summary, s.inspection_findings,
               s.parts_or_fluids, s.next_due_km, s.next_due_date,
               s.data_origin
        FROM service_records s
        WHERE s.vehicle_id = %s
          {domain_sql}
          {relevance_sql}
        ORDER BY s.service_date DESC, s.odometer_km DESC
        LIMIT %s
        """,
        params,
    )
    return list(cursor.fetchall())


def query_repair_cases(
    cursor: DictCursor,
    fixed_vehicle: dict[str, Any],
    domain: str | None,
    dtcs: list[str],
    keywords: list[str],
    limit: int,
    same_model_only: bool,
) -> list[dict[str, Any]]:
    params: list[Any] = []
    if same_model_only:
        vehicle_sql = """
          v.vehicle_id <> %s
          AND v.model_year = %s AND v.make = %s AND v.model = %s AND v.engine = %s
        """
        params.extend(
            [
                fixed_vehicle["vehicle_id"],
                fixed_vehicle["model_year"],
                fixed_vehicle["make"],
                fixed_vehicle["model"],
                fixed_vehicle["engine"],
            ]
        )
    else:
        vehicle_sql = "v.vehicle_id = %s"
        params.append(fixed_vehicle["vehicle_id"])

    domain_sql = ""
    if domain:
        domain_sql = " AND r.system_domain = %s"
        params.append(domain)

    relevance_sql, relevance_params = build_relevance_filter(
        [
            "COALESCE(r.owner_complaint, '')",
            "COALESCE(r.occurrence_conditions, '')",
            "COALESCE(r.pid_evidence, '')",
            "COALESCE(r.confirmed_root_cause, '')",
            "COALESCE(r.inspection_process, '')",
            "COALESCE(r.repair_action, '')",
        ],
        "r.dtc_codes",
        dtcs,
        keywords,
    )
    params.extend(relevance_params)
    params.append(limit)

    cursor.execute(
        f"""
        SELECT r.case_key, v.vehicle_key, r.case_date, r.odometer_km,
               r.system_domain, r.owner_complaint, r.occurrence_conditions,
               r.dtc_codes, r.pid_evidence, r.confirmed_root_cause,
               r.inspection_process, r.repair_action, r.parts_replaced,
               r.verification_result, r.safety_disposition,
               r.resolution_status, r.data_origin
        FROM repair_cases r
        JOIN vehicles v ON v.vehicle_id = r.vehicle_id
        WHERE {vehicle_sql}
          {domain_sql}
          {relevance_sql}
        ORDER BY r.case_date DESC, r.odometer_km DESC
        LIMIT %s
        """,
        params,
    )
    return list(cursor.fetchall())


def get_maintenance_due(
    cursor: DictCursor, vehicle: dict[str, Any], requested_odometer: Any
) -> dict[str, Any]:
    try:
        current_km = int(requested_odometer)
    except (TypeError, ValueError):
        current_km = int(vehicle.get("current_odometer_km") or 0)
    current_km = max(0, min(current_km, 2_000_000))

    cursor.execute(
        """
        SELECT MAX(odometer_km) AS last_service_km,
               MAX(next_due_km) AS recorded_next_due_km
        FROM service_records
        WHERE vehicle_id = %s
        """,
        (vehicle["vehicle_id"],),
    )
    service_state = cursor.fetchone() or {}

    next_milestone = (math.floor(current_km / 5000) + 1) * 5000
    cursor.execute(
        """
        SELECT package_code, package_name_zh, interval_km, interval_months,
               package_priority, rule_type, disclaimer
        FROM maintenance_packages
        WHERE active = 1
          AND %s MOD interval_km = 0
        ORDER BY package_priority DESC
        LIMIT 1
        """,
        (next_milestone,),
    )
    package = cursor.fetchone()
    if package:
        cursor.execute(
            """
            SELECT item_order, system_domain, item_name_zh, action_type,
                   normal_result, abnormal_action, mandatory
            FROM maintenance_package_items
            WHERE package_code = %s
            ORDER BY item_order
            """,
            (package["package_code"],),
        )
        package["items"] = list(cursor.fetchall())

    recorded_due = service_state.get("recorded_next_due_km")
    return {
        "current_odometer_km": current_km,
        "last_service_km": service_state.get("last_service_km"),
        "recorded_next_due_km": recorded_due,
        "recorded_service_overdue": bool(recorded_due and current_km >= recorded_due),
        "next_fixed_mileage_milestone_km": next_milestone,
        "remaining_km_to_milestone": max(0, next_milestone - current_km),
        "selected_package": package,
        "rule_notice": "固定里程套餐為系統示範規則，正式保養仍須依原廠資料、時間、使用環境與實際檢查調整。",
    }


@app.before_request
def authorize_request():
    if request.path == "/health":
        return None
    if not is_authorized():
        return jsonify({"status": "error", "error": "unauthorized"}), 401
    return None


@app.get("/health")
def health():
    try:
        with db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 AS ok")
                result = cursor.fetchone()
        return jsonify(
            {
                "status": "ok",
                "service": "vehicle-history-mysql",
                "database_connected": result == {"ok": 1},
                "fixed_vehicle": FIXED_VEHICLE_NAME,
            }
        )
    except Exception as exc:
        return jsonify({"status": "error", "database_connected": False, "error": str(exc)}), 503


@app.post("/api/search")
def search_history():
    payload = request.get_json(silent=True) or {}
    domain = clean_domain(payload.get("expert_domain"))
    dtcs = clean_dtcs(payload.get("dtcs"))
    keywords = clean_keywords(payload.get("keywords"))
    limit = bounded_limit(payload.get("limit"))

    try:
        with db_connection() as connection:
            with connection.cursor() as cursor:
                vehicle = get_fixed_vehicle(cursor)
                service_history = query_service_history(
                    cursor, vehicle["vehicle_id"], domain, keywords, limit
                )
                fixed_repairs = query_repair_cases(
                    cursor, vehicle, domain, dtcs, keywords, limit, False
                )
                same_model_repairs = query_repair_cases(
                    cursor, vehicle, domain, dtcs, keywords, limit, True
                )
                maintenance_due = get_maintenance_due(
                    cursor, vehicle, payload.get("current_odometer_km")
                )

        return jsonify(
            {
                "status": "ok",
                "fixed_vehicle": FIXED_VEHICLE_NAME,
                "query": {
                    "expert_domain": domain,
                    "dtcs": dtcs,
                    "keywords": keywords,
                    "limit_per_section": limit,
                },
                "fixed_vehicle_profile": vehicle,
                "fixed_vehicle_service_history": service_history,
                "fixed_vehicle_repair_cases": fixed_repairs,
                "same_model_repair_cases": same_model_repairs,
                "maintenance_due": maintenance_due,
                "evidence_rules": [
                    "data_origin=simulated 的紀錄只能作為模擬案例，不可當成本車已確認事實。",
                    "歷史案例不能取代本次OBD、RAG、量測及專家交叉判斷。",
                    "只有本車實際紀錄且與本次條件相符時，才能用來降低或提高某項故障可能性。",
                ],
            }
        )
    except Exception as exc:
        return jsonify({"status": "error", "error": str(exc)}), 500


@app.get("/api/maintenance/due")
def maintenance_due():
    try:
        with db_connection() as connection:
            with connection.cursor() as cursor:
                vehicle = get_fixed_vehicle(cursor)
                result = get_maintenance_due(
                    cursor, vehicle, request.args.get("odometer_km")
                )
        return jsonify(
            {
                "status": "ok",
                "fixed_vehicle": FIXED_VEHICLE_NAME,
                "maintenance_due": result,
            }
        )
    except Exception as exc:
        return jsonify({"status": "error", "error": str(exc)}), 500


if __name__ == "__main__":
    serve(app, host=APP_HOST, port=APP_PORT, threads=4)

