ALTER TABLE extracted_periods
    ADD COLUMN dialogs_total INT NOT NULL DEFAULT 0 AFTER month,
    ADD COLUMN dialogs_processed INT NOT NULL DEFAULT 0 AFTER dialogs_total;
