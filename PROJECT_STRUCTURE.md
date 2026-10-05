# Project Structure / 檔案目錄說明

```text
agentic-ai-vehicle-diagnostics/
├─ README.md                         GitHub 首頁與系統總覽
├─ MODEL_DISCLOSURE.md               開源 AI 模型名稱、來源、授權與系統角色
├─ INSTALLATION.md                   安裝、啟動與 n8n 匯入說明
├─ PROJECT_STRUCTURE.md              本文件
├─ THIRD_PARTY_NOTICES.md            第三方模型/資料/平台授權說明
├─ LICENSE                           本團隊程式 MIT License
├─ SECURITY.md                       金鑰與漏洞回報政策
├─ CONTRIBUTING.md                   貢獻規範
├─ requirements.txt                 Python 依賴版本
├─ .env.example                     可公開環境變數範例
├─ .gitignore                        排除秘密、模型、大型資料與執行期檔案
├─ app/
│  ├─ obd/obd_socketcan_live_reader.py   實車 OBD-II/CAN 讀取、DTC/PID API、錄製/回放
│  ├─ rag/
│  │  ├─ n8n_mcp_server.py               對 n8n 提供 OBD/RAG/系統工具的 MCP Server
│  │  ├─ ingest_rag.py                    建立本機 RAG 索引
│  │  ├─ rag_sources/                     可公開的 DTC/PID/TSB/召回/案例文字資料
│  │  └─ rag_db/                          執行後產生索引；索引本體不提交 Git
│  ├─ history/vehicle_history_api.py      MySQL 歷史維修資料唯讀查詢 API
│  └─ web/ve_web_v6_workorder.py          診斷網站、動畫、派工單、維修指引
├─ database/
│  ├─ vehicle_history_schema.sql          資料庫結構
│  └─ vehicle_history_seed.sql            模擬測試資料，內容明確標示 simulated
├─ n8n/workflows/                         可匯入的公開 n8n 工作流（credentials 已移除）
├─ skills/vehicle-expert-skills/          AI 總監、證據工具與六大專家 Skill 政策
├─ docs/
│  ├─ COMPETITION_COMPLIANCE.md           開源 AI 模型應用組對照
│  ├─ DEPLOYMENT_ARCHITECTURE.md          服務與 Port/資料流
│  ├─ DATA_GOVERNANCE.md                  資料、RAG、歷史案例治理
│  └─ assets/system_architecture.png      系統架構圖
└─ deploy/systemd/                        Linux service 範例
```

## GitHub 上傳原則

建議直接把此資料夾內容放在 GitHub repository 根目錄。不要把外層競賽報名 DOCX 一起放進 public repository，以避免競賽文件與程式版本混在一起。
