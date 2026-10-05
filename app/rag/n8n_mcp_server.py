# -*- coding: utf-8 -*-
"""提供給 n8n AI Agent 使用的本機 MCP 工具伺服器。

安裝套件：
    py -m pip install --upgrade "mcp[cli]>=2,<3"

n8n MCP Client Tool 使用 SSE 時：
    python n8n_mcp_server.py --transport sse
    Endpoint: http://127.0.0.1:8000/sse

目前提供四個唯讀工具：
    get_current_time：讀取本機時間
    get_pc_status：讀取電腦基本狀態
    get_obd_data：讀取 2008 Honda Civic 的模擬 OBD 資料
    search_vehicle_rag：搜尋本機汽車診斷 RAG 索引
"""

from __future__ import annotations

import argparse
import ctypes
from datetime import datetime
import json
import os
import platform
from pathlib import Path
import shutil
import socket
from urllib import request, error
from typing import Any, Literal


HOST = os.environ.get("VE_MCP_HOST", "127.0.0.1").strip() or "127.0.0.1"
PORT = int(os.environ.get("VE_MCP_PORT", "8000"))
BASE_DIR = Path(__file__).resolve().parent
RAG_INDEX_PATH = Path(
    os.environ.get("VE_RAG_INDEX_PATH", str(BASE_DIR / "rag_db" / "rag_index.json"))
).resolve()

VEHICLE_HISTORY_API_URL = os.environ.get(
    "VEHICLE_HISTORY_API_URL",
    "http://127.0.0.1:8770/api/search",
)

VEHICLE_HISTORY_API_HEADER_NAME = os.environ.get(
    "VEHICLE_HISTORY_API_HEADER_NAME",
    "X-API-Key",
).strip()

VEHICLE_HISTORY_API_HEADER_VALUE = (
    os.environ.get("VEHICLE_HISTORY_API_HEADER_VALUE", "").strip()
    or os.environ.get("VE_HISTORY_API_KEY", "").strip()
)


def format_gib(value: int | None) -> str:
    """將位元組轉成容易閱讀的 GB 字串。"""
    if value is None:
        return "無法讀取"
    return f"{value / (1024 ** 3):.1f} GB"


def get_memory_bytes() -> tuple[int | None, int | None]:
    """回傳（總記憶體, 可用記憶體），不使用第三方套件。"""
    if os.name == "nt":
        class MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatusEx()
        status.dwLength = ctypes.sizeof(MemoryStatusEx)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.ullTotalPhys), int(status.ullAvailPhys)
        return None, None

    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        total_pages = os.sysconf("SC_PHYS_PAGES")
        available_pages = os.sysconf("SC_AVPHYS_PAGES")
        return page_size * total_pages, page_size * available_pages
    except (AttributeError, OSError, ValueError):
        return None, None


def get_system_drive() -> Path:
    """取得目前作業系統的系統磁碟。"""
    if os.name == "nt":
        return Path(os.environ.get("SystemDrive", "C:") + "\\")
    return Path("/")


def get_current_time() -> dict[str, Any]:
    """讀取執行 MCP Server 這台電腦的目前本機日期與時間。

    當使用者詢問現在時間、今天日期、星期或幾點時使用此工具。
    不要用模型自己的知識猜測時間。
    """
    now = datetime.now().astimezone()
    weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    return {
        "message": f"本機現在時間是 {now:%Y年%m月%d日 %H:%M:%S}，{weekdays[now.weekday()]}。",
        "iso_time": now.isoformat(timespec="seconds"),
        "timezone": now.tzname() or "local",
        "weekday": weekdays[now.weekday()],
        "source": "local_computer",
    }


def get_pc_status() -> dict[str, Any]:
    """讀取執行 MCP Server 這台電腦的安全基本狀態。

    當使用者詢問本機名稱、作業系統、CPU 核心數、記憶體、磁碟容量
    或電腦基本狀態時使用。此工具只讀取資料，不會修改任何系統設定。
    """
    drive = get_system_drive()
    disk = shutil.disk_usage(drive)
    memory_total, memory_available = get_memory_bytes()
    logical_cpu_count = os.cpu_count()

    message = (
        f"本機名稱：{socket.gethostname()}；"
        f"作業系統：{platform.system()} {platform.release()}；"
        f"邏輯處理器：{logical_cpu_count} 核心；"
        f"記憶體：可用 {format_gib(memory_available)} / 總計 {format_gib(memory_total)}；"
        f"系統碟：剩餘 {format_gib(disk.free)} / 總計 {format_gib(disk.total)}。"
    )

    return {
        "message": message,
        "computer_name": socket.gethostname(),
        "operating_system": f"{platform.system()} {platform.release()}",
        "os_version": platform.version(),
        "architecture": platform.machine(),
        "logical_cpu_count": logical_cpu_count,
        "python_version": platform.python_version(),
        "system_drive": str(drive),
        "disk_total_bytes": disk.total,
        "disk_free_bytes": disk.free,
        "memory_total_bytes": memory_total,
        "memory_available_bytes": memory_available,
        "read_only": True,
        "source": "local_computer",
    }


def get_obd_data() -> dict[str, Any]:
    """讀取目前車輛的 OBD-II 故障碼與即時 PID 模擬資料。

    當使用者要求讀取車況、OBD、DTC、故障碼、引擎轉速、水溫、
    燃油修正或感知器資料時使用。這一版是 2008 Honda Civic 1.8L
    的固定模擬案例，不是實車量測；模型回答時必須明確說明這點。
    """
    captured_at = datetime.now().astimezone()
    return {
        "message": (
            "已讀取模擬 OBD 資料：2008 Honda Civic 1.8L，"
            "目前有故障碼 P0171；怠速時短期燃油修正 +18.8%，"
            "長期燃油修正 +15.6%。此資料僅供流程測試，不是實車量測。"
        ),
        "vehicle": {
            "make": "Honda",
            "model": "Civic",
            "year": 2008,
            "engine": "1.8L gasoline",
            "transmission": "automatic",
        },
        "connection": {
            "protocol": "ISO 15765-4 CAN (11-bit, 500 kbit/s)",
            "ecu": "PCM",
            "status": "connected_simulation",
        },
        "dtc": [
            {
                "code": "P0171",
                "status": "confirmed",
                "description": "System Too Lean (Bank 1)",
            }
        ],
        "pid": {
            "engine_rpm": {"value": 742, "unit": "rpm"},
            "vehicle_speed": {"value": 0, "unit": "km/h"},
            "engine_coolant_temperature": {"value": 91, "unit": "°C"},
            "intake_air_temperature": {"value": 34, "unit": "°C"},
            "calculated_engine_load": {"value": 24.7, "unit": "%"},
            "throttle_position": {"value": 14.1, "unit": "%"},
            "intake_manifold_absolute_pressure": {"value": 31, "unit": "kPa"},
            "short_term_fuel_trim_bank_1": {"value": 18.8, "unit": "%"},
            "long_term_fuel_trim_bank_1": {"value": 15.6, "unit": "%"},
            "commanded_equivalence_ratio": {"value": 1.0, "unit": "ratio"},
            "control_module_voltage": {"value": 13.9, "unit": "V"},
            "fuel_system_status": {"value": "closed_loop", "unit": None},
        },
        "operating_condition": {
            "engine_state": "warm_idle",
            "gear": "P",
            "air_conditioning": "off",
        },
        "captured_at": captured_at.isoformat(timespec="seconds"),
        "simulated": True,
        "read_only": True,
        "source": "built_in_obd_simulator",
    }


def search_vehicle_rag(query: str, top_k: int = 3) -> dict[str, Any]:
    """搜尋本機汽車診斷 RAG 資料庫。

    當使用者要求分析 DTC、PID、車主症狀、可能原因、檢查順序或
    維修方法時使用。query 應包含車型、年份、引擎、故障碼、重要
    PID 與症狀；不要只傳入「幫我診斷」這類模糊文字。
    """
    cleaned_query = query.strip()
    if not cleaned_query:
        return {
            "message": "RAG 搜尋問題不可為空白。",
            "query": query,
            "results": [],
            "result_count": 0,
            "source": "local_rag_index",
            "read_only": True,
        }
    if not RAG_INDEX_PATH.exists():
        return {
            "message": (
                f"找不到 RAG 索引：{RAG_INDEX_PATH}。"
                "請先執行 ingest_rag.py 建立資料庫。"
            ),
            "query": cleaned_query,
            "results": [],
            "result_count": 0,
            "source": "local_rag_index",
            "read_only": True,
        }

    try:
        from ingest_rag import cosine_search
    except ImportError as error:
        return {
            "message": f"無法載入 ingest_rag.py：{error}",
            "query": cleaned_query,
            "results": [],
            "result_count": 0,
            "source": "local_rag_index",
            "read_only": True,
        }

    try:
        index = json.loads(RAG_INDEX_PATH.read_text(encoding="utf-8"))
        safe_top_k = max(1, min(int(top_k), 5))
        results = cosine_search(index, cleaned_query, safe_top_k)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        return {
            "message": f"RAG 索引讀取或搜尋失敗：{error}",
            "query": cleaned_query,
            "results": [],
            "result_count": 0,
            "source": "local_rag_index",
            "read_only": True,
        }

    return {
        "message": f"RAG 搜尋完成，找到 {len(results)} 個相關文字區塊。",
        "query": cleaned_query,
        "results": results,
        "result_count": len(results),
        "index_created_at": index.get("created_at"),
        "source": "local_rag_index",
        "read_only": True,
    }



def search_vehicle_history(
    expert_domain: str,
    dtcs: str = "",
    keywords: str = "",
    current_odometer_km: int = 0,
    limit: int = 5,
) -> dict[str, Any]:
    """搜尋 2009 Honda Civic 1.8L 的 SQL 車輛歷史資料庫。

    此工具提供給各領域專家使用。

    expert_domain 必須為：
    powertrain、electrical、brake、chassis、cooling_hvac 或 safety。

    dtcs：
    本次相關故障碼，可用逗號分隔；沒有則空字串。

    keywords：
    2 至 5 個與本次症狀、零件、故障條件相關的關鍵詞。

    current_odometer_km：
    本次網站提供的實際公里數，不得使用歷史案例里程取代。

    本工具為唯讀搜尋，不會修改 SQL 資料庫。
    """

    allowed_domains = {
        "powertrain",
        "electrical",
        "brake",
        "chassis",
        "cooling_hvac",
        "safety",
    }

    clean_domain = str(expert_domain or "").strip()

    if clean_domain not in allowed_domains:
        return {
            "message": f"不支援的 expert_domain：{clean_domain}",
            "executed": False,
            "results": [],
            "source": "vehicle_history_sql_api",
            "read_only": True,
        }

    try:
        safe_odometer = int(float(current_odometer_km or 0))
    except (TypeError, ValueError):
        safe_odometer = 0

    try:
        safe_limit = max(1, min(int(limit), 10))
    except (TypeError, ValueError):
        safe_limit = 5

    payload = {
        "expert_domain": clean_domain,
        "dtcs": str(dtcs or "").strip(),
        "keywords": str(keywords or "").strip(),
        "current_odometer_km": safe_odometer,
        "limit": safe_limit,
    }

    body = json.dumps(
        payload,
        ensure_ascii=False,
    ).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    if VEHICLE_HISTORY_API_HEADER_NAME and VEHICLE_HISTORY_API_HEADER_VALUE:
        headers[VEHICLE_HISTORY_API_HEADER_NAME] = VEHICLE_HISTORY_API_HEADER_VALUE

    req = request.Request(
        VEHICLE_HISTORY_API_URL,
        data=body,
        headers=headers,
        method="POST",
    )

    try:
        with request.urlopen(req, timeout=15) as response:
            raw = response.read().decode("utf-8", errors="replace")

            try:
                api_result = json.loads(raw)
            except json.JSONDecodeError:
                api_result = {
                    "raw_response": raw
                }

            return {
                "message": "SQL 車輛歷史資料搜尋完成。",
                "executed": True,
                "query": payload,
                "result": api_result,
                "source": "vehicle_history_sql_api",
                "read_only": True,
            }

    except error.HTTPError as exc:
        response_text = ""

        try:
            response_text = exc.read().decode(
                "utf-8",
                errors="replace",
            )
        except Exception:
            pass

        return {
            "message": f"SQL 車輛歷史 API HTTP 錯誤：{exc.code}",
            "executed": False,
            "http_status": exc.code,
            "response": response_text,
            "query": payload,
            "source": "vehicle_history_sql_api",
            "read_only": True,
        }

    except error.URLError as exc:
        return {
            "message": f"無法連線 SQL 車輛歷史 API：{exc.reason}",
            "executed": False,
            "query": payload,
            "source": "vehicle_history_sql_api",
            "read_only": True,
        }

    except Exception as exc:
        return {
            "message": f"SQL 車輛歷史資料搜尋失敗：{exc}",
            "executed": False,
            "query": payload,
            "source": "vehicle_history_sql_api",
            "read_only": True,
        }



def build_mcp_server() -> Any:
    """載入官方 MCP SDK 並註冊白名單工具。"""
    try:
        from mcp.server import MCPServer
    except ImportError as error:
        raise RuntimeError(
            '尚未安裝 MCP SDK。請先執行：py -m pip install --upgrade "mcp[cli]>=2,<3"'
        ) from error

    server = MCPServer(
        "n8n-local-python-tools",
        instructions=(
            "這些是唯讀工具。一般知識問題由模型直接回答；"
            "需要即時本機時間、電腦狀態、車輛 OBD 資料或汽車診斷 RAG 時才呼叫對應工具。"
            "get_obd_data 目前只回傳模擬資料，回答時不得宣稱是實車量測。"
            "進行車輛診斷時，先取得 OBD 資料，再用完整車況查詢 search_vehicle_rag。"
        ),
    )
    server.tool()(get_current_time)
    server.tool()(get_pc_status)
    server.tool()(get_obd_data)
    server.tool()(search_vehicle_rag)
    server.tool()(search_vehicle_history)
    return server


def run_server(transport: Literal["sse", "streamable-http"]) -> None:
    """啟動 MCP Server。"""
    server = build_mcp_server()
    if transport == "sse":
        endpoint = f"http://{HOST}:{PORT}/sse"
        print(f"MCP Server 已啟動：{endpoint}", flush=True)
        print("請保持此 PowerShell 視窗開啟。", flush=True)
        server.run(transport="sse", host=HOST, port=PORT, sse_path="/sse")
    else:
        endpoint = f"http://{HOST}:{PORT}/mcp"
        print(f"MCP Server 已啟動：{endpoint}", flush=True)
        print("請保持此 PowerShell 視窗開啟。", flush=True)
        server.run(
            transport="streamable-http",
            host=HOST,
            port=PORT,
            streamable_http_path="/mcp",
        )


def self_test() -> None:
    """不啟動網路服務，直接測試工具的基本輸出。"""
    time_result = get_current_time()
    pc_result = get_pc_status()
    obd_result = get_obd_data()
    assert time_result["source"] == "local_computer"
    assert pc_result["source"] == "local_computer"
    assert pc_result["read_only"] is True
    assert obd_result["simulated"] is True
    assert obd_result["read_only"] is True
    assert obd_result["vehicle"]["model"] == "Civic"
    assert obd_result["dtc"][0]["code"] == "P0171"
    rag_result = search_vehicle_rag("2008 Honda Civic P0171 燃油修正過高")
    assert rag_result["source"] == "local_rag_index"
    assert rag_result["read_only"] is True
    print(
        json.dumps(
            {
                "time": time_result,
                "pc_status": pc_result,
                "obd": obd_result,
                "rag": rag_result,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print("離線自我測試：OK")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="提供時間、電腦狀態、模擬 OBD 與汽車 RAG 工具給 n8n AI Agent。"
    )
    parser.add_argument(
        "--transport",
        choices=["sse", "streamable-http"],
        default="sse",
        help="n8n MCP Client Tool 的連線方式，預設為 sse。",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="只測試 Python 工具，不啟動 MCP Server。",
    )
    args = parser.parse_args()

    if args.self_test:
        self_test()
    else:
        run_server(args.transport)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nMCP Server 已停止。")
    except Exception as error:
        print(f"錯誤：{error}")
        raise SystemExit(1)
