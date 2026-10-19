"""Transport-neutral application failures mapped by Web/CLI adapters."""
class ApplicationError(ValueError):
    def __init__(self, status_code, detail):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class PersistenceConflict(ValueError):
    """A duplicate persisted identity, independent of the database driver."""
