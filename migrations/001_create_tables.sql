-- API_C2D - Tablas del sistema
-- Fecha: 2026-07-16
-- Base de datos: configurar el nombre y host mediante variables de entorno

-- =============================================
-- 1. companies (del login-module)
-- =============================================
CREATE TABLE IF NOT EXISTS companies (
    id INT AUTO_INCREMENT PRIMARY KEY,
    remote_id BIGINT UNSIGNED NULL UNIQUE COMMENT 'ID remoto de Chat2Desk',
    company_id INT UNIQUE COMMENT 'ID local de Chat2Desk',
    name VARCHAR(255) NOT NULL,
    company_mode VARCHAR(50) NULL,
    lang VARCHAR(10) NULL,
    timezone VARCHAR(50) NULL,
    api_token TEXT NULL COMMENT 'Token C2D encriptado',
    status VARCHAR(20) DEFAULT 'active',
    isdeleted TINYINT(1) DEFAULT 0,
    subscription_type VARCHAR(50) NULL,
    subscription_addons TEXT NULL,
    subscription_agreement_status VARCHAR(50) NULL,
    notifications_phone VARCHAR(50) NULL,
    support_type VARCHAR(50) NULL,
    partner_id BIGINT UNSIGNED NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_remote_id (remote_id),
    INDEX idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- =============================================
-- 2. users (del login-module)
-- =============================================
CREATE TABLE IF NOT EXISTS users (
    id INT AUTO_INCREMENT PRIMARY KEY,
    company_id INT NULL,
    c2d_user_id INT NULL UNIQUE COMMENT 'ID de usuario en Chat2Desk',
    email VARCHAR(255) NOT NULL UNIQUE,
    first_name VARCHAR(100) NULL,
    last_name VARCHAR(100) NULL,
    phone VARCHAR(50) NULL,
    avatar TEXT NULL,
    password VARCHAR(255) NULL,
    role VARCHAR(30) NULL COMMENT 'admin, supervisor, operator',
    access_right_id INT NULL,
    auth_key TEXT NULL COMMENT 'Token dinámico para Iframe',
    last_visit TIMESTAMP NULL,
    status VARCHAR(20) DEFAULT 'enabled',
    isdeleted TINYINT(1) DEFAULT 0,
    remember_token VARCHAR(100) NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE SET NULL,
    INDEX idx_email (email),
    INDEX idx_role (role),
    INDEX idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- =============================================
-- 3. prompt_templates (plantillas de prompts)
-- =============================================
CREATE TABLE IF NOT EXISTS prompt_templates (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(100) NOT NULL COMMENT 'Nombre descriptivo (ej: Ads Hisparep)',
    type VARCHAR(50) NOT NULL COMMENT 'ads_analysis | bot_analysis | custom',
    description TEXT NULL,
    prompt_text TEXT NOT NULL COMMENT 'Texto del prompt para Gemini',
    is_active TINYINT(1) DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_type (type),
    INDEX idx_active (is_active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- =============================================
-- 4. client_prompts (configuración de prompts por cliente)
-- =============================================
CREATE TABLE IF NOT EXISTS client_prompts (
    id INT AUTO_INCREMENT PRIMARY KEY,
    company_id INT NOT NULL,
    template_id INT NULL COMMENT 'FK a prompt_templates (NULL si es personalizado)',
    name VARCHAR(100) NOT NULL,
    prompt_text TEXT NOT NULL COMMENT 'Prompt final (puede ser template modificado)',
    is_active TINYINT(1) DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE,
    FOREIGN KEY (template_id) REFERENCES prompt_templates(id) ON DELETE SET NULL,
    INDEX idx_company (company_id),
    INDEX idx_active (is_active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- =============================================
-- 5. mensajes_request (existente, recrear si no existe)
-- =============================================
CREATE TABLE IF NOT EXISTS mensajes_request (
    id INT AUTO_INCREMENT PRIMARY KEY,
    request_id VARCHAR(50) NOT NULL,
    dialog_id VARCHAR(50) NOT NULL,
    mensaje_id VARCHAR(50) NOT NULL,
    client_id VARCHAR(50) NULL,
    operator_id VARCHAR(50) NULL,
    tipo VARCHAR(20) NOT NULL COMMENT 'from_client | to_client | autoreply | system | comment',
    texto TEXT NULL,
    transport VARCHAR(50) NULL,
    fecha_creacion DATETIME NULL,
    UNIQUE KEY unique_mensaje (mensaje_id),
    INDEX idx_request (request_id),
    INDEX idx_dialog (dialog_id),
    INDEX idx_client (client_id),
    INDEX idx_tipo (tipo),
    INDEX idx_fecha (fecha_creacion)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- =============================================
-- 6. analysis_jobs (trabajos de análisis IA)
-- =============================================
CREATE TABLE IF NOT EXISTS analysis_jobs (
    id INT AUTO_INCREMENT PRIMARY KEY,
    company_id INT NOT NULL,
    request_id VARCHAR(50) NULL,
    client_prompt_id INT NULL COMMENT 'Prompt configurado para este análisis',
    year INT NOT NULL,
    month INT NOT NULL,
    status ENUM('pending', 'running', 'completed', 'error') DEFAULT 'pending',
    prompt1_result JSON NULL COMMENT 'Análisis por lead/conversación',
    prompt2_result JSON NULL COMMENT 'Reporte ejecutivo',
    gemini_tokens_used INT DEFAULT 0,
    error_message TEXT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP NULL,
    completed_at TIMESTAMP NULL,
    FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE,
    FOREIGN KEY (client_prompt_id) REFERENCES client_prompts(id) ON DELETE SET NULL,
    INDEX idx_company_period (company_id, year, month),
    INDEX idx_status (status),
    INDEX idx_request (request_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- =============================================
-- 7. extracted_periods (control de sincronización)
-- =============================================
CREATE TABLE IF NOT EXISTS extracted_periods (
    id INT AUTO_INCREMENT PRIMARY KEY,
    company_id INT NOT NULL,
    year INT NOT NULL,
    month INT NOT NULL,
    total_dialogs INT DEFAULT 0,
    total_messages INT DEFAULT 0,
    status ENUM('pending', 'extracting', 'completed', 'error') DEFAULT 'pending',
    error_message TEXT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP NULL,
    UNIQUE KEY unique_period (company_id, year, month),
    FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE,
    INDEX idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
