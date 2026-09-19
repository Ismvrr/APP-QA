CREATE TABLE IF NOT EXISTS analysis_batches (
    id INT AUTO_INCREMENT PRIMARY KEY,
    company_id INT NOT NULL,
    client_prompt_id INT NULL,
    year INT NOT NULL,
    month INT NOT NULL,
    status ENUM('pending', 'running', 'completed', 'error') DEFAULT 'pending',
    total_conversations INT NOT NULL DEFAULT 0,
    processed_conversations INT NOT NULL DEFAULT 0,
    failed_conversations INT NOT NULL DEFAULT 0,
    total_tokens INT NOT NULL DEFAULT 0,
    consolidate TINYINT(1) NOT NULL DEFAULT 0,
    consolidation_result LONGTEXT NULL,
    error_message TEXT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP NULL,
    completed_at TIMESTAMP NULL,
    FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE,
    INDEX idx_batch_company (company_id, year, month),
    INDEX idx_batch_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

ALTER TABLE analysis_jobs
    ADD COLUMN analysis_batch_id INT NULL AFTER client_prompt_id,
    ADD INDEX idx_analysis_batch (analysis_batch_id);
