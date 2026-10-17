-- Prototype jobs were confirmed before server captions existed. Never publish them
-- automatically when the first executable publisher is installed.
UPDATE jobs SET state='needs_review',error_code='PUBLICATION_RECONFIRM_REQUIRED',
error_message='Создайте новый предпросмотр: старое задание не содержит подтверждённой подписи.',
revision=revision+1
WHERE plan_json='{}' AND state NOT IN ('completed','failed','cancelled');
