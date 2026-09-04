-- Apply explicitly to the dedicated sensitive-word database before app startup.
CREATE TABLE IF NOT EXISTS sensitive_word_versions (
  scope VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
  version BIGINT UNSIGNED NOT NULL DEFAULT 0
) ENGINE=InnoDB;
INSERT IGNORE INTO sensitive_word_versions (scope, version) VALUES ('agent', 0);
CREATE TABLE IF NOT EXISTS sensitive_word_categories (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  scope VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  name VARCHAR(128) NOT NULL,
  normalized_hash BINARY(32) NOT NULL,
  status TINYINT UNSIGNED NOT NULL DEFAULT 1,
  source_system VARCHAR(128) COLLATE utf8mb4_bin NULL,
  source_id VARCHAR(191) COLLATE utf8mb4_bin NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  deleted_at DATETIME(6) NULL,
  live_hash BINARY(32) GENERATED ALWAYS AS (IF(deleted_at IS NULL, normalized_hash, NULL)) STORED,
  UNIQUE KEY category_name_unique (scope, live_hash),
  UNIQUE KEY category_source_unique (scope, source_system, source_id),
  KEY category_scope_status (scope, status, deleted_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS sensitive_words (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  scope VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  category_id BIGINT UNSIGNED NOT NULL,
  word VARCHAR(255) NOT NULL,
  normalized_word VARCHAR(1020) NOT NULL,
  normalized_hash BINARY(32) NOT NULL,
  status TINYINT UNSIGNED NOT NULL DEFAULT 1,
  source_system VARCHAR(128) COLLATE utf8mb4_bin NULL,
  source_id VARCHAR(191) COLLATE utf8mb4_bin NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  deleted_at DATETIME(6) NULL,
  live_hash BINARY(32) GENERATED ALWAYS AS (IF(deleted_at IS NULL, normalized_hash, NULL)) STORED,
  UNIQUE KEY word_unique (scope, category_id, live_hash),
  UNIQUE KEY word_source_unique (scope, source_system, source_id),
  KEY word_scope_category_status (scope, category_id, status, deleted_at),
  CONSTRAINT sensitive_word_category_fk FOREIGN KEY (category_id) REFERENCES sensitive_word_categories(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
