"""Render ZPL to PDF with the Labelary API (https://labelary.com/service.html)."""

import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid

log = logging.getLogger("labelary")

ENABLED = os.environ.get("LABELARY", "true").strip().lower() in ("1", "true", "yes", "on")
URL = os.environ.get("LABELARY_URL", "https://api.labelary.com").rstrip("/")
DPMM = os.environ.get("ZPL_DPMM", "8")
if DPMM not in ("6", "8", "12", "24"):
    log.warning("ZPL_DPMM must be 6, 8, 12 or 24; using 8")
    DPMM = "8"

# Free-plan limits: 3 requests/second, 50 labels and 1 MB per request.
MIN_INTERVAL = 0.35
MAX_LABELS = 50
MAX_BYTES = 900_000
MAX_LABEL_INCHES = 15

_lock = threading.Lock()
_last_request = 0.0


class LabelaryError(Exception):
    pass


def render(zpl, width_in, height_in):
    """Returns (pdf_bytes_list, warnings). Raises LabelaryError if rendering fails."""
    width_in = min(width_in, MAX_LABEL_INCHES)
    height_in = min(height_in, MAX_LABEL_INCHES)
    pdfs, warnings = [], []
    for batch in _batches(zpl):
        pdf, batch_warnings = _request(batch, width_in, height_in)
        pdfs.append(pdf)
        warnings += batch_warnings
    return pdfs, warnings


def _batches(zpl):
    """Only split streams of independent label formats. Never silently drop setup commands."""
    if len(zpl) <= MAX_BYTES and zpl.upper().count(b"^XA") <= MAX_LABELS:
        return [zpl]
    reason = "ZPL exceeds one Labelary request and cannot be split safely"
    # Conservative allowlist for independent labels; downloaded/stored formats, graphics
    # and configuration changes can carry state across requests.
    allowed = set(b"XA XZ FO FT FD FS FH CF A0 GB GC GD GE BY BC BQ BX".split())
    commands = re.finditer(rb"\^(.{2})", zpl, re.S)
    if b"~" in zpl or any(m[1].upper() not in allowed for m in commands):
        raise LabelaryError(reason)
    batches, current, count, end = [], bytearray(), 0, 0
    start = None
    for token in re.finditer(rb"\^(XA|XZ)", zpl, re.I):
        if token[1].upper() == b'XA':
            if start is not None or zpl[end:token.start()].strip():
                raise LabelaryError(reason)
            start = token.start()
            continue
        if start is None or token.end() - start > MAX_BYTES:
            raise LabelaryError(reason)
        label = zpl[start:token.end()]
        start = None
        if count == MAX_LABELS or len(current) + len(label) > MAX_BYTES:
            batches.append(bytes(current))
            current.clear()
            count = 0
        current.extend(label)
        count += 1
        end = token.end()
    if start is not None or not current or zpl[end:].strip():
        raise LabelaryError(reason)
    return [*batches, bytes(current)]


def _request(zpl, width_in, height_in):
    global _last_request
    url = f"{URL}/v1/printers/{DPMM}dpmm/labels/{width_in:g}x{height_in:g}/"
    boundary = uuid.uuid4().hex
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="label.zpl"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n").encode() + zpl + f"\r\n--{boundary}--\r\n".encode()
    headers = {
        "Accept": "application/pdf",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "X-Linter": "On",
        "User-Agent": "fake-ipp-printer",
    }
    for attempt in range(4):
        with _lock:  # stay under the per-second limit, across all jobs
            wait = _last_request + MIN_INTERVAL - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _last_request = time.monotonic()
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read(), _parse_warnings(resp.headers.get("X-Warnings", ""))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace").strip()[:300]
            if e.code == 429 and attempt < 3:
                log.info("rate limited by Labelary, retrying")
                time.sleep(2 ** attempt)
                continue
            raise LabelaryError(f"Labelary returned HTTP {e.code}: {detail or e.reason}")
        except (urllib.error.URLError, OSError) as e:
            raise LabelaryError(f"Could not reach Labelary: {getattr(e, 'reason', e)}")
    raise LabelaryError("Labelary rate limit: too many requests")


def _parse_warnings(header):
    """X-Warnings is pipe-delimited: byte index|byte size|command|parameter|message, repeated."""
    parts = header.split("|") if header else []
    warnings = []
    for i in range(0, len(parts) - 4, 5):
        _, _, command, param, message = parts[i:i + 5]
        where = f"{command} parameter {param}" if command and param else command
        warnings.append(f"{where}: {message}" if where else message)
    return warnings
