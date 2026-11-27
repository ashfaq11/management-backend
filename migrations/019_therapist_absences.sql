-- Therapist leave: only absences are recorded; days present = scheduled shift days minus absences.
CREATE TABLE therapist_absences (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  spa_id BIGINT NOT NULL REFERENCES spas(id),
  therapist_id BIGINT NOT NULL REFERENCES therapists(id),
  on_date DATE NOT NULL,
  kind TEXT NOT NULL DEFAULT 'leave' CHECK (kind IN ('leave','sick','absent','holiday')),
  half_day BOOLEAN NOT NULL DEFAULT false,
  note TEXT,
  created_by BIGINT REFERENCES users(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (therapist_id, on_date)
);
CREATE INDEX therapist_absences_spa_date_idx ON therapist_absences (spa_id, on_date);
COMMENT ON TABLE therapist_absences IS 'Days a therapist was away; attendance counts scheduled shift days minus these.';
COMMENT ON COLUMN therapist_absences.id IS 'Identity.';
COMMENT ON COLUMN therapist_absences.spa_id IS 'Owning spa.';
COMMENT ON COLUMN therapist_absences.therapist_id IS 'Therapist who was away.';
COMMENT ON COLUMN therapist_absences.on_date IS 'The day away.';
COMMENT ON COLUMN therapist_absences.kind IS 'leave, sick, absent (no notice) or holiday.';
COMMENT ON COLUMN therapist_absences.half_day IS 'Away for half the day (counts 0.5).';
COMMENT ON COLUMN therapist_absences.note IS 'Optional reason.';
COMMENT ON COLUMN therapist_absences.created_by IS 'Login that recorded it.';
COMMENT ON COLUMN therapist_absences.created_at IS 'When it was recorded.';
