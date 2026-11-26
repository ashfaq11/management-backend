-- Reusable shift templates and a therapist's weekly shift assignment.
-- day_of_week uses ISO numbering: Monday=0 through Sunday=6.
CREATE TABLE shifts (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  spa_id BIGINT NOT NULL REFERENCES spas(id),
  name TEXT NOT NULL CHECK (length(btrim(name)) > 0),
  start_time TIME NOT NULL,
  end_time TIME NOT NULL,
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (start_time < end_time)
);
CREATE UNIQUE INDEX shifts_spa_name_idx ON shifts (spa_id, lower(name));

CREATE TABLE therapist_shift_assignments (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  spa_id BIGINT NOT NULL REFERENCES spas(id),
  therapist_id BIGINT NOT NULL REFERENCES therapists(id),
  shift_id BIGINT NOT NULL REFERENCES shifts(id),
  day_of_week SMALLINT NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (spa_id, therapist_id, day_of_week)
);
CREATE INDEX therapist_shift_assignments_therapist_idx
  ON therapist_shift_assignments (spa_id, therapist_id, day_of_week);

COMMENT ON TABLE shifts IS 'Reusable daily work-time window for a spa.';
COMMENT ON COLUMN shifts.start_time IS 'Local shift start time; shifts do not cross midnight.';
COMMENT ON COLUMN shifts.end_time IS 'Local shift end time; must be later than start_time.';
COMMENT ON TABLE therapist_shift_assignments IS 'Weekly therapist roster; one shift per weekday. Monday=0 through Sunday=6.';
COMMENT ON COLUMN therapist_shift_assignments.day_of_week IS 'ISO weekday number: Monday 0, Tuesday 1, ..., Sunday 6.';
