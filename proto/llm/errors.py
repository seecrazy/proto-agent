import socket


NETWORK_TIMEOUTS = (TimeoutError, socket.timeout)


class LLMError(Exception):
    """An LLM request or local dispatch failed."""


class ModelPressureWait(LLMError):
    def __init__(self, reason, retry_at=None):
        super().__init__(reason)
        self.reason = reason
        self.retry_at = retry_at


class ModelResponseError(LLMError):
    def __init__(self, reason, stage, http_status=None):
        super().__init__(reason)
        self.reason = reason
        self.stage = stage
        self.http_status = http_status


class ModelRequestStopped(LLMError):
    pass
