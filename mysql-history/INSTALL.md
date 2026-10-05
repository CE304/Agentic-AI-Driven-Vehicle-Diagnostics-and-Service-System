# 車輛歷史 MySQL 安裝與 n8n 串接

## 架構

六位專家不直接產生 SQL，而是呼叫本機 `vehicle_history_search` 唯讀工具。工具使用參數化查詢讀取 MySQL，固定車輛永遠是 `2009 Honda Civic 1.8L`。

資料庫內容包括：

- 固定車輛基本資料與保養／維修時間軸。
- 固定公里數保養套餐與檢查項目。
- 同年份、車型及引擎的其他車輛維修案例。
- 每筆資料的 `data_origin`，用來區分模擬、實際或匯入資料。

## 樹莓派安裝

### 1. 安裝 MySQL 相容伺服器

Raspberry Pi OS 可使用系統提供的 MySQL 相容套件：

```bash
sudo apt update
sudo apt install -y default-mysql-server default-mysql-client
sudo systemctl enable --now mariadb.service
```

若服務實際名稱是 `mysql.service`，改用該名稱。

### 2. 放置檔案

將本專案資料夾內容放進：

```text
/home/iiit/ve-diagnostics/mysql-history/
```

### 3. 建立資料表與模擬資料

```bash
cd ~/ve-diagnostics/mysql-history
sudo mysql < vehicle_history_schema.sql
sudo mysql < vehicle_history_seed.sql
```

確認資料：

```bash
sudo mysql -e "USE vehicle_diagnostics; SELECT COUNT(*) AS vehicles FROM vehicles; SELECT COUNT(*) AS repair_cases FROM repair_cases;"
```

### 4. 建立唯讀帳號

先進入資料庫：

```bash
sudo mysql
```

在 MySQL 提示符號中執行；把密碼改成自己產生的長密碼：

```sql
CREATE USER IF NOT EXISTS 've_history_reader'@'127.0.0.1'
IDENTIFIED BY '請改成長密碼';
GRANT SELECT ON vehicle_diagnostics.*
TO 've_history_reader'@'127.0.0.1';
FLUSH PRIVILEGES;
SHOW GRANTS FOR 've_history_reader'@'127.0.0.1';
EXIT;
```

這個帳號只能 `SELECT`，六位專家無法修改或刪除紀錄。

### 5. 安裝 API 套件

```bash
~/ve-diagnostics/venv/bin/pip install -r ~/ve-diagnostics/mysql-history/requirements.txt
```

建立設定檔：

```bash
cd ~/ve-diagnostics/mysql-history
cp vehicle_history_api.env.example vehicle_history_api.env
nano vehicle_history_api.env
chmod 600 vehicle_history_api.env
```

設定相同的資料庫密碼，並為 `VE_HISTORY_API_KEY` 設定另一組長隨機字串。

### 6. 測試 API

```bash
cd ~/ve-diagnostics/mysql-history
VE_HISTORY_ENV_FILE=vehicle_history_api.env ~/ve-diagnostics/venv/bin/python vehicle_history_api.py
```

另一個終端機測試：

```bash
curl -s http://127.0.0.1:8770/health | jq
```

成功後按 `Ctrl+C` 停止前景測試。

### 7. 設定開機自動啟動

```bash
sudo cp ~/ve-diagnostics/mysql-history/vehicle-history-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vehicle-history-api.service
systemctl status vehicle-history-api.service --no-pager
```

## 安裝 Skill

將：

```text
vehicle-history-db-skill/
```

複製到：

```text
/home/iiit/ve-diagnostics/skills/vehicle-expert-skills/
```

`Read Skills` 應從 `10 items` 變為 `11 items`。

在 `Tidy Skill` 的 `folderToTool` 加入：

```javascript
"vehicle-history-db-skill": "vehicle_history_db_skill",
```

每位專家的 System Message 改為「原專家 Skill + 資料庫 Skill」，例如電氣專家：

```javascript
{{ [
  $('Tidy Skill').first().json.skills.electrical_expert,
  $('Tidy Skill').first().json.skills.vehicle_history_db_skill
].filter(Boolean).join('\n\n') }}
```

其他五位專家只需替換第一個 Skill 名稱。

## n8n 專家工具

在每位專家的 `Tool` 接點新增一個 `HTTP Request Tool`：

- Tool name：`vehicle_history_search`
- Method：`POST`
- URL：`http://127.0.0.1:8770/api/search`
- Header：`X-API-Key`，值使用 `vehicle_history_api.env` 內的 API Key
- Body Content Type：JSON
- Result limit 固定為 `5`

Body 範例；`expert_domain` 應依專家固定成 `powertrain`、`electrical`、`brake`、`chassis`、`cooling_hvac` 或 `safety`：

```json
{
  "expert_domain": "electrical",
  "dtcs": "{{ $fromAI('history_dtcs', '本次與電氣系統相關的DTC，以逗號分隔；沒有則空字串', 'string') }}",
  "keywords": "{{ $fromAI('history_keywords', '2至5個繁體中文症狀、零件或故障條件關鍵詞，以逗號分隔', 'string') }}",
  "current_odometer_km": "{{ $fromAI('history_odometer_km', '本次車輛里程；未知填0', 'number') }}",
  "limit": 5
}
```

六位專家可以各自搜尋資料庫，但資料庫工具不接在 `diagnostic_supervisor`，也不取代原本 RAG。

