import io

import pytest
from PIL import Image

from loader import LoadError, open_document


def encode(img, fmt, **kw):
    buf = io.BytesIO()
    img.save(buf, format=fmt, **kw)
    return buf.getvalue()


def decoded_size(data):
    return Image.open(io.BytesIO(data)).size


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "GIF", "BMP", "WEBP"])
def test_native_formats_pass_through_unchanged(fmt):
    data = encode(Image.new("RGB", (300, 120), "white"), fmt)
    page = open_document(data, f"plan.{fmt.lower()}").render(0)
    assert page.data == data
    assert (page.width, page.height) == (300, 120)


@pytest.mark.parametrize(
    "mode, fmt",
    [("RGB", "TIFF"), ("CMYK", "TIFF"), ("I;16", "TIFF"), ("1", "TIFF"),
     ("RGB", "PPM"), ("RGBA", "TGA"), ("RGB", "ICO")],
)
def test_other_formats_convert_to_png_keeping_size(mode, fmt):
    data = encode(Image.new(mode, (321, 123) if fmt != "ICO" else (64, 64)), fmt)
    size = decoded_size(data)
    page = open_document(data, f"plan.{fmt.lower()}").render(0)
    assert page.data.startswith(b"\x89PNG")
    assert (page.width, page.height) == decoded_size(page.data) == size


def test_exif_rotation_is_applied():
    img = Image.new("RGB", (400, 100), "white")
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90° clockwise when displayed
    data = encode(img, "JPEG", exif=exif.tobytes())
    page = open_document(data, "photo.jpg").render(0)
    assert (page.width, page.height) == (100, 400)
    assert decoded_size(page.data) == (100, 400)


def test_multipage_tiff():
    frames = [Image.new("L", (50, 80)), Image.new("L", (90, 30))]
    buf = io.BytesIO()
    frames[0].save(buf, format="TIFF", save_all=True, append_images=frames[1:])
    doc = open_document(buf.getvalue(), "scan.tif")
    assert doc.page_count == 2
    assert (doc.render(1).width, doc.render(1).height) == (90, 30)


def test_dpi_is_read_from_metadata():
    data = encode(Image.new("RGB", (10, 10)), "PNG", dpi=(300, 300))
    assert open_document(data, "a.png").render(0).dpi == pytest.approx(300, rel=0.01)


def test_pdf_page_rendered_with_exact_dpi():
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    doc.new_page(width=842, height=595)  # A4 landscape in points
    data = doc.tobytes()
    page = open_document(data, "plan.pdf").render(0)
    assert page.width / page.height == pytest.approx(842 / 595, rel=0.01)
    assert page.dpi == pytest.approx(page.width / (842 / 72))
    assert decoded_size(page.data) == (page.width, page.height)


def test_svg():
    pytest.importorskip("pymupdf")
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100"><rect width="200" height="100"/></svg>'
    page = open_document(svg, "drawing.svg").render(0)
    assert page.width / page.height == pytest.approx(2, rel=0.01)
    assert page.dpi is None


def test_garbage_is_rejected():
    with pytest.raises(LoadError):
        open_document(b"definitely not an image", "notes.txt")
    with pytest.raises(LoadError):
        open_document(b"", "empty.png")
