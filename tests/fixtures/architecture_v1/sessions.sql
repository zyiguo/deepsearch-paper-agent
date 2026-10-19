BEGIN TRANSACTION;
CREATE TABLE session_memories (
                    session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
                    revision INTEGER NOT NULL,
                    payload TEXT NOT NULL
                );
CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
INSERT INTO "sessions" VALUES('01cfd095-379b-4978-b093-476d7fa170a4','legacy fixture','2026-10-18T03:48:51.490943+00:00','2026-10-18T03:48:51.499460+00:00');
CREATE TABLE turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    question TEXT NOT NULL,
                    answer_json TEXT NOT NULL,
                    created_at TEXT NOT NULL, attachments_json TEXT NOT NULL DEFAULT '[]', run_id TEXT,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                );
INSERT INTO "turns" VALUES(1,'01cfd095-379b-4978-b093-476d7fa170a4','fixture question','{"text": "fixture answer [1]", "sources": [{"citation_id": 1, "source_path": "demo://papers/example.pdf", "page_number": 1, "text": "实验使用两张显卡。", "page_end": null, "section_path": [], "pages": [], "evidence_kind": "text", "visual_pages": [], "chunk_id": "", "content_id": "", "asset_ids": []}]}','2026-10-18T03:48:51.499460+00:00','[]',NULL);
CREATE INDEX idx_turns_session
                ON turns(session_id, id);
CREATE UNIQUE INDEX idx_turns_run ON turns(run_id) WHERE run_id IS NOT NULL;
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('turns',1);
COMMIT;
