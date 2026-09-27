-- LPQ_VISION - schema.sql (the sealed DDL, verbatim). Applied ONCE by brain/db/init.py,
-- gated on the existence of the sites table. No IF NOT EXISTS here: the gate lives in init.py.

CREATE TABLE sites (
  site            TEXT PRIMARY KEY,
  ingestion       TEXT NOT NULL,             -- 'phone_web' | 'pi_csi' | 'usb'
  telegram_chat_id TEXT,
  active          BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE menu_dishes (
  dish_id      TEXT PRIMARY KEY,
  nombre       TEXT NOT NULL,
  plate_type   TEXT,
  componentes  JSONB NOT NULL,               -- [{nombre, porcion_g}]
  contable     JSONB,
  activo       BOOLEAN NOT NULL DEFAULT TRUE,
  menu_version INT NOT NULL DEFAULT 1
);

CREATE TABLE site_dish_photos (
  site       TEXT NOT NULL REFERENCES sites(site),
  dish_id    TEXT NOT NULL REFERENCES menu_dishes(dish_id),
  photo_path TEXT NOT NULL,
  condition  TEXT NOT NULL DEFAULT 'normal', -- 'normal' | 'lampara'
  PRIMARY KEY (site, dish_id, photo_path)
);

CREATE TABLE plates (
  id              BIGSERIAL PRIMARY KEY,
  site            TEXT NOT NULL REFERENCES sites(site),
  camera          TEXT,
  record_type     TEXT NOT NULL,             -- 'return' | 'outgoing'
  ts              TIMESTAMPTZ NOT NULL,      -- now() at INSERT, server clock (SPEC 1.3)
  photo_path      TEXT NOT NULL UNIQUE,      -- relative to the photos root: <site>/<yyyymmdd>/<uuid>.jpg; UNIQUE = the byte-dedup lock (SPEC 1.3)
  burst_id        TEXT,
  dish_predicted  TEXT,
  dish_verified   TEXT,
  leftovers       JSONB,
  presentation    JSONB,
  scale           JSONB,
  pairing         JSONB,
  confidence      TEXT,                      -- 'alta' | 'media' | 'baja'
  model           TEXT NOT NULL,
  prompt_version  TEXT NOT NULL,
  menu_version    INT NOT NULL,
  validator       JSONB,
  review_status   TEXT NOT NULL DEFAULT 'unreviewed',
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_plates_site_ts ON plates (site, ts);
CREATE INDEX idx_plates_dish    ON plates (dish_predicted);
CREATE INDEX idx_plates_review  ON plates (review_status) WHERE review_status = 'unreviewed';

CREATE TABLE jobs (
  id          BIGSERIAL PRIMARY KEY,
  plate_id    BIGINT NOT NULL REFERENCES plates(id),
  kind        TEXT NOT NULL,                 -- 'analyze' | 'presentation'
  priority    INT NOT NULL DEFAULT 100,
  status      TEXT NOT NULL DEFAULT 'pending',
  run_after   TIMESTAMPTZ NOT NULL DEFAULT now(),
  attempts    INT NOT NULL DEFAULT 0,
  last_error  TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at  TIMESTAMPTZ,
  finished_at TIMESTAMPTZ
);
CREATE INDEX idx_jobs_claim ON jobs (status, run_after, priority, created_at);
