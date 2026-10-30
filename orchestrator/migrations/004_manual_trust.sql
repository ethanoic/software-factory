-- Marks qualification rows that a human approved without measurement (`pool_worker trust`). Idempotent.
ALTER TABLE model_qualification ADD COLUMN IF NOT EXISTS manual boolean NOT NULL DEFAULT false;
