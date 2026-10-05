-- 2009 Honda Civic 1.8L 車輛歷史資料庫
-- 相容 MySQL 8.x 與常見 MariaDB 版本

CREATE DATABASE IF NOT EXISTS vehicle_diagnostics
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE vehicle_diagnostics;

CREATE TABLE IF NOT EXISTS vehicles (
  vehicle_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  vehicle_key VARCHAR(64) NOT NULL,
  vehicle_scope VARCHAR(16) NOT NULL DEFAULT 'fleet',
  make VARCHAR(40) NOT NULL,
  model VARCHAR(40) NOT NULL,
  model_year SMALLINT UNSIGNED NOT NULL,
  engine VARCHAR(40) NOT NULL,
  transmission VARCHAR(40) NULL,
  vin_masked VARCHAR(32) NULL,
  plate_masked VARCHAR(32) NULL,
  current_odometer_km INT UNSIGNED NULL,
  in_service_date DATE NULL,
  active TINYINT(1) NOT NULL DEFAULT 1,
  data_origin VARCHAR(20) NOT NULL DEFAULT 'simulated',
  notes TEXT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (vehicle_id),
  UNIQUE KEY uq_vehicles_vehicle_key (vehicle_key),
  KEY idx_vehicles_model (model_year, make, model, engine),
  KEY idx_vehicles_scope (vehicle_scope)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS maintenance_packages (
  package_code VARCHAR(20) NOT NULL,
  package_name_zh VARCHAR(80) NOT NULL,
  interval_km INT UNSIGNED NOT NULL,
  interval_months SMALLINT UNSIGNED NULL,
  package_priority SMALLINT UNSIGNED NOT NULL DEFAULT 10,
  includes_lower_packages TINYINT(1) NOT NULL DEFAULT 1,
  rule_type VARCHAR(24) NOT NULL DEFAULT 'demo_fixed_mileage',
  applicable_vehicle VARCHAR(120) NOT NULL DEFAULT '2009 Honda Civic 1.8L',
  active TINYINT(1) NOT NULL DEFAULT 1,
  disclaimer VARCHAR(255) NOT NULL,
  PRIMARY KEY (package_code),
  KEY idx_packages_interval (interval_km, active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS maintenance_package_items (
  item_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  package_code VARCHAR(20) NOT NULL,
  item_order SMALLINT UNSIGNED NOT NULL DEFAULT 1,
  system_domain VARCHAR(24) NOT NULL,
  item_name_zh VARCHAR(120) NOT NULL,
  action_type VARCHAR(20) NOT NULL DEFAULT 'inspect',
  normal_result VARCHAR(255) NULL,
  abnormal_action VARCHAR(255) NULL,
  mandatory TINYINT(1) NOT NULL DEFAULT 1,
  PRIMARY KEY (item_id),
  UNIQUE KEY uq_package_item (package_code, item_name_zh),
  CONSTRAINT fk_package_items_package
    FOREIGN KEY (package_code) REFERENCES maintenance_packages(package_code)
    ON UPDATE CASCADE ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS service_records (
  service_record_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  vehicle_id BIGINT UNSIGNED NOT NULL,
  service_date DATE NOT NULL,
  odometer_km INT UNSIGNED NOT NULL,
  record_type VARCHAR(24) NOT NULL,
  system_domain VARCHAR(24) NOT NULL,
  package_code VARCHAR(20) NULL,
  work_summary TEXT NOT NULL,
  inspection_findings TEXT NULL,
  parts_or_fluids TEXT NULL,
  next_due_km INT UNSIGNED NULL,
  next_due_date DATE NULL,
  workshop_name VARCHAR(100) NULL,
  data_origin VARCHAR(20) NOT NULL DEFAULT 'simulated',
  source_note VARCHAR(255) NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (service_record_id),
  KEY idx_service_vehicle_date (vehicle_id, service_date),
  KEY idx_service_vehicle_km (vehicle_id, odometer_km),
  KEY idx_service_domain (system_domain),
  CONSTRAINT fk_service_vehicle
    FOREIGN KEY (vehicle_id) REFERENCES vehicles(vehicle_id)
    ON UPDATE CASCADE ON DELETE RESTRICT,
  CONSTRAINT fk_service_package
    FOREIGN KEY (package_code) REFERENCES maintenance_packages(package_code)
    ON UPDATE CASCADE ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS repair_cases (
  repair_case_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  case_key VARCHAR(64) NOT NULL,
  vehicle_id BIGINT UNSIGNED NOT NULL,
  case_date DATE NOT NULL,
  odometer_km INT UNSIGNED NOT NULL,
  system_domain VARCHAR(24) NOT NULL,
  owner_complaint TEXT NOT NULL,
  occurrence_conditions TEXT NULL,
  dtc_codes VARCHAR(255) NULL,
  pid_evidence TEXT NULL,
  confirmed_root_cause TEXT NULL,
  inspection_process TEXT NULL,
  repair_action TEXT NULL,
  parts_replaced TEXT NULL,
  verification_result TEXT NULL,
  safety_disposition VARCHAR(120) NULL,
  resolution_status VARCHAR(24) NOT NULL DEFAULT 'confirmed',
  data_origin VARCHAR(20) NOT NULL DEFAULT 'simulated',
  source_note VARCHAR(255) NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (repair_case_id),
  UNIQUE KEY uq_repair_case_key (case_key),
  KEY idx_repair_vehicle_date (vehicle_id, case_date),
  KEY idx_repair_domain (system_domain),
  KEY idx_repair_odometer (odometer_km),
  KEY idx_repair_model_search (vehicle_id, system_domain, case_date),
  CONSTRAINT fk_repair_vehicle
    FOREIGN KEY (vehicle_id) REFERENCES vehicles(vehicle_id)
    ON UPDATE CASCADE ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE OR REPLACE VIEW v_fixed_vehicle_timeline AS
SELECT
  v.vehicle_key,
  v.model_year,
  v.make,
  v.model,
  v.engine,
  s.service_date AS event_date,
  s.odometer_km,
  'service' AS event_type,
  s.system_domain,
  s.work_summary AS event_summary,
  s.data_origin
FROM vehicles v
JOIN service_records s ON s.vehicle_id = v.vehicle_id
WHERE v.vehicle_scope = 'fixed'
UNION ALL
SELECT
  v.vehicle_key,
  v.model_year,
  v.make,
  v.model,
  v.engine,
  r.case_date AS event_date,
  r.odometer_km,
  'repair' AS event_type,
  r.system_domain,
  CONCAT(r.owner_complaint, '｜', COALESCE(r.confirmed_root_cause, '原因未確認')) AS event_summary,
  r.data_origin
FROM vehicles v
JOIN repair_cases r ON r.vehicle_id = v.vehicle_id
WHERE v.vehicle_scope = 'fixed';

