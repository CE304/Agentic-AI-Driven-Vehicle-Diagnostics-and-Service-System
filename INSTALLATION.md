# Installation / Deployment

## 1. Recommended environment

Validated project snapshot: Raspberry Pi / Ubuntu 24.04 LTS, Python 3.12, MySQL 8, Node.js 22, n8n 2.x, python-can 4.6.1, Flask 3.1.3, PyMySQL 1.2.0, Waitress 3.0.2 and MCP 2.x.

## 2. Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## 3. MySQL history database

```bash
mysql -u root -p < database/vehicle_history_schema.sql
# Optional demo/simulated history
mysql -u root -p < database/vehicle_history_seed.sql
```

Create a least-privilege read account for the API and fill `VE_HISTORY_DB_*` in `.env`. Do not commit passwords.

## 4. RAG index

The repository intentionally excludes copyrighted original workshop-manual PDFs. Add documents you are legally allowed to use to a local source directory, then build the index:

```bash
python app/rag/ingest_rag.py \
  --source-dir app/rag/rag_sources \
  --db-dir app/rag/rag_db \
  --query "Honda Civic P0351 ignition coil"
```

## 5. Services

Open separate terminals for the first test:

```bash
python app/obd/obd_socketcan_live_reader.py --help
python app/history/vehicle_history_api.py
python app/rag/n8n_mcp_server.py --transport sse
python app/web/ve_web_v6_workorder.py
```

Typical local ports: web `5000`, n8n `5678`, OBD API `8768`, history API `8770`, MCP `8000`, local LLM OpenAI-compatible endpoint `1234`. All should be bound to localhost unless remote access is intentionally configured and protected.

## 6. n8n import

Import both JSON files from `n8n/workflows/`. The public copies have credentials removed on purpose. After importing:

1. Configure the local OpenAI-compatible credential to your local model server.
2. Reconnect the expert sub-workflow selector in the main workflow.
3. Configure the Vehicle History API header credential if API-key checking is enabled.
4. Configure the MCP Client endpoint to `http://127.0.0.1:8000/sse`.
5. Copy the Chat Trigger production webhook URL into `N8N_WEBHOOK_URL` in `.env`.
6. Activate workflows and run one simulator case before connecting a real vehicle.

## 7. Real vehicle safety

The OBD reader is read-only for Mode 01/03/07/0A. The web application contains an explicit DTC-clear action; Mode 04 should only be sent after the operator intentionally confirms the action and understands that clearing codes can erase diagnostic evidence.
