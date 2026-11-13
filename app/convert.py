"""Turn whatever a client sent into a PDF, the way a real printer would render it."""

import io
import struct
import subprocess
import tempfile
import textwrap
from pathlib import Path

from PIL import Image
from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

import labelary

SUPPORTED_FORMATS = [
    "application/pdf",
    "application/postscript",
    "image/pwg-raster",
    "image/urf",
    "image/jpeg",
    "image/png",
    "text/plain",
    "application/octet-stream",
]

EXTENSIONS = {
    "application/pdf": "pdf",
    "application/postscript": "ps",
    "image/pwg-raster": "pwg",
    "image/urf": "urf",
    "image/jpeg": "jpg",
    "image/png": "png",
    "text/plain": "txt",
    "application/octet-stream": "bin",
    "application/vnd.zebra-zpl": "zpl",
}

UEL = b"\x1b%-12345X"

# CUPS/PWG color spaces where zero means "no ink" (subtractive)
_SUBTRACTIVE_SPACES = {3, 6}  # black, cmyk


def strip_pjl(data):
    """Remove a PJL job wrapper (UEL + @PJL lines), as sent by many raw-socket drivers."""
    if not data.startswith(UEL):
        return data
    data = data[len(UEL):]
    while data.lstrip().upper().startswith(b"@PJL"):
        data = data.lstrip()
        end = data.find(b"\n")
        data = data[end + 1:] if end >= 0 else b""
    end = data.rfind(UEL)
    return data[:end] if end >= 0 else data


def detect(data):
    data = strip_pjl(data)
    head = data[:16]
    if head.startswith(b"%PDF"):
        return "application/pdf"
    if head.startswith(b"%!") or head.startswith(b"\x04%!"):
        return "application/postscript"
    if head.startswith(b"RaS2"):
        return "image/pwg-raster"
    if head.startswith(b"UNIRAST"):
        return "image/urf"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG"):
        return "image/png"
    if b"^XA" in data[:4096].upper():
        return "application/vnd.zebra-zpl"
    sample = data[:4096]
    if sample and sum(b < 9 or 13 < b < 32 for b in sample if b != 27) / len(sample) < 0.02:
        return "text/plain"
    return "application/octet-stream"


def to_pdf(data, fmt, page_size=letter):
    """Returns (pdf_bytes, detected_format, notes). page_size (points) is the loaded media, used
    for formats that the printer lays out itself (ZPL labels, text, unknown data)."""
    data = strip_pjl(data)
    if fmt == "application/octet-stream" or fmt not in SUPPORTED_FORMATS:
        fmt = detect(data)
    if fmt == "application/vnd.zebra-zpl":
        pdf, notes = _zpl_to_pdf(data, page_size)
        return pdf, fmt, notes
    return _convert(data, fmt, page_size), fmt, []


def _convert(data, fmt, page_size):
    if fmt == "application/pdf":
        return data
    if fmt == "application/postscript":
        return _ghostscript(data)
    if fmt == "image/pwg-raster":
        return _images_to_pdf(_decode_pwg(data))
    if fmt == "image/urf":
        return _images_to_pdf(_decode_urf(data))
    if fmt in ("image/jpeg", "image/png"):
        img = Image.open(io.BytesIO(data))
        dpi = img.info.get("dpi", (150, 150))[0] or 150
        return _images_to_pdf([(img, dpi)])
    if fmt == "text/plain":
        return _text_to_pdf(data.decode("utf-8", "replace"), page_size)
    return _placeholder_pdf(data, page_size)


def _zpl_to_pdf(data, page_size):
    """Render with Labelary at the loaded label size; fall back to showing the ZPL source."""
    if labelary.ENABLED:
        try:
            pdfs, warnings = labelary.render(data, page_size[0] / 72, page_size[1] / 72)
            return merge(pdfs), [f"ZPL warning: {w}" for w in warnings]
        except labelary.LabelaryError as e:
            reason = str(e)
    else:
        reason = "Labelary rendering is disabled (LABELARY=false)"
    header = f"ZPL label data, shown as source. {reason}\n" + "-" * 60 + "\n"
    return _text_to_pdf(header + data.decode("utf-8", "replace"), page_size), [reason]


def page_count(pdf_bytes):
    return len(PdfReader(io.BytesIO(pdf_bytes)).pages)


def merge(pdfs):
    if len(pdfs) == 1:
        return pdfs[0]
    writer = PdfWriter()
    for pdf in pdfs:
        writer.append(io.BytesIO(pdf))
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def select_pages(pdf_bytes, ranges):
    """Apply IPP page-ranges ([[first, last], ...], 1-based inclusive)."""
    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()
    for first, last in ranges:
        for i in range(max(1, first), min(last, len(reader.pages)) + 1):
            writer.add_page(reader.pages[i - 1])
    if not writer.pages:
        return pdf_bytes
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


# ------------------------------------------------------------- converters

def _ghostscript(data):
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp, "in.ps"), Path(tmp, "out.pdf")
        src.write_bytes(data)
        subprocess.run(
            ["gs", "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER", "-sDEVICE=pdfwrite",
             f"-sOutputFile={dst}", str(src)],
            check=True, capture_output=True, timeout=300,
        )
        return dst.read_bytes()


def _images_to_pdf(pages):
    if not pages:
        raise ValueError("document contains no pages")
    images = []
    for img, _ in pages:
        if img.mode not in ("RGB", "L", "1", "CMYK"):
            img = img.convert("RGB")
        images.append(img)
    out = io.BytesIO()
    images[0].save(out, "PDF", resolution=float(pages[0][1]), save_all=True,
                   append_images=images[1:])
    return out.getvalue()


def _text_to_pdf(text, page_size=letter):
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=page_size)
    width, height = page_size
    margin = 54 if width > 400 else 18  # narrower margins on labels and envelopes
    size = 10 if width > 400 else 8
    leading = size * 1.2
    cols = int((width - 2 * margin) / (size * 0.6))
    lines = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        pieces = raw.split("\f")
        for i, piece in enumerate(pieces):
            if i:
                lines.append("\f")
            lines.extend(textwrap.wrap(piece.expandtabs(8), cols, drop_whitespace=False) or [""])
    y = height - margin
    c.setFont("Courier", size)
    for line in lines:
        if line == "\f" or y < margin:
            c.showPage()
            c.setFont("Courier", size)
            y = height - margin
            if line == "\f":
                continue
        c.drawString(margin, y, line)
        y -= leading
    c.save()
    return out.getvalue()


def _placeholder_pdf(data, page_size=letter):
    lines = ["Unrecognized document format",
             f"{len(data)} bytes received; the original file is saved with this job.", ""]
    for off in range(0, min(len(data), 512), 16):
        chunk = data[off:off + 16]
        lines.append(f"{off:04x}  {chunk.hex(' ')}  "
                     + "".join(chr(b) if 32 <= b < 127 else "." for b in chunk))
    return _text_to_pdf("\n".join(lines), page_size)


# ------------------------------------------------------- raster decoding

def _decode_lines(buf, pos, height, bytes_per_line, unit, white):
    """PWG/URF PackBits-style run-length decoding. Returns (raw_bytes, new_pos)."""
    out = bytearray()
    fill = bytes([white])
    y = 0
    while y < height:
        repeat = buf[pos] + 1
        pos += 1
        line = bytearray()
        while len(line) < bytes_per_line:
            code = buf[pos]
            pos += 1
            if code == 128:
                line += fill * (bytes_per_line - len(line))
            elif code < 128:
                line += buf[pos:pos + unit] * (code + 1)
                pos += unit
            else:
                n = (257 - code) * unit
                line += buf[pos:pos + n]
                pos += n
        repeat = min(repeat, height - y)
        out += bytes(line[:bytes_per_line]) * repeat
        y += repeat
    return bytes(out), pos


def _raster_image(raw, width, height, bpp, subtractive, bytes_per_line):
    if bpp == 1:
        img = Image.frombytes("1", (width, height), raw, "raw", "1;I" if subtractive else "1", bytes_per_line)
    elif bpp == 8:
        img = Image.frombytes("L", (width, height), raw, "raw", "L", bytes_per_line)
        if subtractive:
            img = img.point(lambda v: 255 - v)
    elif bpp == 24:
        img = Image.frombytes("RGB", (width, height), raw, "raw", "RGB", bytes_per_line)
    elif bpp == 32:
        img = Image.frombytes("CMYK", (width, height), raw, "raw", "CMYK", bytes_per_line)
    elif bpp in (16, 48, 64):  # 16 bits per color: keep the high byte of each sample
        channels = bpp // 16
        mode = {1: "L", 3: "RGB", 4: "CMYK"}[channels]
        img = Image.frombytes(mode, (width, height), bytes(raw[0::2]), "raw", mode)
    else:
        raise ValueError(f"unsupported raster depth: {bpp} bits per pixel")
    return img


def _decode_pwg(buf):
    if not buf.startswith(b"RaS2"):
        raise ValueError("not a PWG raster stream")
    pos, pages = 4, []
    while pos + 1796 <= len(buf):
        h = buf[pos:pos + 1796]
        pos += 1796
        dpi = struct.unpack_from(">I", h, 276)[0] or 300
        width, height, _, bpc, bpp, bpl, _, cspace = struct.unpack_from(">8I", h, 372)
        if width == 0 or height == 0:
            break
        subtractive = cspace in _SUBTRACTIVE_SPACES
        try:
            raw, pos = _decode_lines(buf, pos, height, bpl, max(1, bpp // 8),
                                     0x00 if subtractive else 0xFF)
        except IndexError:
            break  # truncated stream: keep whatever full pages we have
        pages.append((_raster_image(raw, width, height, bpp, subtractive, bpl), dpi))
    return pages


def _decode_urf(buf):
    if not buf.startswith(b"UNIRAST\x00"):
        raise ValueError("not a URF stream")
    (count,) = struct.unpack_from(">I", buf, 8)
    pos, pages = 12, []
    while pos + 32 <= len(buf) and (count == 0 or len(pages) < count):
        h = buf[pos:pos + 32]
        pos += 32
        bpp, cspace = h[0], h[1]
        width, height, dpi = struct.unpack_from(">III", h, 12)
        subtractive = cspace == 6 or bpp == 1  # CMYK, or 1-bit where set bits are ink
        bpl = (width * bpp + 7) // 8
        try:
            raw, pos = _decode_lines(buf, pos, height, bpl, max(1, bpp // 8),
                                     0x00 if subtractive else 0xFF)
        except IndexError:
            break
        pages.append((_raster_image(raw, width, height, bpp, subtractive, bpl), dpi or 300))
    return pages
