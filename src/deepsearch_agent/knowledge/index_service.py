"""Own the mutable corpus, write protection and atomically published snapshots."""
from threading import RLock


class IndexService:
    def __init__(self, kb, index_path):
        self.kb = kb
        self.index_path = index_path
        self.lock = RLock()
        self.dirty = True
        self.write_blocked = False
        self.persistence_error = None
        self.on_publish = None

    def publish(self, candidate):
        """Caller holds lock; failed persistence never replaces the active corpus."""
        with self.lock:
            if self.write_blocked:
                raise ValueError('知识库写入受保护，请先修复快照')
            candidate.save(self.index_path)
            self.kb = candidate
            self.dirty = False
            self.persistence_error = None
            if self.on_publish is not None:
                self.on_publish(candidate)

    def save(self):
        with self.lock:
            if self.write_blocked:
                raise ValueError('知识库写入受保护，请先修复快照')
            try:
                self.kb.save(self.index_path)
            except (OSError, ValueError, TypeError) as error:
                self.dirty = True
                self.persistence_error = f'保存失败：{error}'
                raise
            self.dirty = False
            self.persistence_error = None

    def restore(self):
        with self.lock:
            try:
                self.kb.load(self.index_path)
            except (OSError, ValueError, KeyError, TypeError) as error:
                self.persistence_error = f'加载失败：{error}'
                self.write_blocked = True
                raise
            self.dirty = False
            self.write_blocked = False
            self.persistence_error = None
