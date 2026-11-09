"""In-memory download adapter for existing protocol fixtures."""
import time

from zlib_connector.transfer import TransferProgress


class MemoryDownloadTransport:
    def download_to(self, url, target, *, progress=None, cancel=None):
        if cancel is not None:
            cancel.check()
        started = time.monotonic()
        data = self.request(url, download=True).body
        received = 0
        if progress is not None:
            progress(TransferProgress(0, len(data), time.monotonic() - started))
        for offset in range(0, len(data), 65536):
            if cancel is not None:
                cancel.check()
            chunk = data[offset:offset + 65536]
            target.write(chunk)
            received += len(chunk)
            if progress is not None:
                progress(TransferProgress(received, len(data), time.monotonic() - started))
        if cancel is not None:
            cancel.check()
        return received
