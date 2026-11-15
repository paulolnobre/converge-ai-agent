-- The period a day's times were filtered by (choose_day), so a slot taken
-- under the person sends them back to the same filter. Safe to repeat.
ALTER TABLE offers ADD COLUMN IF NOT EXISTS period VARCHAR(10);

-- A booking asked for before the contact details carries the days the person
-- named (booking:<day>:<period>:<d1>,<d2>,...): 60 characters held three.
-- Widening a VARCHAR rewrites nothing. Safe to repeat.
ALTER TABLE contacts ALTER COLUMN pending_action TYPE VARCHAR(255);
