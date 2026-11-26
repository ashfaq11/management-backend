-- Descriptions stored in the database itself (visible in psql \d+, pgAdmin, DBeaver) and used to build docs/schema.md.
COMMENT ON TABLE users IS 'Staff logins. Role admin = everything; staff = customers, onboarding, appointments.';
COMMENT ON COLUMN users.username IS 'Login name, at least 3 characters; unique ignoring case (users_username_key).';
COMMENT ON COLUMN users.password_hash IS 'PBKDF2-SHA256 hash "pbkdf2$iterations$salt$hash"; the password itself is never stored.';
COMMENT ON COLUMN users.role IS 'admin or staff.';
COMMENT ON COLUMN users.is_active IS 'False blocks login and ends all sessions immediately; accounts are deactivated, never deleted.';

COMMENT ON TABLE sessions IS 'Login sessions as opaque bearer tokens. Only the SHA-256 fingerprint of a token is stored, so a database leak cannot be replayed.';
COMMENT ON COLUMN sessions.token_hash IS 'SHA-256 hex of the token handed to the browser.';
COMMENT ON COLUMN sessions.expires_at IS 'Sessions last 12 hours; expired rows are swept at the next login.';

COMMENT ON TABLE customers IS 'Spa clients, created by onboarding or by hand.';
COMMENT ON COLUMN customers.phone IS 'Unique across customers (customers_phone_key); how reception finds a returning client.';
COMMENT ON COLUMN customers.gender IS 'Free text; the UI offers Female, Male, Other.';
COMMENT ON COLUMN customers.dob IS 'Date of birth (optional).';
COMMENT ON COLUMN customers.notes IS 'Allergies, skin type, preferences.';

COMMENT ON TABLE therapists IS 'Staff who perform services and are paid on payslips. Never deleted, only set inactive, so past payslips stay reproducible.';
COMMENT ON COLUMN therapists.fixed_salary IS 'Monthly fixed pay in INR; the base of every payslip.';
COMMENT ON COLUMN therapists.incentive_percent IS 'Percent (0-100) of the price of the therapist''s completed appointments in a month paid on top of the salary.';
COMMENT ON COLUMN therapists.is_active IS 'Inactive therapists cannot be booked and drop out of the monthly payroll list.';

COMMENT ON TABLE services IS 'Price list of treatments. Editing a price affects future bookings only.';
COMMENT ON COLUMN services.price IS 'List price in INR, copied onto an appointment when it is booked.';
COMMENT ON COLUMN services.duration_min IS 'Minutes a booking blocks on the therapist''s calendar.';
COMMENT ON COLUMN services.is_active IS 'Inactive services cannot be booked; history keeps pointing at them.';

COMMENT ON TABLE appointments IS 'One booking of one customer with one therapist for one service. Completed appointments drive revenue and incentive.';
COMMENT ON COLUMN appointments.start_at IS 'Spa-local wall-clock time (no time zone).';
COMMENT ON COLUMN appointments.duration_min IS 'Copied from the service at booking time.';
COMMENT ON COLUMN appointments.end_at IS 'Generated: start_at + duration_min. Used by the no-overlap constraint.';
COMMENT ON COLUMN appointments.price IS 'Amount agreed at booking in INR (list price unless overridden). Frozen: later price edits never change it. Payroll uses this.';
COMMENT ON COLUMN appointments.status IS 'booked -> completed | cancelled | no_show. Only completed counts for revenue and incentive; only booked can be edited.';
COMMENT ON CONSTRAINT appointments_no_overlap ON appointments IS 'A therapist cannot have two booked/completed appointments whose time ranges overlap (back-to-back is allowed). Enforced by the database so concurrent requests cannot double-book.';

-- Remaining columns (tests/test_schema_files.py fails if any table or column is left undocumented).
COMMENT ON COLUMN users.id IS 'Surrogate key.';
COMMENT ON COLUMN users.created_at IS 'When the account was created.';
COMMENT ON COLUMN sessions.user_id IS 'Owner of the session; deleting the user deletes their sessions.';
COMMENT ON COLUMN sessions.created_at IS 'When the user signed in.';
COMMENT ON COLUMN customers.id IS 'Surrogate key.';
COMMENT ON COLUMN customers.name IS 'Full name; must not be blank.';
COMMENT ON COLUMN customers.email IS 'Optional contact email.';
COMMENT ON COLUMN customers.address IS 'Optional postal address.';
COMMENT ON COLUMN customers.created_at IS 'Onboarding time; the "new customers" report counts by it in the spa time zone.';
COMMENT ON COLUMN customers.updated_at IS 'Last edit.';
COMMENT ON COLUMN therapists.id IS 'Surrogate key.';
COMMENT ON COLUMN therapists.name IS 'Name shown on bookings and printed on the payslip.';
COMMENT ON COLUMN therapists.phone IS 'Optional contact number.';
COMMENT ON COLUMN therapists.joined_on IS 'Optional joining date.';
COMMENT ON COLUMN therapists.created_at IS 'When the record was added.';
COMMENT ON COLUMN services.id IS 'Surrogate key.';
COMMENT ON COLUMN services.name IS 'Treatment name; unique ignoring case (services_name_key).';
COMMENT ON COLUMN appointments.id IS 'Surrogate key.';
COMMENT ON COLUMN appointments.customer_id IS 'Who is being treated. Customers are never deleted, so history stays intact.';
COMMENT ON COLUMN appointments.therapist_id IS 'Who performs it; also the key of the no-overlap constraint.';
COMMENT ON COLUMN appointments.service_id IS 'What is performed.';
COMMENT ON COLUMN appointments.notes IS 'Free-text note for this visit.';
COMMENT ON COLUMN appointments.created_at IS 'When it was booked.';
COMMENT ON COLUMN appointments.updated_at IS 'Last reschedule or status change.';
