"""Cancellable plain-HTTP reads without restarting the standard HTTP parser."""

import http.client
import io
import select
import time


class _CancellableReader(io.RawIOBase):
    def __init__(self, raw, *, deadline, cancel):
        super().__init__()
        self._raw, self._deadline, self._cancel = raw, deadline, cancel

    def readable(self):
        return True

    def readinto(self, buffer):
        while True:
            self._cancel.check()
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            ready, _, _ = select.select([self._raw], [], [], min(0.05, remaining))
            self._cancel.check()
            if ready:
                # The raw socket is nonblocking: readiness can become stale,
                # but no buffered parser operation is restarted after a timeout.
                received = self._raw.readinto(buffer)
                if received is not None:
                    return received

    def close(self):
        try:
            self._raw.close()
        finally:
            super().close()


class _ReadableSocket:
    def __init__(self, sock, *, deadline, cancel):
        self._socket, self._deadline, self._cancel = sock, deadline, cancel

    def makefile(self, mode):
        # Retain a real makefile owner. HTTPConnection may close its own socket
        # after headers; this reader keeps the descriptor alive until body close.
        raw = self._socket.makefile(mode, buffering=0)
        try:
            self._socket.setblocking(False)
            return io.BufferedReader(_CancellableReader(raw, deadline=self._deadline, cancel=self._cancel))
        except BaseException:
            raw.close()
            raise


class CancellableResponse(http.client.HTTPResponse):
    """Keep stdlib HTTP framing while checking cancellation during header reads.

    On Windows, shutdown from another thread need not wake a pending makefile
    read. Readiness waits avoid that blocking call; ReadDeadline still guards the
    connection, and ownership remains with normal socket/file close operations.
    This adapter is for the plain loopback HTTP client, never TLS transports.
    """

    def __init__(self, sock, *args, deadline, cancel, **kwargs):
        super().__init__(_ReadableSocket(sock, deadline=deadline, cancel=cancel), *args, **kwargs)
