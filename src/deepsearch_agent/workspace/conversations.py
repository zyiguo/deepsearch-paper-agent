import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from deepsearch_agent.core.answers import Answer


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def serialize_answer(answer):
    result = {'text':answer.text, 'sources':[{'citation_id':i, **asdict(source)}
              for i, source in enumerate(answer.sources, start=1)]}
    if getattr(answer, 'skills_used', None):
        result['skills_used'] = answer.skills_used
    return result


def stored_turn(row):
    def decoded(value):
        return json.loads(value) if isinstance(value, str) else value
    return {'id':row['id'], 'question':row['question'], 'answer':decoded(row['answer_json']),
            'created_at':row['created_at'], 'attachments':decoded(row['attachments_json'])}


class ConversationStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    question TEXT NOT NULL,
                    answer_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                );

                CREATE INDEX IF NOT EXISTS idx_turns_session
                ON turns(session_id, id);

                CREATE TABLE IF NOT EXISTS session_memories (
                    session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
                    revision INTEGER NOT NULL,
                    payload TEXT NOT NULL
                );
            """)
            columns = {row['name'] for row in db.execute('PRAGMA table_info(turns)')}
            if 'attachments_json' not in columns:
                db.execute("ALTER TABLE turns ADD COLUMN attachments_json TEXT NOT NULL DEFAULT '[]'")
            if 'run_id' not in columns:
                db.execute('ALTER TABLE turns ADD COLUMN run_id TEXT')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_turns_run ON turns(run_id) WHERE run_id IS NOT NULL')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")

        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, title: str) -> dict:
        title = title.strip()

        if not title or len(title) > 100:
            raise ValueError("会话名称须为 1 到 100 个字符")

        now = utc_now()
        session = {
            "id": str(uuid4()),
            "title": title,
            "created_at": now,
            "updated_at": now,
        }

        with self._db() as db:
            db.execute(
                """
                INSERT INTO sessions
                (id, title, created_at, updated_at)
                VALUES (:id, :title, :created_at, :updated_at)
                """,
                session,
            )

        return session

    def list_sessions(self) -> list[dict]:
        with self._db() as db:
            rows = db.execute(
                """
                SELECT id, title, created_at, updated_at
                FROM sessions
                ORDER BY updated_at DESC, id
                """
            ).fetchall()

        return [dict(row) for row in rows]

    def find_turn(self, run_id, session_id):
        with self._db() as db:
            row = db.execute('SELECT * FROM turns WHERE run_id=? AND session_id=?',
                             (run_id, session_id)).fetchone()
        return stored_turn(row) if row else None

    def delete(self, session_id: str) -> None:
        with self._db() as db:
            db.execute('DELETE FROM turns WHERE session_id = ?', (session_id,))
            result = db.execute('DELETE FROM sessions WHERE id = ?', (session_id,))
            if result.rowcount == 0:
                raise KeyError('会话不存在')

    def get(self, session_id: str) -> dict:
        with self._db() as db:
            session = db.execute(
                "SELECT * FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()

            if session is None:
                raise KeyError("会话不存在")

            rows = db.execute(
                """
                SELECT id, question, answer_json, created_at, attachments_json
                FROM turns
                WHERE session_id = ?
                ORDER BY id
                """,
                (session_id,),
            ).fetchall()

        return {
            **dict(session),
            "turns": [
                {
                    "id": row["id"],
                    "question": row["question"],
                    "answer": json.loads(row["answer_json"]),
                    "created_at": row["created_at"],
                    "attachments": json.loads(row['attachments_json']),
                }
                for row in rows
            ],
        }

    def append_turn(
        self,
        session_id: str,
        question: str,
        answer: Answer,
        attachments: list[dict] | None = None,
        run_id: str | None = None,
    ) -> dict:
        now = utc_now()
        answer_data = serialize_answer(answer)
        with self._db() as db:
            if run_id is not None:
                previous = db.execute('SELECT id, session_id, question, answer_json, created_at, attachments_json '
                                      'FROM turns WHERE run_id=?', (run_id,)).fetchone()
                if previous is not None:
                    if previous['session_id'] != session_id:
                        raise ValueError('run_id belongs to another session')
                    return {'id': previous['id'], 'question': previous['question'],
                            'answer': json.loads(previous['answer_json']), 'created_at': previous['created_at'],
                            'attachments': json.loads(previous['attachments_json'])}
            exists = db.execute(
                "SELECT id FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()

            if exists is None:
                raise KeyError("会话不存在")

            cursor = db.execute(
                """
                INSERT INTO turns
                (session_id, question, answer_json, created_at, attachments_json, run_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    question,
                    json.dumps(answer_data, ensure_ascii=False),
                    now,
                    json.dumps(attachments or [], ensure_ascii=False),
                    run_id,
                ),
            )

            db.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )

            turn_id = cursor.lastrowid

        return {
            "id": turn_id,
            "question": question,
            "answer": answer_data,
            "created_at": now,
            "attachments": attachments or [],
        }

    def initialize_memory(self, session_id, payload):
        with self._db() as db:
            db.execute('INSERT OR IGNORE INTO session_memories VALUES (?,?,?)', (session_id,0,payload))
            return db.execute('SELECT payload FROM session_memories WHERE session_id=?', (session_id,)).fetchone()['payload']

    def compare_and_set_memory(self, session_id, revision, payload, expected):
        with self._db() as db:
            result = db.execute('UPDATE session_memories SET revision=?,payload=? WHERE session_id=? AND revision=?',
                                (revision,payload,session_id,expected))
        return result.rowcount == 1
