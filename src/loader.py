"""Open any image or document file and turn each page into a displayable image.

Every page is returned at its true pixel size and orientation, so the UI can
show it 1:1 without stretching. Raster formats the browser can show natively
(PNG, JPEG, GIF, WebP, BMP) are passed through untouched when no rotation is
needed; everything else is converted to PNG.
"""

import io
import math
from dataclasses import dataclass

# Longest side, in pixels, of any page we hand to the UI. Larger images are
# scaled down evenly (aspect ratio kept) to stay within GPU texture limits.
MAX_SIDE = 12000

# Longest side we aim for when rasterising vector pages (PDF, SVG...).
VECTOR_TARGET_SIDE = 5000
VECTOR_MIN_DPI = 72
VECTOR_MAX_DPI = 400

# Formats that can be displayed as-is.
NATIVE_FORMATS = {"PNG", "JPEG", "GIF", "WEBP", "BMP"}

# Document formats handled by PyMuPDF, keyed by file extension.
DOCUMENT_TYPES = {
    "pdf": "pdf",
    "svg": "svg",
    "xps": "xps",
    "oxps": "xps",
    "epub": "epub",
    "cbz": "cbz",
}


# Image types MuPDF can read when Pillow can't (JPEG 2000, JPEG XR...).
MUPDF_IMAGE_TYPES = {"jp2", "jpx", "j2k", "jxr", "wdp", "hdp", "pnm", "pbm", "pgm", "pam", "psd"}


class LoadError(Exception):
    """Raised with a user-friendly message when a file cannot be opened."""


@dataclass
class RenderedPage:
    data: bytes  # encoded image bytes (PNG/JPEG/...)
    width: int
    height: int
    # Pixels per inch of the original sheet, when known. Exact for PDFs; taken
    # from file metadata for scans (may be missing or wrong there).
    dpi: float | None = None


def _register_heif():
    try:
        from pillow_heif import register_heif_opener

        register_heif_opener()
    except Exception:
        pass  # HEIC/AVIF support is optional


def _import_pymupdf():
    try:
        import pymupdf  # noqa: F401

        return pymupdf
    except ImportError:
        try:
            import fitz  # older PyMuPDF name

            return fitz
        except ImportError:
            return None


def _extension(name):
    name = (name or "").lower()
    return name.rsplit(".", 1)[-1] if "." in name else ""


def _sniff_document_type(data, name):
    head = data[:1024]
    if b"%PDF-" in head:
        return "pdf"
    ext = _extension(name)
    if ext in DOCUMENT_TYPES:
        return DOCUMENT_TYPES[ext]
    stripped = head.lstrip()
    if stripped.startswith(b"<svg") or (
        stripped.startswith(b"<?xml") and b"<svg" in data[:4096]
    ):
        return "svg"
    return None


def _valid_dpi(info):
    dpi = info.get("dpi")
    try:
        if isinstance(dpi, (tuple, list)):
            dpi = dpi[0]
        dpi = float(dpi)
    except (TypeError, ValueError):
        return None
    return dpi if math.isfinite(dpi) and dpi > 1 else None


def _to_display_mode(img):
    """Convert any Pillow mode to one that PNG can store and screens can show."""
    from PIL import Image

    mode = img.mode
    if mode in ("RGB", "RGBA", "L", "LA"):
        return img
    if mode == "1":
        return img.convert("L")
    if mode in ("P", "PA"):
        has_alpha = mode == "PA" or "transparency" in img.info
        return img.convert("RGBA" if has_alpha else "RGB")
    if mode.startswith("I") or mode == "F":
        # 16/32-bit or float scans: stretch the used range to 8 bits.
        img = img.convert("F")
        lo, hi = img.getextrema()
        if hi <= lo:
            return Image.new("L", img.size, 0)
        scale = 255.0 / (hi - lo)
        return img.point(lambda v: (v - lo) * scale).convert("L")
    if mode in ("RGBa", "La"):
        return img.convert("RGBA")
    return img.convert("RGB")  # CMYK, YCbCr, LAB, HSV...


def _encode_png(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG", compress_level=1)
    return buf.getvalue()


class ImageDocument:
    """Raster image. Multi-page TIFFs become multiple pages."""

    kind = "image"

    def __init__(self, data, name=""):
        from PIL import Image

        _register_heif()
        Image.MAX_IMAGE_PIXELS = 400_000_000  # big scans are normal here
        try:
            self.img = Image.open(io.BytesIO(data))
            self.img.load()
        except Exception as ex:
            raise LoadError(f"Not a readable image ({ex.__class__.__name__}).") from ex
        self.data = data
        self.name = name
        self.format = self.img.format or _extension(name).upper()
        frames = getattr(self.img, "n_frames", 1) or 1
        self.page_count = frames if self.format == "TIFF" else 1

    def render(self, index=0):
        from PIL import ImageOps

        img = self.img
        if self.page_count > 1:
            img.seek(index)
        dpi = _valid_dpi(img.info)
        orientation = img.getexif().get(0x0112, 1) if hasattr(img, "getexif") else 1
        w, h = img.size

        if (
            self.page_count == 1
            and self.format in NATIVE_FORMATS
            and orientation in (None, 1)
            and max(w, h) <= MAX_SIDE
        ):
            return RenderedPage(self.data, w, h, dpi)

        frame = ImageOps.exif_transpose(img.copy())
        frame = _to_display_mode(frame)
        if max(frame.size) > MAX_SIDE:
            factor = MAX_SIDE / max(frame.size)
            new_size = (
                max(1, round(frame.width * factor)),
                max(1, round(frame.height * factor)),
            )
            frame = frame.resize(new_size, resample=3)  # BICUBIC
            if dpi:
                dpi *= factor
        return RenderedPage(_encode_png(frame), frame.width, frame.height, dpi)


class VectorDocument:
    """PDF, SVG, XPS... rendered with PyMuPDF at a resolution we control."""

    kind = "document"

    def __init__(self, data, filetype, name=""):
        pymupdf = _import_pymupdf()
        if pymupdf is None:
            raise LoadError(
                f"{filetype.upper()} support needs PyMuPDF, which isn't available here."
            )
        self.pymupdf = pymupdf
        self.name = name
        self.format = filetype.upper()
        try:
            self.doc = pymupdf.open(stream=data, filetype=filetype)
        except Exception as ex:
            raise LoadError(f"Could not open this {self.format} file.") from ex
        if getattr(self.doc, "needs_pass", False):
            raise LoadError("This PDF is password-protected.")
        self.page_count = self.doc.page_count
        if self.page_count < 1:
            raise LoadError("This document has no pages.")

    def page_size_inches(self, index):
        rect = self.doc[index].rect
        return rect.width / 72.0, rect.height / 72.0

    def render(self, index=0):
        page = self.doc[index]
        rect = page.rect  # in points (1/72 in), already accounts for rotation
        longest_in = max(rect.width, rect.height, 1) / 72.0
        dpi = VECTOR_TARGET_SIDE / longest_in
        dpi = max(VECTOR_MIN_DPI, min(VECTOR_MAX_DPI, dpi))
        dpi = min(dpi, MAX_SIDE / longest_in)
        zoom = dpi / 72.0
        pix = page.get_pixmap(matrix=self.pymupdf.Matrix(zoom, zoom), alpha=False)
        # Use the real pixel count to get the exact DPI after rounding.
        real_dpi = pix.width / (rect.width / 72.0) if rect.width else dpi
        # SVG "inches" are CSS pixels at 96 per inch, not a real sheet size.
        sheet_dpi = None if self.format == "SVG" else real_dpi
        return RenderedPage(pix.tobytes("png"), pix.width, pix.height, sheet_dpi)


def open_document(data, name=""):
    """Open `data` (file bytes) and return an ImageDocument or VectorDocument."""
    if not data:
        raise LoadError("The file is empty.")
    doc_type = _sniff_document_type(data, name)
    if doc_type:
        return VectorDocument(data, doc_type, name)
    try:
        return ImageDocument(data, name)
    except LoadError:
        # Last resort: MuPDF reads a few image types Pillow doesn't (JPX, JXR...).
        pymupdf = _import_pymupdf()
        ext = _extension(name)
        if pymupdf is not None and ext in MUPDF_IMAGE_TYPES:
            try:
                doc = VectorDocument(data, ext, name)
                doc.format = ext.upper()
                return doc
            except Exception:
                pass
        raise LoadError(
            "Unsupported file. Open an image (PNG, JPEG, TIFF, WebP, HEIC, BMP, "
            "GIF...) or a PDF/SVG."
        )
