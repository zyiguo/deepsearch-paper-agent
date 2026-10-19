PRAGMA user_version=1;
BEGIN TRANSACTION;
CREATE TABLE events (
                    run_id TEXT NOT NULL REFERENCES runs(run_id), seq INTEGER NOT NULL,
                    body TEXT NOT NULL, PRIMARY KEY(run_id, seq));
INSERT INTO "events" VALUES('d7eb235a0f8e425dbf5b9a12a75a0f2d',0,'{"hash":"a0d29a9242a0861462bb1d697552a28a62801d6921c172572a25ad8c426bdccd","kind":"run.started","payload":{"question":"legacy fixture"},"previous_hash":"","run_id":"d7eb235a0f8e425dbf5b9a12a75a0f2d","seq":0,"timestamp":"2026-10-18T03:48:51.531128+00:00"}');
INSERT INTO "events" VALUES('d7eb235a0f8e425dbf5b9a12a75a0f2d',1,'{"hash":"1aad99bf3f2798cec5bd422cf79e9dbd0b131592250b0a4a663c0c1dfa4adfff","kind":"rag.outcome","payload":{"status":"verified"},"previous_hash":"a0d29a9242a0861462bb1d697552a28a62801d6921c172572a25ad8c426bdccd","run_id":"d7eb235a0f8e425dbf5b9a12a75a0f2d","seq":1,"timestamp":"2026-10-18T03:48:51.538651+00:00"}');
INSERT INTO "events" VALUES('d7eb235a0f8e425dbf5b9a12a75a0f2d',2,'{"hash":"df84770762ced5c587a742c1926068a0e79732d3eb69c91d1642b317862e1eeb","kind":"answer.final","payload":{"sources":[],"text":"fixture answer"},"previous_hash":"1aad99bf3f2798cec5bd422cf79e9dbd0b131592250b0a4a663c0c1dfa4adfff","run_id":"d7eb235a0f8e425dbf5b9a12a75a0f2d","seq":2,"timestamp":"2026-10-18T03:48:51.545712+00:00"}');
INSERT INTO "events" VALUES('d7eb235a0f8e425dbf5b9a12a75a0f2d',3,'{"hash":"7a46f6cd25369215e92b50844ef87ec6bdb4a6b152dcfe3acd0e31566b8a5c92","kind":"run.finished","payload":{"status":"completed"},"previous_hash":"df84770762ced5c587a742c1926068a0e79732d3eb69c91d1642b317862e1eeb","run_id":"d7eb235a0f8e425dbf5b9a12a75a0f2d","seq":3,"timestamp":"2026-10-18T03:48:51.552935+00:00"}');
CREATE TABLE runs (
                    run_id TEXT PRIMARY KEY, metadata TEXT NOT NULL, status TEXT NOT NULL);
INSERT INTO "runs" VALUES('d7eb235a0f8e425dbf5b9a12a75a0f2d','{"question":"legacy fixture"}','completed');
COMMIT;
