-- v2: 节律与运维（全部是投影/可重建，除了 quota 的已用量）

CREATE TABLE presence_kde (
  bin        INTEGER PRIMARY KEY,
  w          REAL NOT NULL,
  updated_ts INTEGER NOT NULL
);

CREATE TABLE changepoints (
  id  INTEGER PRIMARY KEY,
  ts  INTEGER NOT NULL,
  p   REAL NOT NULL,
  note TEXT
);

CREATE TABLE state_snapshot (
  ts      INTEGER PRIMARY KEY,
  phase   TEXT,
  s_user  REAL,
  s_node  REAL,
  backlog INTEGER,
  note    TEXT
);

CREATE TABLE backlog (
  kind      TEXT PRIMARY KEY,
  n         INTEGER NOT NULL,
  oldest_ts INTEGER
);

CREATE TABLE quota (
  day        TEXT NOT NULL,
  kind       TEXT NOT NULL,
  used       INTEGER NOT NULL DEFAULT 0,
  lim        INTEGER NOT NULL,
  PRIMARY KEY(day, kind)
);

CREATE TABLE jobs (
  id          TEXT PRIMARY KEY,
  kind        TEXT NOT NULL,
  payload     BLOB,
  created_ts  INTEGER NOT NULL,
  lease_until INTEGER,
  attempts    INTEGER NOT NULL DEFAULT 0,
  cursor      TEXT,
  status      TEXT NOT NULL,
  source      TEXT NOT NULL DEFAULT 'dream'
);
CREATE INDEX jobs_status ON jobs(status, lease_until);
