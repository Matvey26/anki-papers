"""Sync failure categories shared by the adapter and queue."""

class AuthenticationError(RuntimeError):
    pass


class RetryableSyncError(RuntimeError):
    pass


class PermanentSyncError(RuntimeError):
    pass
