"""Compatibility views for request consumers; index state has one owner."""
from starlette.datastructures import State


class RuntimeState(State):
    INDEX_FIELDS = frozenset({'kb', 'index_path', 'lock', 'dirty', 'write_blocked', 'persistence_error'})

    def __getattr__(self, name):
        index = self._state.get('index')
        if index is not None and name in self.INDEX_FIELDS:
            return getattr(index, name)
        return super().__getattr__(name)

    def __setattr__(self, name, value):
        index = self._state.get('index')
        if index is not None and name in self.INDEX_FIELDS:
            setattr(index, name, value)
            if name == 'kb' and index.on_publish is not None:
                index.on_publish(value)
        else:
            super().__setattr__(name, value)
