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
    """Split into requests within the label-count and size limits. Anything before the first ^XA
    (e.g. ~DG graphic downloads) is repeated in each batch so later labels can still use it."""
    labels = re.findall(rb"\^XA.*?(?:\^XZ|$)", zpl, flags=re.S | re.I)
    if len(labels) <= MAX_LABELS and len(zpl) <= MAX_BYTES:
        return [zpl]
    start = re.search(rb"\^XA", zpl, flags=re.I)
    preamble = zpl[:start.start()] if start else b""
    batches, current = [], []
    for label in labels:
        size = len(preamble) + sum(map(len, current)) + len(label)
        if current and (len(current) >= MAX_LABELS or size > MAX_BYTES):
            batches.append(preamble + b"".join(current))
            current = []
        current.append(label)
    if current:
        batches.append(preamble + b"".join(current))
    return batches


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
