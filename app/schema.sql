CREATE TABLE IF NOT EXISTS shows (
 id uuid PRIMARY KEY, name text NOT NULL, price_paise bigint NOT NULL CHECK(price_paise >= 0),
 per_user_limit integer NOT NULL CHECK(per_user_limit > 0)
);
CREATE TABLE IF NOT EXISTS reservations (
 id uuid PRIMARY KEY, show_id uuid NOT NULL REFERENCES shows(id), user_id text NOT NULL,
 seats text[] NOT NULL, amount_paise bigint NOT NULL CHECK(amount_paise >= 0),
 status text NOT NULL CHECK(status IN ('confirmed','cancelled')),
 idempotency_key text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(user_id, idempotency_key), UNIQUE(id, show_id)
);
CREATE INDEX IF NOT EXISTS reservations_user_show ON reservations(user_id,show_id) WHERE status='confirmed';
CREATE TABLE IF NOT EXISTS seats (
 show_id uuid NOT NULL REFERENCES shows(id), label text NOT NULL,
 reservation_id uuid, PRIMARY KEY(show_id,label),
 FOREIGN KEY(reservation_id,show_id) REFERENCES reservations(id,show_id)
);
CREATE TABLE IF NOT EXISTS outcomes (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 reason text NOT NULL CHECK(reason IN ('seat_taken','per_user_limit','idempotent_replay','idempotency_conflict','unknown_seat')),
 created_at timestamptz NOT NULL DEFAULT now()
);
