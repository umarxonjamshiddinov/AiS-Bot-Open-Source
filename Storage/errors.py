class StorageError(RuntimeError):
    """Persistence failed. Callers must not grant access or silently lose writes."""
