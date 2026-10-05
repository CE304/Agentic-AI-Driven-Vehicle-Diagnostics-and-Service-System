
2008 Honda Civic 1.8L DTC RAG 免費公開資料擴充包

建立日期：2026-08-20
目標車輛：2008 Honda Civic 一般版 1.8L

這包新增什麼
1. 從 NHTSA Manufacturer Communications 公開資料整理 1,126,177 筆資料列。
2. 建立 5,629 個不重複 DTC 字串的搜尋索引：P=3,319、B=994、C=756、U=560。
3. 建立 2008 Civic 家族車型隔離層，避免把 Si、Hybrid、GX/CNG 的資料誤套到一般 1.8L。
4. 加入 DTC 回答政策，要求 AI 區分『正式定義』『通報觀察』『個案推論』。

安裝到你的 Windows RAG
1. 解壓縮本 ZIP。
2. 把解壓後資料夾內的 .txt 全部複製到：
   C:\n8n_python\rag_sources\Honda_Civic\03_dtc
3. 保留你原有的 Honda_Civic_2008_P0171_RAG_測試資料.txt，不要刪除。
4. 執行你現有的 ingest_rag.py 重建索引。
5. 重新啟動 n8n MCP／RAG 服務，再用 P0456、B1501、P0201 測試。

檔案用途
- 01：DTC 結構、證據分級與安全規則。
- 02：2008 Civic 已核對 DTC／車型變體隔離。
- 03A～03D：NHTSA 公開技術通報的 P/B/C/U 代碼觀察索引。
- 04：公開通報中最常出現的前 200 個 DTC 搜尋入口。
- 05：強制總 AI 不猜 Honda 專有碼的回答政策。
- 06：2008 Civic 家族公開文件 ID 與車型索引。
- 07：來源、檔案雜湊與限制。

重要限制
- 『最多』在免費且可核對的範圍內，不等於 SAE/Honda 完整正式定義庫。
- SAE J2012/J2012DA 的完整現行 DTC 定義資料屬正式標準產品；Honda 原廠細節應查 ServiceExpress/HDS。
- 03A～03D 只證明代碼字串曾出現在 NHTSA 公開製造商通報，不提供正式定義。
- 禁止依代碼直接更換零件；必須結合 freeze frame、PID、線路圖、量測與車型適用性。
