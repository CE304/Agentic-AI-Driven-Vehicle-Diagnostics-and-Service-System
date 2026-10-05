# RAG 資料來源說明

此資料夾僅收錄本專案可公開之文字型索引、測試資料與來源說明。

## 未隨 GitHub 公開的資料

Honda Owner Manual、Workshop/Service Manual 等可能受著作權保護的原始 PDF **不包含在本 repository**。部署者應自行取得具有合法使用權的文件，放入本機 RAG 來源目錄後再執行 `ingest_rag.py` 建立索引。

這樣可避免把第三方受著作權保護文件重新散布到 GitHub，同時保留 RAG 建庫程式、資料格式與檢索流程的可重現性。
