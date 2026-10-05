USE vehicle_diagnostics;

-- 所有資料均為系統展示用模擬資料，不代表這台實車已實際執行過這些維修。

INSERT INTO vehicles
  (vehicle_key, vehicle_scope, make, model, model_year, engine, transmission,
   vin_masked, plate_masked, current_odometer_km, in_service_date, data_origin, notes)
VALUES
  ('CIVIC2009_FIXED', 'fixed', 'Honda', 'Civic', 2009, '1.8L', '5AT',
   'SIM-FIXED-0001', 'NPUST-02', 186420, '2009-08-15', 'simulated',
   '固定診斷車輛；目前內容為模擬履歷，正式使用前應逐筆改為真實紀錄。'),
  ('CIVIC2009_FLEET_01', 'fleet', 'Honda', 'Civic', 2009, '1.8L', '5AT', 'SIM-FLEET-0001', NULL, 162300, '2009-03-12', 'simulated', '同型車案例樣本'),
  ('CIVIC2009_FLEET_02', 'fleet', 'Honda', 'Civic', 2009, '1.8L', '5AT', 'SIM-FLEET-0002', NULL, 211880, '2009-05-20', 'simulated', '同型車案例樣本'),
  ('CIVIC2009_FLEET_03', 'fleet', 'Honda', 'Civic', 2009, '1.8L', '5AT', 'SIM-FLEET-0003', NULL, 143500, '2009-07-02', 'simulated', '同型車案例樣本'),
  ('CIVIC2009_FLEET_04', 'fleet', 'Honda', 'Civic', 2009, '1.8L', '5AT', 'SIM-FLEET-0004', NULL, 198200, '2009-01-25', 'simulated', '同型車案例樣本'),
  ('CIVIC2009_FLEET_05', 'fleet', 'Honda', 'Civic', 2009, '1.8L', '5AT', 'SIM-FLEET-0005', NULL, 176900, '2009-09-10', 'simulated', '同型車案例樣本'),
  ('CIVIC2009_FLEET_06', 'fleet', 'Honda', 'Civic', 2009, '1.8L', '5AT', 'SIM-FLEET-0006', NULL, 224600, '2009-11-18', 'simulated', '同型車案例樣本'),
  ('CIVIC2009_FLEET_07', 'fleet', 'Honda', 'Civic', 2009, '1.8L', '5AT', 'SIM-FLEET-0007', NULL, 154300, '2009-06-21', 'simulated', '同型車案例樣本'),
  ('CIVIC2009_FLEET_08', 'fleet', 'Honda', 'Civic', 2009, '1.8L', '5AT', 'SIM-FLEET-0008', NULL, 189700, '2009-04-08', 'simulated', '同型車案例樣本')
ON DUPLICATE KEY UPDATE
  current_odometer_km = VALUES(current_odometer_km),
  notes = VALUES(notes);

INSERT INTO maintenance_packages
  (package_code, package_name_zh, interval_km, interval_months, package_priority,
   includes_lower_packages, rule_type, disclaimer)
VALUES
  ('PKG_005K', '5,000公里基礎保養', 5000, 6, 5, 1, 'demo_fixed_mileage', '系統自訂示範套餐；實際項目須依原廠手冊、使用環境及車況確認。'),
  ('PKG_010K', '10,000公里定期保養', 10000, 12, 10, 1, 'demo_fixed_mileage', '系統自訂示範套餐；實際項目須依原廠手冊、使用環境及車況確認。'),
  ('PKG_020K', '20,000公里進階保養', 20000, 24, 20, 1, 'demo_fixed_mileage', '系統自訂示範套餐；實際項目須依原廠手冊、使用環境及車況確認。'),
  ('PKG_040K', '40,000公里大保養', 40000, 24, 40, 1, 'demo_fixed_mileage', '系統自訂示範套餐；油品與週期須依原廠規格、使用環境及車況確認。'),
  ('PKG_080K', '80,000公里擴充保養', 80000, 48, 80, 1, 'demo_fixed_mileage', '系統自訂示範套餐；不得取代原廠 Maintenance Minder 或技師檢查。'),
  ('PKG_100K', '100,000公里重點保養', 100000, 60, 100, 1, 'demo_fixed_mileage', '系統自訂示範套餐；火星塞、冷卻液及汽門間隙須按原廠資料與實測決定。')
ON DUPLICATE KEY UPDATE
  package_name_zh = VALUES(package_name_zh),
  interval_months = VALUES(interval_months),
  package_priority = VALUES(package_priority),
  disclaimer = VALUES(disclaimer);

INSERT INTO maintenance_package_items
  (package_code, item_order, system_domain, item_name_zh, action_type, normal_result, abnormal_action, mandatory)
VALUES
  ('PKG_005K', 1, 'powertrain', '引擎機油與機油濾芯', 'replace', '油量與油壓正常，無明顯洩漏', '查明消耗或洩漏原因後使用符合規格的油品', 1),
  ('PKG_005K', 2, 'safety', '燈光、雨刷與胎壓快速檢查', 'inspect', '功能正常且胎壓符合車門標籤', '依檢查結果修復，輪胎結構異常時停止使用', 1),
  ('PKG_010K', 1, 'chassis', '四輪輪胎磨耗與換位評估', 'inspect', '磨耗均勻，無鼓包或露線', '先排除定位、懸吊或胎壓問題再換位或更換', 1),
  ('PKG_010K', 2, 'brake', '煞車片、碟盤及煞車液外觀', 'inspect', '厚度與液位正常，無洩漏', '量測厚度與漏點，確認異常後維修', 1),
  ('PKG_010K', 3, 'electrical', '電瓶靜態電壓與充電電壓', 'measure', '依電瓶狀態與發電系統規格判定', '進行負載測試、壓降與發電機輸出檢查', 1),
  ('PKG_020K', 1, 'powertrain', '引擎空氣濾芯', 'inspect_replace', '濾芯無過度阻塞或破損', '確認進氣箱密封後更換符合規格濾芯', 1),
  ('PKG_020K', 2, 'cooling_hvac', '冷氣濾網與出風量', 'inspect_replace', '濾網乾淨且出風量正常', '更換濾網並檢查鼓風機與風道', 1),
  ('PKG_020K', 3, 'chassis', '轉向與底盤接頭防塵套', 'inspect', '無鬆動、破裂或異常間隙', '定位故障部位並量測後維修', 1),
  ('PKG_040K', 1, 'powertrain', '自動變速箱油狀態與更換條件', 'inspect_replace', '油量、顏色及換檔狀態正常', '確認油品規格與故障狀態後處理，不以換油掩蓋故障', 1),
  ('PKG_040K', 2, 'brake', '煞車液含水狀態與更換週期', 'measure_replace', '含水量與使用時間符合規範', '依量測及時間更換並正確排氣', 1),
  ('PKG_040K', 3, 'cooling_hvac', '冷卻液液位、冰點與洩漏檢查', 'measure', '液位穩定且無洩漏', '冷車加壓測試並查明漏點', 1),
  ('PKG_080K', 1, 'electrical', '啟動與充電系統完整測試', 'measure', '啟動壓降、充電輸出與接地正常', '分段量測電源及接地壓降', 1),
  ('PKG_080K', 2, 'chassis', '避震器、控制臂襯套與輪軸承', 'inspect', '無滲漏、鬆動或異音', '架車定位異常間隙後維修', 1),
  ('PKG_080K', 3, 'powertrain', '附件皮帶及張力器', 'inspect', '無裂紋、異音或張力異常', '確認磨耗或軸承異常後更換', 1),
  ('PKG_100K', 1, 'powertrain', '火星塞與點火線圈狀態', 'inspect_replace', '火星塞磨耗與線圈輸出正常', '依失火量測與原廠規格處理', 1),
  ('PKG_100K', 2, 'cooling_hvac', '冷卻液更換與冷卻系統檢漏', 'replace_measure', '系統無洩漏、風扇及節溫控制正常', '先修復漏點或控制故障再更換冷卻液', 1),
  ('PKG_100K', 3, 'powertrain', '汽門間隙檢查條件評估', 'inspect', '冷車間隙與運轉聲響符合規格', '依原廠程序量測後才調整', 1)
ON DUPLICATE KEY UPDATE
  item_order = VALUES(item_order),
  action_type = VALUES(action_type),
  normal_result = VALUES(normal_result),
  abnormal_action = VALUES(abnormal_action),
  mandatory = VALUES(mandatory);

INSERT INTO service_records
  (vehicle_id, service_date, odometer_km, record_type, system_domain, package_code,
   work_summary, inspection_findings, parts_or_fluids, next_due_km, next_due_date,
   workshop_name, data_origin, source_note)
SELECT v.vehicle_id, x.service_date, x.odometer_km, x.record_type, x.system_domain,
       x.package_code, x.work_summary, x.inspection_findings, x.parts_or_fluids,
       x.next_due_km, x.next_due_date, '示範保養廠', 'simulated', '模擬固定車輛歷史紀錄'
FROM vehicles v
JOIN (
  SELECT '2010-02-15' service_date, 5000 odometer_km, 'maintenance' record_type, 'powertrain' system_domain, 'PKG_005K' package_code, '更換引擎機油與機油濾芯' work_summary, '未見明顯油液洩漏' inspection_findings, '機油、機油濾芯' parts_or_fluids, 10000 next_due_km, '2010-08-15' next_due_date
  UNION ALL SELECT '2011-01-08', 20000, 'maintenance', 'multi', 'PKG_020K', '完成20,000公里示範套餐', '煞車片與輪胎磨耗正常，空氣濾芯較髒', '機油、機油濾芯、引擎空氣濾芯', 25000, '2011-07-08'
  UNION ALL SELECT '2013-03-19', 42000, 'maintenance', 'multi', 'PKG_040K', '完成40,000公里示範套餐', '煞車液使用時間已到，變速箱油顏色偏深但無金屬屑', '機油、濾芯、煞車液、自動變速箱油', 45000, '2013-09-19'
  UNION ALL SELECT '2015-06-11', 68500, 'repair', 'electrical', NULL, '更換12V電瓶並清潔端子', '冷車啟動轉速偏慢，電瓶負載測試未通過；充電電壓正常', '12V電瓶、端子保護劑', 78500, '2016-06-11'
  UNION ALL SELECT '2016-08-03', 82300, 'maintenance', 'multi', 'PKG_080K', '完成80,000公里示範套餐', '附件皮帶有輕微龜裂；前控制臂襯套外觀老化但無明顯間隙', '機油、濾芯、附件皮帶', 90000, '2017-08-03'
  UNION ALL SELECT '2018-04-27', 105600, 'maintenance', 'multi', 'PKG_100K', '完成100,000公里重點保養', '冷卻系統無外漏；火星塞電極磨耗達更換條件', '火星塞、冷卻液、機油、濾芯', 110000, '2018-10-27'
  UNION ALL SELECT '2019-10-14', 126900, 'repair', 'brake', NULL, '更換前煞車片並量測碟盤', '前煞車片接近磨耗極限，碟盤厚度仍在可用範圍', '前煞車片', 136900, '2020-10-14'
  UNION ALL SELECT '2021-02-22', 145300, 'repair', 'chassis', NULL, '更換右前輪軸承', '車速增加時右前輪有連續低頻嗡聲，架車量測確認軸承異常', '右前輪軸承', 155300, '2022-02-22'
  UNION ALL SELECT '2022-07-09', 158400, 'repair', 'cooling_hvac', NULL, '更換冷氣濾網並清潔鼓風機葉輪', '出風量偏小，濾網阻塞，鼓風機電流與各檔位正常', '冷氣濾網', 178400, '2024-07-09'
  UNION ALL SELECT '2023-11-18', 171200, 'maintenance', 'multi', 'PKG_010K', '完成定期保養與電瓶檢測', '充電系統正常，電瓶健康度下降但仍可啟動', '機油、機油濾芯', 180000, '2024-05-18'
  UNION ALL SELECT '2025-03-06', 181050, 'repair', 'powertrain', NULL, '更換第二缸點火線圈並複測', '曾出現第二缸失火；交換線圈後失火跟隨，確認線圈異常', '第二缸點火線圈', 186000, '2025-09-06'
  UNION ALL SELECT '2026-05-16', 185700, 'maintenance', 'multi', 'PKG_005K', '更換機油與濾芯並進行安全快速檢查', '未見明顯漏油；輪胎無鼓包，煞車液位正常', '機油、機油濾芯', 190000, '2026-11-16'
) x
WHERE v.vehicle_key = 'CIVIC2009_FIXED'
  AND NOT EXISTS (
    SELECT 1 FROM service_records s
    WHERE s.vehicle_id = v.vehicle_id
      AND s.service_date = x.service_date
      AND s.odometer_km = x.odometer_km
      AND s.work_summary = x.work_summary
  );

INSERT INTO repair_cases
  (case_key, vehicle_id, case_date, odometer_km, system_domain, owner_complaint,
   occurrence_conditions, dtc_codes, pid_evidence, confirmed_root_cause,
   inspection_process, repair_action, parts_replaced, verification_result,
   safety_disposition, resolution_status, data_origin, source_note)
SELECT x.case_key, v.vehicle_id, x.case_date, x.odometer_km, x.system_domain,
       x.owner_complaint, x.occurrence_conditions, x.dtc_codes, x.pid_evidence,
       x.confirmed_root_cause, x.inspection_process, x.repair_action,
       x.parts_replaced, x.verification_result, x.safety_disposition,
       'confirmed', 'simulated', '模擬案例；只能作為相似案例，不能直接確認本次故障'
FROM vehicles v
JOIN (
  SELECT 'FIXED-R001' case_key, 'CIVIC2009_FIXED' vehicle_key, '2025-03-06' case_date, 181050 odometer_km, 'powertrain' system_domain, '怠速抖動且加速偶爾頓挫' owner_complaint, '暖車後怠速與輕負載較明顯' occurrence_conditions, 'P0302' dtc_codes, '第二缸失火計數增加，燃油修正未見持續大幅偏正' pid_evidence, '第二缸點火線圈內部異常' confirmed_root_cause, '檢查火星塞後交換第一、二缸線圈，失火位置跟隨線圈移動' inspection_process, '更換確認異常的點火線圈' repair_action, '點火線圈' parts_replaced, '清碼後冷熱車怠速與負載測試，P0302及症狀未再出現' verification_result, '維修前避免高速及重負載，故障燈閃爍時停止行駛' safety_disposition
  UNION ALL SELECT 'FIXED-R002', 'CIVIC2009_FIXED', '2015-06-11', 68500, 'electrical', '冷車啟動無力', '停放兩天後較明顯', 'P0562', '啟動瞬間系統電壓降至規範以下，發動後充電電壓正常', '12V電瓶老化且端子接觸電阻偏高', '電瓶負載測試、啟動壓降與端子壓降測試', '更換電瓶並清潔鎖緊端子', '12V電瓶', '連續三次冷啟動正常，低電壓碼未再出現', '完成前避免反覆啟動，無法穩定啟動時安排救援' 
  UNION ALL SELECT 'FLEET-R001', 'CIVIC2009_FLEET_01', '2024-05-08', 151200, 'powertrain', '怠速不穩並偶爾熄火', '冷車較正常，熱車停等較明顯', 'P0171', '短期與長期燃油修正持續偏正，節氣門小開度時最明顯', '進氣歧管後方真空軟管龜裂漏氣', '煙霧測漏確認漏點，夾閉軟管後燃油修正回落', '更換龜裂真空軟管並確認固定', '真空軟管', '熱車怠速穩定，燃油修正恢復合理範圍', '可短程低速前往維修；若頻繁熄火則拖吊'
  UNION ALL SELECT 'FLEET-R002', 'CIVIC2009_FLEET_02', '2023-10-21', 203700, 'powertrain', '引擎故障燈亮但動力大致正常', '長途行駛後亮燈', 'P0420', '上下游氧感知器波形相似度偏高，燃油修正無明顯異常', '觸媒轉換效率低於門檻', '先排除排氣洩漏、失火與空燃比問題，再進行觸媒效率測試', '更換經量測確認異常的觸媒總成', '觸媒轉換器', '完成監視器行駛循環後故障碼未再出現', '無閃爍故障燈或過熱時可短程低負載行駛'
  UNION ALL SELECT 'FLEET-R003', 'CIVIC2009_FLEET_03', '2025-01-12', 139800, 'electrical', '間歇無法啟動且儀表燈變暗', '高溫停車後再啟動較明顯', 'P0562', '啟動時正極線壓降偏高，電瓶負載測試正常', '電瓶正極端子氧化造成高電阻', '量測電瓶、啟動電流及正負極壓降', '清潔端子並更換受損正極接頭', '電瓶正極接頭', '熱浸後連續啟動測試正常', '無法可靠啟動時不建議上路'
  UNION ALL SELECT 'FLEET-R004', 'CIVIC2009_FLEET_04', '2024-08-17', 187600, 'cooling_hvac', '水溫偏高且冷氣停等時變不冷', '低速塞車時發生，高速行駛較正常', 'P0480', '水溫上升時風扇命令存在但風扇未轉', '散熱風扇繼電器接點燒蝕', '確認風扇可直供運轉，量測繼電器控制及輸出', '更換確認異常的風扇繼電器並複測', '散熱風扇繼電器', '怠速開冷氣與高溫控制測試均正常', '過熱時立即停止行駛並拖吊'
  UNION ALL SELECT 'FLEET-R005', 'CIVIC2009_FLEET_05', '2023-06-03', 163400, 'brake', '煞車時方向盤抖動', '高速煞車時明顯', NULL, 'OBD無動力系統故障碼；煞車碟盤端面偏擺超出規格', '前碟盤厚度變化與端面偏擺異常', '量測碟盤厚度、偏擺、輪轂接觸面及懸吊間隙', '更換前碟盤與煞車片並清潔輪轂接觸面', '前碟盤、前煞車片', '分段磨合及安全路試後抖動消失', '避免高速行駛並盡快維修'
  UNION ALL SELECT 'FLEET-R006', 'CIVIC2009_FLEET_06', '2022-12-09', 216900, 'chassis', '車速增加時持續嗡聲', '左右轉向時音量改變', NULL, '輪速資料一致，右前輪軸承負載改變時噪音明顯', '右前輪軸承磨耗', '架車檢查間隙並使用聽診器比較四輪軸承', '更換右前輪軸承並確認輪轂', '右前輪軸承', '路試各速度區間異音消失', '若出現明顯間隙或劇烈噪音則停止行駛'
  UNION ALL SELECT 'FLEET-R007', 'CIVIC2009_FLEET_07', '2025-04-28', 149600, 'powertrain', '冷車啟動後抖動且故障燈閃爍', '雨天或高濕度後較常出現', 'P0301', '第一缸失火計數快速增加，其餘缸正常', '第一缸點火線圈絕緣劣化', '火星塞外觀正常，交換線圈後失火跟隨到另一缸', '更換第一缸點火線圈', '點火線圈', '冷啟動與負載測試後無失火', '故障燈閃爍時立即停止行駛'
  UNION ALL SELECT 'FLEET-R008', 'CIVIC2009_FLEET_08', '2024-02-16', 181100, 'cooling_hvac', '暖車很慢且暖氣不夠熱', '冬季及高速行駛時明顯', 'P0128', '冷卻液溫度長時間低於預期且高速時下降', '節溫器卡在開啟位置', '冷車確認液位後比較上下水管溫升並查核ECT合理性', '更換節溫器並正確排除冷卻系統空氣', '節溫器、冷卻液', '暖車時間與水溫控制恢復正常', '無過熱時可短程行駛，但應儘快維修'
  UNION ALL SELECT 'FLEET-R009', 'CIVIC2009_FLEET_01', '2022-09-24', 133500, 'electrical', '引擎偶爾熄火且多個警示燈同時亮', '顛簸路面後較常發生', 'U0100', '故障時ECM供電瞬間中斷，CAN線路終端電阻正常', 'ECM主繼電器接點間歇不良', '監測ECM電源、接地與CAN波形，震動測試重現供電中斷', '更換主繼電器並修復鬆動端子', '主繼電器、端子', '震動與路試後通訊未再中斷', '可能熄火，不建議繼續行駛'
  UNION ALL SELECT 'FLEET-R010', 'CIVIC2009_FLEET_02', '2021-11-05', 184200, 'powertrain', '油耗增加且怠速略粗糙', '熱車後持續', 'P0135', '前氧感知器加熱器電流為零，加熱器供電正常', '前氧感知器加熱器內部斷路', '量測保險絲、供電、接地與感知器加熱器電阻', '更換經電阻量測確認異常的前氧感知器', '前氧感知器', '閉迴路時間恢復且故障碼未再出現', '一般可短程行駛，仍應儘快維修'
  UNION ALL SELECT 'FLEET-R011', 'CIVIC2009_FLEET_03', '2023-03-13', 124900, 'brake', 'ABS與VSA燈間歇亮起', '雨天或洗車後較常發生', NULL, '右後輪速訊號偶爾瞬間歸零，其餘輪速正常', '右後輪速感知器接頭進水腐蝕', '檢查輪速波形、接頭拉力與線束，發現端子綠鏽', '修復接頭並完成防水處理', '輪速感知器接頭端子', '輪速同步且警示燈未再出現', '基本煞車仍在但ABS/VSA可能失效，應低速前往維修'
  UNION ALL SELECT 'FLEET-R012', 'CIVIC2009_FLEET_04', '2022-05-19', 169300, 'chassis', '方向盤回正不順且車輛偏右', '更換輪胎後仍存在', NULL, '胎壓正常；右前外傾與前束偏離設定值', '右前控制臂後襯套龜裂產生位移', '檢查輪胎、煞車拖滯、底盤間隙後進行定位量測', '更換確認異常的控制臂襯套並重新定位', '右前控制臂襯套', '定位值與路試回正正常', '避免高速行駛，轉向不穩加劇時停止行駛'
  UNION ALL SELECT 'FLEET-R013', 'CIVIC2009_FLEET_05', '2024-12-07', 171800, 'cooling_hvac', '冷氣有時冷有時不冷', '高溫怠速時較明顯', NULL, '壓縮機命令存在但離合器線圈熱態電阻異常升高', '壓縮機離合器線圈熱衰退', '確認冷媒壓力合理、供電與接地正常後量測線圈冷熱態電阻', '更換確認異常的離合器線圈總成', '壓縮機離合器線圈', '高溫怠速30分鐘冷氣維持正常', '不影響基本行駛，但需注意除霧能力'
  UNION ALL SELECT 'FLEET-R014', 'CIVIC2009_FLEET_06', '2025-02-14', 221400, 'powertrain', '加速無力並伴隨排氣異味', '高速負載時較明顯', 'P0300,P0420', '多缸失火且觸媒溫度異常升高', '老化火星塞造成失火，長期失火導致觸媒受損', '先確認壓縮與燃油，再檢查火星塞、線圈及觸媒背壓', '更換火星塞並在確認觸媒損壞後更換觸媒', '火星塞、觸媒轉換器', '負載測試無失火且監視器完成', '故障燈閃爍或觸媒過熱時立即停止行駛'
  UNION ALL SELECT 'FLEET-R015', 'CIVIC2009_FLEET_07', '2023-08-30', 137200, 'electrical', '夜間燈光忽明忽暗', '怠速開冷氣及大燈時明顯', 'P0562', '負載下充電電壓偏低且發電機交流紋波過高', '發電機整流二極體異常', '檢查皮帶、電瓶、正負極壓降、充電電流及交流紋波', '更換確認異常的發電機', '發電機', '各負載下充電電壓與紋波正常', '可能突然無法啟動或熄火，應儘快維修'
  UNION ALL SELECT 'FLEET-R016', 'CIVIC2009_FLEET_08', '2021-04-11', 146600, 'safety', '行駛中聞到汽油味', '加滿油後最明顯', 'P0455', '蒸發排放系統煙霧測試於油箱上方管路大量洩漏', '蒸發排放軟管龜裂', '停車通風、確認無明顯液態燃油後執行低壓煙霧測漏', '更換龜裂軟管並固定', '蒸發排放軟管', '煙霧測試無洩漏且汽油味消失', '燃油氣味明顯時停止行駛、遠離火源並拖吊'
) x ON x.vehicle_key = v.vehicle_key
ON DUPLICATE KEY UPDATE
  owner_complaint = VALUES(owner_complaint),
  occurrence_conditions = VALUES(occurrence_conditions),
  dtc_codes = VALUES(dtc_codes),
  pid_evidence = VALUES(pid_evidence),
  confirmed_root_cause = VALUES(confirmed_root_cause),
  inspection_process = VALUES(inspection_process),
  repair_action = VALUES(repair_action),
  parts_replaced = VALUES(parts_replaced),
  verification_result = VALUES(verification_result),
  safety_disposition = VALUES(safety_disposition);

