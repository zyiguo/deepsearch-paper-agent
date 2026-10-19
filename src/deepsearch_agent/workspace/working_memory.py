"""Bound, compact Markdown working memory; independent of retrieval and LLMs."""
import hashlib
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import RLock
from uuid import UUID, uuid4


DOCUMENTS = {'plan': ('PLAN.md', '# Plan\n\n## Pending\n'),
             'history': ('HISTORY.md', '# History\n\n## Completed\n\n## User Preferences\n')}
MAX_CONTENT_CHARS = 2000


class WorkingMemoryError(ValueError):
    """Public error codes only; never expose filesystem paths."""


class WorkingMemoryStore:
    def __init__(self, root, *, session_exists=None):
        self.root = Path(root).resolve()
        self.session_exists = session_exists or (lambda _: True)
        self.lock = RLock()
        self.generations = {}

    def _directory(self, session_id):
        try:
            if str(UUID(session_id)) != session_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise WorkingMemoryError('memory_invalid_scope') from None
        directory = self.root / session_id
        if directory.resolve() != directory or directory.is_symlink() or directory.is_junction():
            raise WorkingMemoryError('memory_unsafe_path')
        return directory

    def _path(self, session_id, document):
        if document not in DOCUMENTS:
            raise WorkingMemoryError('memory_invalid_document')
        path = self._directory(session_id) / DOCUMENTS[document][0]
        if path.resolve() != path or path.is_symlink() or path.is_junction():
            raise WorkingMemoryError('memory_unsafe_path')
        return path

    def bind(self, session_id):
        with self.lock:
            self._directory(session_id)
            if not self.session_exists(session_id):
                raise WorkingMemoryError('memory_scope_expired')
            generation = self.generations.setdefault(session_id, uuid4().hex)
            return BoundWorkingMemory(self, session_id, generation)

    def clear(self, session_id):
        with self.lock:
            directory = self._directory(session_id)
            # Revoke handles first, including when cleanup later encounters an IO error.
            self.generations[session_id] = uuid4().hex
            for document in DOCUMENTS:
                self._path(session_id, document).unlink(missing_ok=True)
            if directory.exists() and not any(directory.iterdir()):
                directory.rmdir()

    @staticmethod
    def _write(path, content):
        temporary = None
        try:
            with NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                                    dir=path.parent, suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


class BoundWorkingMemory:
    def __init__(self, store, session_id, generation):
        self.store, self.session_id, self.generation = store, session_id, generation
        self.read_revisions = {}

    def _check(self):
        if (self.store.generations.get(self.session_id) != self.generation
                or not self.store.session_exists(self.session_id)):
            raise WorkingMemoryError('memory_scope_expired')

    def _load(self, document):
        self._check()
        path = self.store._path(self.session_id, document)
        path.parent.mkdir(parents=True, exist_ok=True)
        for name, (_, initial) in DOCUMENTS.items():
            target = self.store._path(self.session_id, name)
            if not target.exists():
                self.store._write(target, initial)
        if path.stat().st_size > MAX_CONTENT_CHARS * 4:
            raise WorkingMemoryError('memory_too_large')
        content = path.read_text(encoding='utf-8')
        if len(content) > MAX_CONTENT_CHARS:
            raise WorkingMemoryError('memory_too_large')
        return {'document': document, 'content': content,
                'revision': hashlib.sha256(content.encode('utf-8')).hexdigest(),
                'trust': 'working_notes_not_evidence'}

    def read(self, document):
        with self.store.lock:
            value = self._load(document)
            self.read_revisions[document] = value['revision']
            return value

    def replace(self, document, content, expected_revision):
        with self.store.lock:
            current = self._load(document)
            if self.read_revisions.get(document) != expected_revision:
                raise WorkingMemoryError('memory_read_required')
            if current['revision'] != expected_revision:
                raise WorkingMemoryError('memory_conflict')
            if not isinstance(content, str) or len(content) > MAX_CONTENT_CHARS:
                raise WorkingMemoryError('memory_too_large')
            self.store._write(self.store._path(self.session_id, document), content)
            self.read_revisions.pop(document, None)
            # A write acknowledgement is not a new read authorization.
            return {'document': document, 'updated': True,
                    'revision': hashlib.sha256(content.encode('utf-8')).hexdigest(),
                    'trust': 'working_notes_not_evidence'}
