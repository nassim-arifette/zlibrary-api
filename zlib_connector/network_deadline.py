"""Absolute connected-socket deadlines shared by remote and local HTTP clients."""
import socket
import threading
import time


class ReadDeadline:
    """Interrupt a socket even while http.client reads internal lines/trailers.

    A socket timeout limits individual reads, not repeated reads inside a header
    or chunk parser. One short-lived guard per response enforces the existing
    absolute deadline. Cancellation is checked at most 50 ms apart; no socket
    operation is retried, and the guard is stopped and joined before cleanup.
    """

    def __init__(self, sock, deadline, cancel):
        self._socket, self._deadline, self._cancel = sock, deadline, cancel
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._socket is not None:
            self._thread = threading.Thread(target=self._run, name="zlib-transfer-deadline", daemon=True)
            self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            remaining = self._deadline - time.monotonic()
            if remaining <= 0 or (self._cancel is not None and self._cancel.is_cancelled):
                try:
                    if isinstance(self._socket, socket.socket):
                        # Use the socket operation directly: SSLSocket.shutdown
                        # also mutates TLS parser state in the reading thread.
                        socket.socket.shutdown(self._socket, socket.SHUT_RDWR)
                    else:
                        self._socket.shutdown(socket.SHUT_RDWR)
                except (OSError, AttributeError):
                    pass  # The response may already have closed its socket.
                return
            self._stop.wait(min(remaining, 0.05) if self._cancel is not None else remaining)

    def close(self):
        self._stop.set()
        if self._thread is not None and self._thread.ident is not None:
            self._thread.join()
