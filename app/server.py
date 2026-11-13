"""Fake IPP printer: accepts jobs over IPP, renders them to PDF, shows them on a web dashboard."""

import asyncio
import gzip
import json
import logging
import math
import os
import shutil
import socket
import time
import uuid
import zlib
from dataclasses import asdict, dataclass, field
from pathlib import Path

from aiohttp import web

import convert
import ipp
import mdns

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("printer")


def env_bool(name, default):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


PRINTER_NAME = os.environ.get("PRINTER_NAME", "Fake IPP Printer")
PRINTER_LOCATION = os.environ.get("PRINTER_LOCATION", "Docker")
MAKE_AND_MODEL = os.environ.get("PRINTER_MAKE_MODEL", "Fake IPP Everywhere Printer")
SECONDS_PER_PAGE = float(os.environ.get("SECONDS_PER_PAGE", "2"))
IPP_PORT = int(os.environ.get("IPP_PORT", "631"))
WEB_PORT = int(os.environ.get("WEB_PORT", "8080"))
RAW_PORT = int(os.environ.get("RAW_PORT", "9100"))  # JetDirect/AppSocket raw printing; 0 disables
RAW_IDLE_TIMEOUT = float(os.environ.get("RAW_IDLE_TIMEOUT", "10"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
MDNS_ENABLED = env_bool("MDNS", True)
COLOR = env_bool("COLOR", True)
DUPLEX = env_bool("DUPLEX", True)
MEDIA_DEFAULT = os.environ.get("MEDIA_DEFAULT", "na_letter_8.5x11in")
RESOURCE_PATH = "/ipp/print"

JOBS_DIR = DATA_DIR / "jobs"
STATIC_DIR = Path(__file__).parent / "static"
PRINTER_UUID = str(uuid.uuid5(uuid.NAMESPACE_URL, f"fake-ipp-printer:{PRINTER_NAME}"))
START_TIME = time.time()

# media name -> (width, height) in hundredths of a millimetre
MEDIA = {
    "na_letter_8.5x11in": (21590, 27940),
    "na_legal_8.5x14in": (21590, 35560),
    "iso_a4_210x297mm": (21000, 29700),
    "iso_a5_148x210mm": (14800, 21000),
    "na_index-4x6_4x6in": (10160, 15240),
    "oe_4x6-label_4x6in": (10160, 15240),
    "na_number-10_4.125x9.5in": (10477, 24130),
}
MEDIA_LABELS = {
    "na_letter_8.5x11in": "Letter (8.5 × 11 in)",
    "na_legal_8.5x14in": "Legal (8.5 × 14 in)",
    "iso_a4_210x297mm": "A4 (210 × 297 mm)",
    "iso_a5_148x210mm": "A5 (148 × 210 mm)",
    "na_index-4x6_4x6in": "4 × 6 in",
    "oe_4x6-label_4x6in": "4 × 6 in label",
    "na_number-10_4.125x9.5in": "#10 envelope (4.125 × 9.5 in)",
}
if MEDIA_DEFAULT not in MEDIA:
    log.warning("unknown MEDIA_DEFAULT %r; using na_letter_8.5x11in", MEDIA_DEFAULT)
    MEDIA_DEFAULT = "na_letter_8.5x11in"


def media_points(name):
    """Media size in PDF points (1/72 in)."""
    x, y = MEDIA[name]
    return x * 72 / 2540, y * 72 / 2540

# job-state enum
PENDING, HELD, PROCESSING, STOPPED, CANCELED, ABORTED, COMPLETED = 3, 4, 5, 6, 7, 8, 9
JOB_STATE_NAMES = {3: "pending", 4: "pending-held", 5: "processing", 6: "processing-stopped",
                   7: "canceled", 8: "aborted", 9: "completed"}
TERMINAL = (CANCELED, ABORTED, COMPLETED)
# printer-state enum
IDLE, BUSY, HALTED = 3, 4, 5


@dataclass
class Job:
    id: int
    name: str
    user: str
    created: float
    copies: int = 1
    state: int = HELD
    reasons: list = field(default_factory=lambda: ["job-incoming"])
    message: str = ""
    documents: list = field(default_factory=list)  # [{"file", "format", "size"}]
    detected_formats: list = field(default_factory=list)
    notes: list = field(default_factory=list)  # rendering warnings, e.g. ZPL lint results
    attributes: dict = field(default_factory=dict)
    processing_at: float | None = None
    completed_at: float | None = None
    pages: int | None = None
    impressions_completed: int = 0
    has_pdf: bool = False
    closed: bool = False

    @property
    def dir(self):
        return JOBS_DIR / f"{self.id:05d}"

    @property
    def total_impressions(self):
        return None if self.pages is None else self.pages * self.copies

    def save(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / "job.json.tmp"
        tmp.write_text(json.dumps(asdict(self), indent=2))
        tmp.replace(self.dir / "job.json")

    def public(self):
        d = asdict(self)
        d["state_name"] = JOB_STATE_NAMES[self.state]
        d["total_impressions"] = self.total_impressions
        d["size"] = sum(doc["size"] for doc in self.documents)
        return d


class Printer:
    def __init__(self):
        self.jobs: dict[int, Job] = {}
        self.queue: asyncio.Queue[int] = asyncio.Queue()
        self.seconds_per_page = SECONDS_PER_PAGE
        self.media = MEDIA_DEFAULT  # what's loaded in the tray
        self.config_changed = START_TIME
        self.paused = False
        self.out_of_paper = False
        self.accepting = True
        self.active_job: int | None = None
        self.state_changed = time.time()
        self.cancel_events: dict[int, asyncio.Event] = {}
        self.wake = asyncio.Event()
        self.subscribers: set[asyncio.Queue] = set()
        self.next_id = 1

    # ------------------------------------------------------------ state

    @property
    def state(self):
        if self.paused or self.out_of_paper:
            return HALTED
        return BUSY if self.active_job else IDLE

    @property
    def state_reasons(self):
        reasons = []
        if self.paused:
            reasons.append("paused")
        if self.out_of_paper:
            reasons += ["media-empty-error", "media-needed-error"]
        return reasons or ["none"]

    @property
    def state_message(self):
        if self.out_of_paper:
            return "Out of paper"
        if self.paused:
            return "Paused"
        if self.active_job:
            return f"Printing job {self.active_job}"
        return "Ready"

    def public(self):
        return {
            "name": PRINTER_NAME,
            "location": PRINTER_LOCATION,
            "make_and_model": MAKE_AND_MODEL,
            "uuid": PRINTER_UUID,
            "state": {IDLE: "idle", BUSY: "processing", HALTED: "stopped"}[self.state],
            "state_reasons": self.state_reasons,
            "state_message": self.state_message,
            "seconds_per_page": self.seconds_per_page,
            "media": self.media,
            "media_options": [{"name": m, "label": MEDIA_LABELS[m]} for m in MEDIA],
            "paused": self.paused,
            "out_of_paper": self.out_of_paper,
            "accepting": self.accepting,
            "active_job": self.active_job,
            "queued": sum(1 for j in self.jobs.values() if j.state in (PENDING, PROCESSING, HELD)),
            "uris": [f"ipp://{ip}:{IPP_PORT}{RESOURCE_PATH}" for ip in mdns.local_ipv4_addresses()]
                    or [f"ipp://localhost:{IPP_PORT}{RESOURCE_PATH}"],
            "web_port": WEB_PORT,
            "ipp_port": IPP_PORT,
            "raw_port": RAW_PORT,
            "raw_uris": [f"socket://{ip}:{RAW_PORT}" for ip in mdns.local_ipv4_addresses()] if RAW_PORT else [],
        }

    # ------------------------------------------------------------ events

    def publish(self, message):
        for q in list(self.subscribers):
            q.put_nowait(message)

    def publish_job(self, job, save=True):
        if save:
            job.save()
        self.publish({"type": "job", "job": job.public()})

    def publish_printer(self):
        self.state_changed = time.time()
        self.publish({"type": "printer", "printer": self.public()})

    # ------------------------------------------------------------ jobs

    def load(self):
        JOBS_DIR.mkdir(parents=True, exist_ok=True)
        for meta in sorted(JOBS_DIR.glob("*/job.json")):
            try:
                job = Job(**json.loads(meta.read_text()))
            except Exception as e:
                log.warning("skipping unreadable job %s: %s", meta, e)
                continue
            self.jobs[job.id] = job
            if job.state in (PENDING, PROCESSING, STOPPED) or (job.state == HELD and job.closed):
                job.state, job.reasons, job.impressions_completed = PENDING, ["none"], 0
                job.save()
                self.enqueue(job)
            elif job.state == HELD:
                job.state, job.reasons, job.message = ABORTED, ["aborted-by-system"], "Server restarted before the job was complete"
                job.save()
        self.next_id = max(self.jobs, default=0) + 1
        log.info("loaded %d existing jobs", len(self.jobs))

    def create_job(self, name, user, job_attrs):
        copies = job_attrs.get("copies", [1])[0]
        job = Job(
            id=self.next_id,
            name=name or "Untitled",
            user=user or "anonymous",
            created=time.time(),
            copies=max(1, copies if isinstance(copies, int) else 1),
            attributes={k: jsonable(v) for k, v in job_attrs.items()},
        )
        self.next_id += 1
        self.jobs[job.id] = job
        self.publish_job(job)
        return job

    def add_document(self, job, data, fmt):
        n = len(job.documents) + 1
        filename = f"document-{n}.{convert.EXTENSIONS.get(fmt, 'bin')}"
        job.dir.mkdir(parents=True, exist_ok=True)
        (job.dir / filename).write_bytes(data)
        job.documents.append({"file": filename, "format": fmt, "size": len(data)})
        log.info("job %d: received %s (%d bytes, %s)", job.id, filename, len(data), fmt)

    def close_job(self, job):
        job.closed = True
        if not job.documents:
            job.state, job.reasons, job.message = ABORTED, ["aborted-by-system"], "No document data received"
            job.completed_at = time.time()
            self.publish_job(job)
            return
        job.state, job.reasons = PENDING, ["none"]
        self.publish_job(job)
        self.enqueue(job)

    def enqueue(self, job):
        self.cancel_events[job.id] = asyncio.Event()
        self.queue.put_nowait(job.id)

    def cancel(self, job, reason="job-canceled-by-user"):
        if job.state in TERMINAL:
            return False
        job.state, job.reasons, job.message = CANCELED, [reason], "Canceled"
        job.completed_at = time.time()
        if ev := self.cancel_events.get(job.id):
            ev.set()
        self.publish_job(job)
        return True

    def delete(self, job):
        self.cancel(job)
        self.jobs.pop(job.id, None)
        shutil.rmtree(job.dir, ignore_errors=True)
        self.publish({"type": "job-deleted", "id": job.id})

    # ------------------------------------------------------------ engine

    async def wait_until_ready(self, cancel):
        while self.paused or self.out_of_paper:
            self.wake.clear()
            waiter = asyncio.ensure_future(self.wake.wait())
            canceller = asyncio.ensure_future(cancel.wait())
            await asyncio.wait({waiter, canceller}, return_when=asyncio.FIRST_COMPLETED)
            waiter.cancel()
            canceller.cancel()
            if cancel.is_set():
                return

    async def run(self):
        while True:
            job_id = await self.queue.get()
            job = self.jobs.get(job_id)
            cancel = self.cancel_events.get(job_id)
            if not job or job.state != PENDING or not cancel:
                continue
            try:
                await self.process(job, cancel)
            except Exception as e:
                log.exception("job %d failed", job.id)
                job.state, job.reasons, job.message = ABORTED, ["aborted-by-system"], f"Error: {e}"
                job.completed_at = time.time()
                self.publish_job(job)
            finally:
                self.cancel_events.pop(job.id, None)
                self.active_job = None
                self.publish_printer()

    async def process(self, job, cancel):
        await self.wait_until_ready(cancel)
        if cancel.is_set():
            return
        self.active_job = job.id
        job.state, job.reasons, job.message = PROCESSING, ["job-transforming"], "Rendering document"
        job.processing_at = time.time()
        self.publish_job(job)
        self.publish_printer()

        pdf, detected = await asyncio.to_thread(self.render, job)
        if cancel.is_set():
            return
        job.pages = convert.page_count(pdf)
        job.detected_formats = detected
        job.has_pdf = True
        job.reasons, job.message = ["job-printing"], "Printing"
        self.publish_job(job)
        log.info("job %d: %d page(s) x %d copies at %.2fs/page", job.id, job.pages, job.copies, self.seconds_per_page)

        while job.impressions_completed < job.total_impressions:
            if self.paused or self.out_of_paper:
                job.state, job.reasons = STOPPED, ["printer-stopped"]
                job.message = self.state_message
                self.publish_job(job)
                await self.wait_until_ready(cancel)
                if cancel.is_set():
                    return
                job.state, job.reasons, job.message = PROCESSING, ["job-printing"], "Printing"
                self.publish_job(job)
            try:
                await asyncio.wait_for(cancel.wait(), timeout=max(0.0, self.seconds_per_page))
                return  # canceled mid-page
            except TimeoutError:
                pass
            if self.paused or self.out_of_paper:
                continue  # page didn't make it out; reprint it once ready
            job.impressions_completed += 1
            self.publish_job(job, save=job.impressions_completed == job.total_impressions)

        job.state, job.reasons, job.message = COMPLETED, ["job-completed-successfully"], "Completed"
        job.completed_at = time.time()
        self.publish_job(job)
        log.info("job %d: completed", job.id)

    def render(self, job):
        pdfs, detected, job.notes = [], [], []
        for doc in job.documents:
            data = (job.dir / doc["file"]).read_bytes()
            pdf, fmt, notes = convert.to_pdf(data, doc["format"], media_points(self.media))
            pdfs.append(pdf)
            detected.append(fmt)
            job.notes += notes
            for note in notes:
                log.info("job %d: %s", job.id, note)
        pdf = convert.merge(pdfs)
        if ranges := job.attributes.get("page-ranges"):
            pdf = convert.select_pages(pdf, ranges)
        (job.dir / "output.pdf").write_bytes(pdf)
        return pdf, detected


def jsonable(value):
    if isinstance(value, list):
        return [jsonable(v) for v in value]
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, bytes):
        return value.hex()
    return value


# ===================================================================== IPP

T = ipp


def printer_uri(request):
    scheme = "ipps" if request.secure else "ipp"
    return f"{scheme}://{request.host}{RESOURCE_PATH}"


def media_col(name, source="main"):
    x, y = MEDIA[name]
    return [
        ("media-size", T.BEG_COLLECTION, [[("x-dimension", T.INTEGER, [x]), ("y-dimension", T.INTEGER, [y])]]),
        ("media-size-name", T.KEYWORD, [name]),
        ("media-top-margin", T.INTEGER, [0]),
        ("media-bottom-margin", T.INTEGER, [0]),
        ("media-left-margin", T.INTEGER, [0]),
        ("media-right-margin", T.INTEGER, [0]),
        ("media-source", T.KEYWORD, [source]),
        ("media-type", T.KEYWORD, ["labels" if "label" in name else "stationery"]),
    ]


def printer_attributes(printer, request):
    uri = printer_uri(request)
    host = request.host.split(":")[0] if not request.host.startswith("[") else request.host.split("]")[0] + "]"
    web_url = f"http://{host}:{WEB_PORT}/"
    color_modes = ["auto", "color", "monochrome"] if COLOR else ["monochrome"]
    raster_types = ["sgray_8", "srgb_8"] if COLOR else ["sgray_8"]
    sides = ["one-sided", "two-sided-long-edge", "two-sided-short-edge"] if DUPLEX else ["one-sided"]
    now = int(time.time())
    ppm = max(1, round(60 / printer.seconds_per_page)) if printer.seconds_per_page > 0 else 999
    return [
        # printer description
        ("printer-uri-supported", T.URI, [uri]),
        ("uri-security-supported", T.KEYWORD, ["none"]),
        ("uri-authentication-supported", T.KEYWORD, ["none"]),
        ("printer-name", T.NAME, [PRINTER_NAME]),
        ("printer-info", T.TEXT, [PRINTER_NAME]),
        ("printer-location", T.TEXT, [PRINTER_LOCATION]),
        ("printer-make-and-model", T.TEXT, [MAKE_AND_MODEL]),
        ("printer-more-info", T.URI, [web_url]),
        ("printer-supply-info-uri", T.URI, [web_url]),
        ("printer-device-id", T.TEXT, ["MFG:Fake;MDL:IPP Printer;CMD:PDF,PS,PWGRaster,URF,JPEG,PNG;"]),
        ("printer-uuid", T.URI, [f"urn:uuid:{PRINTER_UUID}"]),
        ("printer-state", T.ENUM, [printer.state]),
        ("printer-state-reasons", T.KEYWORD, printer.state_reasons),
        ("printer-state-message", T.TEXT, [printer.state_message]),
        ("printer-is-accepting-jobs", T.BOOLEAN, [printer.accepting]),
        ("queued-job-count", T.INTEGER, [printer.public()["queued"]]),
        ("printer-up-time", T.INTEGER, [now]),
        ("printer-current-time", T.DATETIME, [T.datetime_value(now)]),
        ("printer-state-change-time", T.INTEGER, [int(printer.state_changed)]),
        ("printer-config-change-time", T.INTEGER, [int(printer.config_changed)]),
        ("printer-state-change-date-time", T.DATETIME, [T.datetime_value(printer.state_changed)]),
        ("printer-config-change-date-time", T.DATETIME, [T.datetime_value(printer.config_changed)]),
        ("printer-geo-location", T.UNKNOWN, []),
        ("printer-organization", T.TEXT, [""]),
        ("printer-organizational-unit", T.TEXT, [""]),
        ("printer-icons", T.URI, [f"{web_url}icon.png"]),
        ("printer-supply", T.OCTET_STRING, [
            "index=1;class=supplyThatIsConsumed;type=toner;unit=percent;maxcapacity=100;level=100;colorantname=black;",
            f"index=2;class=supplyThatIsConsumed;type=inputTray;unit=sheets;maxcapacity=500;"
            f"level={0 if printer.out_of_paper else 500};"]),
        ("printer-supply-description", T.TEXT, ["Black Toner", "Paper Tray"]),
        ("ipp-versions-supported", T.KEYWORD, ["1.0", "1.1", "2.0"]),
        ("ipp-features-supported", T.KEYWORD, ["ipp-everywhere"]),
        ("operations-supported", T.ENUM, [T.PRINT_JOB, T.VALIDATE_JOB, T.CREATE_JOB, T.SEND_DOCUMENT,
                                          T.CANCEL_JOB, T.GET_JOB_ATTRIBUTES, T.GET_JOBS,
                                          T.GET_PRINTER_ATTRIBUTES, T.CANCEL_MY_JOBS, T.CLOSE_JOB,
                                          T.IDENTIFY_PRINTER]),
        ("multiple-document-jobs-supported", T.BOOLEAN, [True]),
        ("multiple-operation-time-out", T.INTEGER, [120]),
        ("multiple-operation-time-out-action", T.KEYWORD, ["process-job"]),
        ("charset-configured", T.CHARSET, ["utf-8"]),
        ("charset-supported", T.CHARSET, ["utf-8", "us-ascii"]),
        ("natural-language-configured", T.LANGUAGE, ["en"]),
        ("generated-natural-language-supported", T.LANGUAGE, ["en"]),
        ("document-format-default", T.MIME, ["application/octet-stream"]),
        ("document-format-supported", T.MIME, convert.SUPPORTED_FORMATS),
        ("compression-supported", T.KEYWORD, ["none", "gzip", "deflate"]),
        ("pdl-override-supported", T.KEYWORD, ["attempted"]),
        ("color-supported", T.BOOLEAN, [COLOR]),
        ("pages-per-minute", T.INTEGER, [ppm]),
        *([("pages-per-minute-color", T.INTEGER, [ppm])] if COLOR else []),
        ("printer-kind", T.KEYWORD, ["document", "envelope", "label", "photo"]),
        ("identify-actions-default", T.KEYWORD, ["display"]),
        ("identify-actions-supported", T.KEYWORD, ["display", "sound"]),
        ("which-jobs-supported", T.KEYWORD, ["completed", "not-completed", "all"]),
        ("job-ids-supported", T.BOOLEAN, [True]),
        ("preferred-attributes-supported", T.BOOLEAN, [False]),
        ("printer-get-attributes-supported", T.KEYWORD, ["document-format"]),
        ("job-creation-attributes-supported", T.KEYWORD, [
            "copies", "finishings", "ipp-attribute-fidelity", "job-name", "media", "media-col",
            "orientation-requested", "output-bin", "page-ranges", "print-color-mode", "print-quality",
            "printer-resolution", "sides", "print-scaling"]),
        # job template
        ("copies-default", T.INTEGER, [1]),
        ("copies-supported", T.RANGE, [(1, 999)]),
        ("finishings-default", T.ENUM, [3]),
        ("finishings-supported", T.ENUM, [3]),
        ("media-default", T.KEYWORD, [printer.media]),
        ("media-supported", T.KEYWORD, list(MEDIA)),
        ("media-ready", T.KEYWORD, [printer.media]),
        ("media-col-default", T.BEG_COLLECTION, [media_col(printer.media)]),
        ("media-col-ready", T.BEG_COLLECTION, [media_col(printer.media)]),
        ("media-col-database", T.BEG_COLLECTION, [media_col(m) for m in MEDIA]),
        ("media-col-supported", T.KEYWORD, ["media-size", "media-size-name", "media-top-margin",
                                             "media-bottom-margin", "media-left-margin",
                                             "media-right-margin", "media-source", "media-type"]),
        ("media-size-supported", T.BEG_COLLECTION, [
            [("x-dimension", T.INTEGER, [x]), ("y-dimension", T.INTEGER, [y])] for x, y in MEDIA.values()]),
        ("media-source-supported", T.KEYWORD, ["main"]),
        ("media-type-supported", T.KEYWORD, ["stationery", "labels", "photographic"]),
        ("media-top-margin-supported", T.INTEGER, [0]),
        ("media-bottom-margin-supported", T.INTEGER, [0]),
        ("media-left-margin-supported", T.INTEGER, [0]),
        ("media-right-margin-supported", T.INTEGER, [0]),
        ("orientation-requested-default", T.ENUM, [3]),
        ("orientation-requested-supported", T.ENUM, [3, 4, 5, 6]),
        ("output-bin-default", T.KEYWORD, ["face-down"]),
        ("output-bin-supported", T.KEYWORD, ["face-down"]),
        ("page-ranges-supported", T.BOOLEAN, [True]),
        ("overrides-supported", T.KEYWORD, ["document-number", "pages"]),
        ("print-content-optimize-default", T.KEYWORD, ["auto"]),
        ("print-content-optimize-supported", T.KEYWORD, ["auto", "graphics", "photo", "text", "text-and-graphics"]),
        ("print-rendering-intent-default", T.KEYWORD, ["auto"]),
        ("print-rendering-intent-supported", T.KEYWORD, ["auto", "perceptual", "relative"]),
        ("print-color-mode-default", T.KEYWORD, [color_modes[0]]),
        ("print-color-mode-supported", T.KEYWORD, color_modes),
        ("print-quality-default", T.ENUM, [4]),
        ("print-quality-supported", T.ENUM, [3, 4, 5]),
        ("print-scaling-default", T.KEYWORD, ["auto"]),
        ("print-scaling-supported", T.KEYWORD, ["auto", "auto-fit", "fill", "fit", "none"]),
        ("printer-resolution-default", T.RESOLUTION, [(300, 300, 3)]),
        ("printer-resolution-supported", T.RESOLUTION, [(300, 300, 3), (600, 600, 3)]),
        ("sides-default", T.KEYWORD, ["one-sided"]),
        ("sides-supported", T.KEYWORD, sides),
        ("job-priority-default", T.INTEGER, [50]),
        ("job-priority-supported", T.INTEGER, [100]),
        ("job-sheets-default", T.NAME, ["none"]),
        ("job-sheets-supported", T.NAME, ["none"]),
        # raster formats
        ("pwg-raster-document-resolution-supported", T.RESOLUTION, [(300, 300, 3), (600, 600, 3)]),
        ("pwg-raster-document-type-supported", T.KEYWORD, raster_types),
        ("pwg-raster-document-sheet-back", T.KEYWORD, ["normal"]),
        ("urf-supported", T.KEYWORD, ["V1.4", "CP1", "W8", "RS300-600", "DM1", "IS1", "MT1-3"]
                                      + (["SRGB24"] if COLOR else [])),
    ]


DESCRIPTION_GROUPS = {"all", "printer-description", "job-template", "media-col-database"}


def filter_attrs(attrs, requested):
    if not requested or DESCRIPTION_GROUPS.intersection(requested) or "job-description" in requested:
        return attrs
    wanted = set(requested)
    return [a for a in attrs if a[0] in wanted]


def job_attributes(job, request):
    uri = printer_uri(request)

    def ts(value):
        return (T.INTEGER, [int(value)]) if value else (T.NO_VALUE, [])

    attrs = [
        ("job-id", T.INTEGER, [job.id]),
        ("job-uri", T.URI, [f"{uri}/{job.id}"]),
        ("job-printer-uri", T.URI, [uri]),
        ("job-name", T.NAME, [job.name]),
        ("job-originating-user-name", T.NAME, [job.user]),
        ("job-state", T.ENUM, [job.state]),
        ("job-state-reasons", T.KEYWORD, job.reasons),
        ("job-state-message", T.TEXT, [job.message or JOB_STATE_NAMES[job.state]]),
        ("job-printer-up-time", T.INTEGER, [int(time.time())]),
        ("time-at-creation", T.INTEGER, [int(job.created)]),
        ("time-at-processing", *ts(job.processing_at)),
        ("time-at-completed", *ts(job.completed_at)),
        ("date-time-at-creation", T.DATETIME, [T.datetime_value(job.created)]),
        ("copies", T.INTEGER, [job.copies]),
        ("number-of-documents", T.INTEGER, [len(job.documents)]),
        ("job-k-octets", T.INTEGER, [math.ceil(sum(d["size"] for d in job.documents) / 1024)]),
        ("job-impressions-completed", T.INTEGER, [job.impressions_completed]),
        ("job-media-sheets-completed", T.INTEGER, [job.impressions_completed]),
    ]
    if job.total_impressions is not None:
        attrs.append(("job-impressions", T.INTEGER, [job.total_impressions]))
        attrs.append(("job-media-sheets", T.INTEGER, [job.total_impressions]))
    if job.documents:
        attrs.append(("document-format-supplied", T.MIME, [job.documents[0]["format"]]))
    return attrs


def job_summary(job, request):
    keep = {"job-id", "job-uri", "job-state", "job-state-reasons", "job-state-message"}
    return [a for a in job_attributes(job, request) if a[0] in keep]


class IPPError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def read_document(req):
    data = req.data
    compression = req.get(T.OPERATION, "compression", "none")
    try:
        if compression == "gzip":
            data = gzip.decompress(data)
        elif compression == "deflate":
            data = zlib.decompress(data)
        elif compression not in ("none", None):
            raise IPPError(T.COMPRESSION_NOT_SUPPORTED, f"Unsupported compression '{compression}'")
    except (OSError, zlib.error) as e:
        raise IPPError(T.BAD_REQUEST, f"Could not decompress document: {e}")
    return data


def document_format(req):
    fmt = req.get(T.OPERATION, "document-format", "application/octet-stream")
    if fmt not in convert.SUPPORTED_FORMATS:
        raise IPPError(T.DOCUMENT_FORMAT_NOT_SUPPORTED, f"Document format '{fmt}' is not supported")
    return fmt


def find_job(printer, req):
    job_id = req.get(T.OPERATION, "job-id")
    if job_id is None and (job_uri := req.get(T.OPERATION, "job-uri")):
        tail = job_uri.rstrip("/").rsplit("/", 1)[-1]
        job_id = int(tail) if tail.isdigit() else None
    if job_id is None:
        raise IPPError(T.BAD_REQUEST, "Missing job-id")
    job = printer.jobs.get(job_id)
    if not job:
        raise IPPError(T.NOT_FOUND, f"Job {job_id} not found")
    return job


def new_job(printer, req):
    if not printer.accepting:
        raise IPPError(T.NOT_POSSIBLE, "Printer is not accepting jobs")
    return printer.create_job(
        req.get(T.OPERATION, "job-name") or req.get(T.OPERATION, "document-name"),
        req.get(T.OPERATION, "requesting-user-name"),
        req.group(T.JOB),
    )


def validate_request(req):
    """The checks RFC 8011 section 4.1 requires of every request."""
    if req.request_id <= 0:
        raise IPPError(T.BAD_REQUEST, "request-id must be greater than zero")
    if not req.groups or req.groups[0][0] != T.OPERATION:
        raise IPPError(T.BAD_REQUEST, "Missing operation attributes")
    names = list(req.groups[0][1])
    if names[:2] != ["attributes-charset", "attributes-natural-language"]:
        raise IPPError(T.BAD_REQUEST, "attributes-charset and attributes-natural-language must come first")
    if "printer-uri" not in names and "job-uri" not in names:
        raise IPPError(T.BAD_REQUEST, "Missing printer-uri")


def handle_operation(printer, req, request):
    validate_request(req)
    op = req.operation
    requested = req.group(T.OPERATION).get("requested-attributes", [])

    if op == T.GET_PRINTER_ATTRIBUTES:
        return T.OK, [(T.PRINTER, filter_attrs(printer_attributes(printer, request), requested))]

    if op == T.VALIDATE_JOB:
        document_format(req)
        return T.OK, []

    if op == T.PRINT_JOB:
        fmt = document_format(req)
        data = read_document(req)
        job = new_job(printer, req)
        printer.add_document(job, data, fmt)
        printer.close_job(job)
        return T.OK, [(T.JOB, job_summary(job, request))]

    if op == T.CREATE_JOB:
        job = new_job(printer, req)
        return T.OK, [(T.JOB, job_summary(job, request))]

    if op == T.SEND_DOCUMENT:
        job = find_job(printer, req)
        if job.closed or job.state != HELD:
            raise IPPError(T.NOT_POSSIBLE, "Job is not accepting documents")
        last = req.get(T.OPERATION, "last-document")
        if last is None:
            raise IPPError(T.BAD_REQUEST, "Missing last-document")
        if req.data:
            printer.add_document(job, read_document(req), document_format(req))
            printer.publish_job(job)
        if last:
            printer.close_job(job)
        return T.OK, [(T.JOB, job_summary(job, request))]

    if op == T.CLOSE_JOB:
        job = find_job(printer, req)
        if not job.closed and job.state == HELD:
            printer.close_job(job)
        return T.OK, [(T.JOB, job_summary(job, request))]

    if op == T.CANCEL_JOB:
        job = find_job(printer, req)
        if not printer.cancel(job):
            raise IPPError(T.NOT_POSSIBLE, f"Job {job.id} is already {JOB_STATE_NAMES[job.state]}")
        return T.OK, []

    if op == T.CANCEL_MY_JOBS:
        user = req.get(T.OPERATION, "requesting-user-name", "anonymous")
        ids = req.group(T.OPERATION).get("job-ids")
        targets = [j for j in printer.jobs.values() if j.user == user and j.state not in TERMINAL]
        if ids:
            targets = [j for j in targets if j.id in ids]
        for job in targets:
            printer.cancel(job)
        return T.OK, []

    if op == T.GET_JOB_ATTRIBUTES:
        job = find_job(printer, req)
        return T.OK, [(T.JOB, filter_attrs(job_attributes(job, request), requested))]

    if op == T.GET_JOBS:
        which = req.get(T.OPERATION, "which-jobs", "not-completed")
        limit = req.get(T.OPERATION, "limit") or 0
        jobs = sorted(printer.jobs.values(), key=lambda j: j.id, reverse=True)
        if req.get(T.OPERATION, "my-jobs", False):
            user = req.get(T.OPERATION, "requesting-user-name", "anonymous")
            jobs = [j for j in jobs if j.user == user]
        if which == "completed":
            jobs = [j for j in jobs if j.state in TERMINAL]
        elif which == "not-completed":
            jobs = [j for j in jobs if j.state not in TERMINAL]
        if limit > 0:
            jobs = jobs[:limit]
        requested = requested or ["job-id", "job-uri"]
        return T.OK, [(T.JOB, filter_attrs(job_attributes(j, request), requested)) for j in jobs]

    if op == T.IDENTIFY_PRINTER:
        message = req.get(T.OPERATION, "message", "")
        printer.publish({"type": "identify", "message": message})
        log.info("identify-printer requested %s", message)
        return T.OK, []

    raise IPPError(T.OPERATION_NOT_SUPPORTED, f"Operation 0x{op:04x} is not supported")


async def ipp_handler(request):
    printer = request.app["printer"]
    if request.content_type != "application/ipp":
        return web.Response(status=400, text="Expected Content-Type: application/ipp\n")
    body = await request.read()
    try:
        req = ipp.parse(body)
    except Exception as e:
        log.warning("bad IPP request from %s: %s", request.remote, e)
        resp = ipp.encode_response((1, 1), T.BAD_REQUEST, 0, [(T.OPERATION, [
            ("attributes-charset", T.CHARSET, ["utf-8"]),
            ("attributes-natural-language", T.LANGUAGE, ["en"]),
            ("status-message", T.TEXT, [f"Bad request: {e}"])])])
        return web.Response(body=resp, content_type="application/ipp")

    version = req.version if req.version[0] in (1, 2) else (1, 1)
    if version[0] == 2 and version[1] > 0:
        version = (2, 0)
    try:
        if req.version[0] not in (1, 2):
            raise IPPError(T.VERSION_NOT_SUPPORTED, "IPP version not supported")
        status, groups = handle_operation(printer, req, request)
        message = None
    except IPPError as e:
        status, groups, message = e.status, [], str(e)
    except Exception as e:
        log.exception("error handling IPP operation 0x%04x", req.operation)
        status, groups, message = T.INTERNAL_ERROR, [], str(e)

    op_attrs = [("attributes-charset", T.CHARSET, ["utf-8"]),
                ("attributes-natural-language", T.LANGUAGE, ["en"])]
    if message:
        op_attrs.append(("status-message", T.TEXT, [message]))
        log.info("IPP op 0x%04x from %s -> 0x%04x %s", req.operation, request.remote, status, message)
    else:
        log.info("IPP op 0x%04x from %s -> 0x%04x", req.operation, request.remote, status)
    resp = ipp.encode_response(version, status, req.request_id, [(T.OPERATION, op_attrs)] + groups)
    return web.Response(body=resp, content_type="application/ipp")


# ============================================================ raw socket

def zebra_host_status(printer):
    """Reply to the ZPL ~HS (host status) query, reflecting the simulated printer state."""
    paper_out = int(printer.out_of_paper)
    paused = int(printer.paused)
    queued = min(999, printer.public()["queued"])
    return (f"\x02030,{paper_out},{paused},1218,{queued:03d},0,0,0,000,0,0,0\x03\r\n"
            f"\x02000,0,0,0,1,2,4,0,00000000,1,000\x03\r\n"
            f"\x021234,0\x03\r\n").encode()


async def raw_handler(printer, reader, writer):
    """One TCP connection on port 9100 = one job, as on a JetDirect-style printer."""
    peer = writer.get_extra_info("peername")
    host = peer[0] if peer else "unknown"
    log.info("raw: connection from %s", host)
    chunks = []
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(reader.read(65536), timeout=RAW_IDLE_TIMEOUT)
            except TimeoutError:
                log.info("raw: %s idle for %.0fs, ending job", host, RAW_IDLE_TIMEOUT)
                break
            if not chunk:
                break
            if b"~HS" in chunk.upper():
                log.info("raw: %s sent ~HS status query, replying", host)
                writer.write(zebra_host_status(printer))
                await writer.drain()
            chunks.append(chunk)
    except (ConnectionResetError, BrokenPipeError) as e:
        log.info("raw: %s connection dropped: %s", host, e)
    finally:
        writer.close()

    data = b"".join(chunks)
    payload = data.replace(b"~HS", b"").replace(b"~hs", b"").strip()
    if not payload:
        log.info("raw: %s closed without sending a document (%d bytes: %r)", host, len(data), data[:80])
        return
    log.info("raw: %s sent %d bytes, starting %r", host, len(data), data[:80])
    if not printer.accepting:
        log.info("raw: not accepting jobs, discarding")
        return
    fmt = convert.detect(data)
    job = printer.create_job(f"Raw job from {host}", host, {"transport": ["raw-socket:9100"]})
    printer.add_document(job, data, fmt)
    printer.close_job(job)


# ===================================================================== Web UI

def get_job(request):
    try:
        job = request.app["printer"].jobs.get(int(request.match_info["id"]))
    except ValueError:
        job = None
    if not job:
        raise web.HTTPNotFound(text="No such job")
    return job


async def index(request):
    return web.FileResponse(STATIC_DIR / "index.html")


async def icon(request):
    return web.FileResponse(STATIC_DIR / "icon.png")


async def api_state(request):
    printer = request.app["printer"]
    jobs = sorted(printer.jobs.values(), key=lambda j: j.id, reverse=True)
    return web.json_response({"printer": printer.public(), "jobs": [j.public() for j in jobs]})


async def api_settings(request):
    printer = request.app["printer"]
    body = await request.json()
    if "seconds_per_page" in body:
        try:
            value = float(body["seconds_per_page"])
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(text="seconds_per_page must be a number")
        if not math.isfinite(value) or value < 0:
            raise web.HTTPBadRequest(text="seconds_per_page must be >= 0")
        printer.seconds_per_page = value
    if "media" in body:
        if body["media"] not in MEDIA:
            raise web.HTTPBadRequest(text=f"media must be one of: {', '.join(MEDIA)}")
        if body["media"] != printer.media:
            printer.media = body["media"]
            printer.config_changed = time.time()
            log.info("media changed to %s", printer.media)
    if "paused" in body:
        printer.paused = bool(body["paused"])
    if "out_of_paper" in body:
        printer.out_of_paper = bool(body["out_of_paper"])
    if "accepting" in body:
        printer.accepting = bool(body["accepting"])
    printer.wake.set()
    printer.publish_printer()
    return web.json_response(printer.public())


async def api_cancel(request):
    job = get_job(request)
    request.app["printer"].cancel(job)
    return web.json_response(job.public())


async def api_delete(request):
    request.app["printer"].delete(get_job(request))
    return web.json_response({"ok": True})


async def api_clear(request):
    printer = request.app["printer"]
    for job in [j for j in printer.jobs.values() if j.state in TERMINAL]:
        printer.delete(job)
    return web.json_response({"ok": True})


async def api_upload(request):
    """Lets you drop a file on the dashboard to queue a job without an IPP client."""
    printer = request.app["printer"]
    reader = await request.multipart()
    part = await reader.next()
    if part is None:
        raise web.HTTPBadRequest(text="No file")
    data = await part.read(decode=True)
    fmt = convert.detect(data)
    job = printer.create_job(part.filename or "Upload", "web-upload", {})
    printer.add_document(job, data, fmt)
    printer.close_job(job)
    return web.json_response(job.public())


async def job_pdf(request):
    job = get_job(request)
    path = job.dir / "output.pdf"
    if not path.exists():
        raise web.HTTPNotFound(text="This job has not been rendered yet")
    stem = os.path.splitext(job.name)[0]
    safe = "".join(c if c.isalnum() or c in "-_. " else "_" for c in stem).strip() or "job"
    return web.FileResponse(path, headers={
        "Content-Type": "application/pdf",
        "Content-Disposition": f'inline; filename="job-{job.id}-{safe}.pdf"',
    })


async def job_document(request):
    job = get_job(request)
    try:
        doc = job.documents[int(request.match_info["n"]) - 1]
    except (ValueError, IndexError):
        raise web.HTTPNotFound()
    return web.FileResponse(job.dir / doc["file"], headers={
        "Content-Type": "application/octet-stream",
        "Content-Disposition": f'attachment; filename="job-{job.id}-{doc["file"]}"',
    })


async def events(request):
    printer = request.app["printer"]
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                                       "X-Accel-Buffering": "no"})
    await resp.prepare(request)
    queue = asyncio.Queue()
    printer.subscribers.add(queue)
    try:
        jobs = sorted(printer.jobs.values(), key=lambda j: j.id, reverse=True)
        snapshot = {"type": "snapshot", "printer": printer.public(), "jobs": [j.public() for j in jobs]}
        await resp.write(f"data: {json.dumps(snapshot)}\n\n".encode())
        while True:
            try:
                message = await asyncio.wait_for(queue.get(), timeout=15)
            except TimeoutError:
                await resp.write(b": keepalive\n\n")
                continue
            await resp.write(f"data: {json.dumps(message)}\n\n".encode())
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        printer.subscribers.discard(queue)
    return resp


# ===================================================================== main

def build_app(printer):
    app = web.Application(client_max_size=2 * 1024 ** 3)
    app["printer"] = printer
    app.router.add_get("/", index)
    app.router.add_get("/icon.png", icon)
    app.router.add_get("/api/state", api_state)
    app.router.add_get("/api/events", events)
    app.router.add_post("/api/settings", api_settings)
    app.router.add_post("/api/upload", api_upload)
    app.router.add_post("/api/clear", api_clear)
    app.router.add_post("/api/jobs/{id}/cancel", api_cancel)
    app.router.add_delete("/api/jobs/{id}", api_delete)
    app.router.add_get("/jobs/{id}/pdf", job_pdf)
    app.router.add_get("/jobs/{id}/documents/{n}", job_document)
    app.router.add_post("/{tail:.*}", ipp_handler)  # IPP: any POST with application/ipp
    return app


def txt_record():
    pdls = ",".join(f for f in convert.SUPPORTED_FORMATS if f != "application/octet-stream")
    urf = "V1.4,CP1,W8,RS300-600,DM1,IS1,MT1-3" + (",SRGB24" if COLOR else "")
    return {
        "txtvers": "1",
        "qtotal": "1",
        "rp": RESOURCE_PATH.lstrip("/"),
        "ty": MAKE_AND_MODEL,
        "product": f"({MAKE_AND_MODEL})",
        "note": PRINTER_LOCATION,
        "adminurl": f"http://{socket.gethostname()}.local:{WEB_PORT}/",
        "pdl": pdls,
        "URF": urf,
        "UUID": PRINTER_UUID,
        "Color": "T" if COLOR else "F",
        "Duplex": "T" if DUPLEX else "F",
        "Copies": "T",
        "kind": "document,envelope,label,photo",
        "PaperMax": "legal-A4",
        "usb_MFG": "Fake",
        "usb_MDL": "IPP Printer",
        "priority": "20",  # lower than the raw record, so CUPS prefers IPP
    }


async def main():
    printer = Printer()
    printer.load()
    app = build_app(printer)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    for port in sorted({IPP_PORT, WEB_PORT}):
        await web.TCPSite(runner, host=None, port=port).start()
    log.info("IPP:  ipp://<host>:%d%s", IPP_PORT, RESOURCE_PATH)
    log.info("Web:  http://<host>:%d/", WEB_PORT)
    raw_server = None
    if RAW_PORT:
        raw_server = await asyncio.start_server(lambda r, w: raw_handler(printer, r, w), port=RAW_PORT)
        log.info("Raw: tcp://<host>:%d (JetDirect/AppSocket, ZPL/PDF/PS/text)", RAW_PORT)
    log.info("Speed: %.2f seconds per page", printer.seconds_per_page)

    advertiser = mdns.Advertiser(PRINTER_NAME, IPP_PORT, txt_record(), RAW_PORT) if MDNS_ENABLED else None
    if advertiser:
        try:
            await advertiser.start()
        except Exception as e:
            log.warning("mDNS advertisement failed: %s", e)

    engine = asyncio.create_task(printer.run())
    try:
        await asyncio.Event().wait()
    finally:
        engine.cancel()
        if raw_server:
            raw_server.close()
        if advertiser:
            await advertiser.stop()
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
