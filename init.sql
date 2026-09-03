-- Chat Workspace database schema (schema only; no user data, tokens, or provider keys).
-- Generated from the current MySQL schema. Configure credentials through environment variables.
CREATE DATABASE IF NOT EXISTS `chat_workspace` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE `chat_workspace`;
SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

CREATE TABLE IF NOT EXISTS `admin_audit_logs` (
  `id` varchar(36) COLLATE utf8mb4_unicode_ci NOT NULL,
  `admin_id` varchar(36) COLLATE utf8mb4_unicode_ci NOT NULL,
  `action` varchar(120) COLLATE utf8mb4_unicode_ci NOT NULL,
  `target_user_id` varchar(36) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `metadata_json` json DEFAULT NULL,
  `created_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  KEY `ix_admin_audit_logs_admin_id` (`admin_id`),
  KEY `ix_admin_audit_logs_target_user_id` (`target_user_id`),
  KEY `ix_admin_audit_logs_created_at` (`created_at`),
  CONSTRAINT `admin_audit_logs_ibfk_1` FOREIGN KEY (`admin_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE IF NOT EXISTS `alembic_version` (
  `version_num` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  PRIMARY KEY (`version_num`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE IF NOT EXISTS `assets` (
  `id` varchar(36) COLLATE utf8mb4_unicode_ci NOT NULL,
  `user_id` varchar(36) COLLATE utf8mb4_unicode_ci NOT NULL,
  `message_id` varchar(36) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `kind` varchar(30) COLLATE utf8mb4_unicode_ci NOT NULL,
  `storage_key` varchar(500) COLLATE utf8mb4_unicode_ci NOT NULL,
  `mime_type` varchar(120) COLLATE utf8mb4_unicode_ci NOT NULL,
  `size_bytes` int NOT NULL,
  `created_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  KEY `message_id` (`message_id`),
  KEY `ix_assets_user_id` (`user_id`),
  CONSTRAINT `assets_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE,
  CONSTRAINT `assets_ibfk_2` FOREIGN KEY (`message_id`) REFERENCES `messages` (`id`) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE IF NOT EXISTS `entitlements` (
  `id` varchar(36) COLLATE utf8mb4_unicode_ci NOT NULL,
  `user_id` varchar(36) COLLATE utf8mb4_unicode_ci NOT NULL,
  `starts_at` datetime NOT NULL,
  `expires_at` datetime NOT NULL,
  `status` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `granted_by` varchar(36) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `created_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  KEY `ix_entitlements_user_id` (`user_id`),
  KEY `ix_entitlements_status` (`status`),
  CONSTRAINT `entitlements_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE IF NOT EXISTS `exports` (
  `id` varchar(36) COLLATE utf8mb4_unicode_ci NOT NULL,
  `user_id` varchar(36) COLLATE utf8mb4_unicode_ci NOT NULL,
  `thread_id` varchar(36) COLLATE utf8mb4_unicode_ci NOT NULL,
  `format` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `status` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `storage_key` varchar(500) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `created_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  KEY `ix_exports_user_id` (`user_id`),
  KEY `ix_exports_thread_id` (`thread_id`),
  CONSTRAINT `exports_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE,
  CONSTRAINT `exports_ibfk_2` FOREIGN KEY (`thread_id`) REFERENCES `threads` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE IF NOT EXISTS `messages` (
  `id` varchar(36) COLLATE utf8mb4_unicode_ci NOT NULL,
  `thread_id` varchar(36) COLLATE utf8mb4_unicode_ci NOT NULL,
  `user_id` varchar(36) COLLATE utf8mb4_unicode_ci NOT NULL,
  `role` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `content` text COLLATE utf8mb4_unicode_ci NOT NULL,
  `content_type` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `sequence` int NOT NULL,
  `created_at` datetime NOT NULL,
  `content_json` json DEFAULT NULL,
  `tool_call_id` varchar(160) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `tool_name` varchar(120) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `asset_ids_json` json DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `ix_messages_thread_id` (`thread_id`),
  KEY `ix_messages_user_id` (`user_id`),
  KEY `ix_messages_tool_call_id` (`tool_call_id`),
  CONSTRAINT `messages_ibfk_1` FOREIGN KEY (`thread_id`) REFERENCES `threads` (`id`) ON DELETE CASCADE,
  CONSTRAINT `messages_ibfk_2` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE IF NOT EXISTS `model_channels` (
  `id` varchar(36) COLLATE utf8mb4_unicode_ci NOT NULL,
  `name` varchar(120) COLLATE utf8mb4_unicode_ci NOT NULL,
  `provider` varchar(80) COLLATE utf8mb4_unicode_ci NOT NULL,
  `protocol` varchar(30) COLLATE utf8mb4_unicode_ci NOT NULL,
  `base_url` varchar(500) COLLATE utf8mb4_unicode_ci NOT NULL,
  `api_key_encrypted` text COLLATE utf8mb4_unicode_ci NOT NULL,
  `modality` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `enabled` tinyint(1) NOT NULL,
  `priority` int NOT NULL,
  `models_json` json NOT NULL,
  `created_by` varchar(36) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `created_at` datetime NOT NULL,
  `updated_at` datetime NOT NULL,
  `capabilities_json` json DEFAULT NULL,
  `models_synced_at` datetime DEFAULT NULL,
  `last_sync_error` varchar(500) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `last_tested_at` datetime DEFAULT NULL,
  `last_test_ok` tinyint(1) DEFAULT NULL,
  `channel_type` varchar(20) COLLATE utf8mb4_unicode_ci,
  PRIMARY KEY (`id`),
  UNIQUE KEY `ix_model_channels_name` (`name`),
  KEY `ix_model_channels_enabled` (`enabled`),
  KEY `ix_model_channels_modality` (`modality`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE IF NOT EXISTS `model_requests` (
  `id` varchar(36) COLLATE utf8mb4_unicode_ci NOT NULL,
  `user_id` varchar(36) COLLATE utf8mb4_unicode_ci NOT NULL,
  `thread_id` varchar(36) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `message_id` varchar(36) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `channel_id` varchar(36) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `model` varchar(120) COLLATE utf8mb4_unicode_ci NOT NULL,
  `modality` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `status` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `input_tokens` int DEFAULT NULL,
  `output_tokens` int DEFAULT NULL,
  `provider_request_id` varchar(200) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `latency_ms` int DEFAULT NULL,
  `error_code` varchar(100) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `usage_json` json DEFAULT NULL,
  `created_at` datetime NOT NULL,
  `parent_request_id` varchar(36) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `turn_index` int DEFAULT '0',
  `idempotency_key` varchar(160) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `events_json` json DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `ux_model_requests_idempotency` (`user_id`,`thread_id`,`idempotency_key`,`modality`),
  KEY `message_id` (`message_id`),
  KEY `channel_id` (`channel_id`),
  KEY `ix_model_requests_thread_id` (`thread_id`),
  KEY `ix_model_requests_status` (`status`),
  KEY `ix_model_requests_user_id` (`user_id`),
  KEY `ix_model_requests_created_at` (`created_at`),
  KEY `ix_model_requests_parent_request_id` (`parent_request_id`),
  KEY `ix_model_requests_idempotency_key` (`idempotency_key`),
  CONSTRAINT `model_requests_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE,
  CONSTRAINT `model_requests_ibfk_2` FOREIGN KEY (`thread_id`) REFERENCES `threads` (`id`) ON DELETE SET NULL,
  CONSTRAINT `model_requests_ibfk_3` FOREIGN KEY (`message_id`) REFERENCES `messages` (`id`) ON DELETE SET NULL,
  CONSTRAINT `model_requests_ibfk_4` FOREIGN KEY (`channel_id`) REFERENCES `model_channels` (`id`) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE IF NOT EXISTS `projects` (
  `id` varchar(36) COLLATE utf8mb4_unicode_ci NOT NULL,
  `user_id` varchar(36) COLLATE utf8mb4_unicode_ci NOT NULL,
  `name` varchar(160) COLLATE utf8mb4_unicode_ci NOT NULL,
  `description` varchar(500) COLLATE utf8mb4_unicode_ci NOT NULL,
  `archived_at` datetime DEFAULT NULL,
  `created_at` datetime NOT NULL,
  `updated_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  KEY `ix_projects_user_id` (`user_id`),
  CONSTRAINT `projects_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE IF NOT EXISTS `refresh_tokens` (
  `id` varchar(36) COLLATE utf8mb4_unicode_ci NOT NULL,
  `user_id` varchar(36) COLLATE utf8mb4_unicode_ci NOT NULL,
  `token_hash` varchar(128) COLLATE utf8mb4_unicode_ci NOT NULL,
  `expires_at` datetime NOT NULL,
  `revoked_at` datetime DEFAULT NULL,
  `created_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `ix_refresh_tokens_token_hash` (`token_hash`),
  KEY `ix_refresh_tokens_user_id` (`user_id`),
  CONSTRAINT `refresh_tokens_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE IF NOT EXISTS `threads` (
  `id` varchar(36) COLLATE utf8mb4_unicode_ci NOT NULL,
  `user_id` varchar(36) COLLATE utf8mb4_unicode_ci NOT NULL,
  `project_id` varchar(36) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `title` varchar(200) COLLATE utf8mb4_unicode_ci NOT NULL,
  `model` varchar(120) COLLATE utf8mb4_unicode_ci NOT NULL,
  `status` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `archived_at` datetime DEFAULT NULL,
  `created_at` datetime NOT NULL,
  `updated_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  KEY `ix_threads_user_id` (`user_id`),
  KEY `ix_threads_status` (`status`),
  KEY `ix_threads_project_id` (`project_id`),
  CONSTRAINT `threads_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE,
  CONSTRAINT `threads_ibfk_2` FOREIGN KEY (`project_id`) REFERENCES `projects` (`id`) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE IF NOT EXISTS `users` (
  `id` varchar(36) COLLATE utf8mb4_unicode_ci NOT NULL,
  `email` varchar(320) COLLATE utf8mb4_unicode_ci NOT NULL,
  `display_name` varchar(120) COLLATE utf8mb4_unicode_ci NOT NULL,
  `password_hash` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `role` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `status` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `created_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `ix_users_email` (`email`),
  KEY `ix_users_role` (`role`),
  KEY `ix_users_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- The schema matches Alembic revision 0004_channel_type.
INSERT INTO `alembic_version` (`version_num`) VALUES ('0004_channel_type')
  ON DUPLICATE KEY UPDATE `version_num` = VALUES(`version_num`);
SET FOREIGN_KEY_CHECKS = 1;
