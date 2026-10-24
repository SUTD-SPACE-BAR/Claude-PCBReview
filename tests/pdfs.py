"""Minimal PDFs, built in-process, so the datasheet tests need no datasheet.

Why this exists
---------------
The reader was written against three real documents and every lesson in it came
from one of them: a footnote marker glued to a word and read as a measurement, a
thermal row carrying no unit at all, and a five-page scan with no text layer.
Those are the cases worth testing and **none of them can ship** — a datasheet in
the distribution names a part, and four part numbers in one place identify a
board as surely as its name does (`packaging/scan.py`).

So the lines are reproduced and the documents are not. `text_pdf()` writes a
real PDF carrying exactly the lines a test asks for, and `image_pdf()` writes
one with a page and no text at all, which is what a scan looks like to
`pdfplumber`. Both are a few hundred bytes and neither names anything.

The generator is deliberately the dullest correct PDF there is: uncompressed
content streams, `Helvetica`, one `Tj` per line, and a real cross-reference
table with real byte offsets. pdfminer will repair a broken xref, and a fixture
that leant on that would be testing the repair path rather than the reader.
"""

from __future__ import annotations

from pathlib import Path


def _escape(line: str) -> str:
    """A PDF literal string. Only three characters need it."""
    return (line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)"))


def _content(lines: list[str]) -> bytes:
    body = ["BT", "/F1 10 Tf", "1 0 0 1 40 750 Tm", "12 TL"]
    for line in lines:
        body.append(f"({_escape(line)}) Tj")
        body.append("T*")
    body.append("ET")
    return "\n".join(body).encode("latin-1", "replace")


def build(pages: list[list[str]]) -> bytes:
    """A PDF of `len(pages)` pages, each carrying its own lines.

    A page with an empty list gets a content stream with no text-showing
    operator in it, which is how a scanned page reads: it exists, it has a
    size, and `extract_text()` returns nothing for it.
    """
    objects: list[bytes] = []

    def add(raw: bytes) -> int:
        objects.append(raw)
        return len(objects)          # 1-based object number

    catalog = add(b"")               # 1, patched below once Pages is known
    pages_obj = add(b"")             # 2, likewise
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    kids: list[int] = []
    for lines in pages:
        stream = _content(lines)
        content = add(b"<< /Length " + str(len(stream)).encode() + b" >>\n"
                      b"stream\n" + stream + b"\nendstream")
        page = add(b"<< /Type /Page /Parent " + str(pages_obj).encode()
                   + b" 0 R /MediaBox [0 0 612 792] /Contents "
                   + str(content).encode() + b" 0 R /Resources << /Font << /F1 "
                   + str(font).encode() + b" 0 R >> >> >>")
        kids.append(page)

    objects[catalog - 1] = (b"<< /Type /Catalog /Pages "
                            + str(pages_obj).encode() + b" 0 R >>")
    objects[pages_obj - 1] = (
        b"<< /Type /Pages /Kids [" +
        b" ".join(str(k).encode() + b" 0 R" for k in kids) +
        b"] /Count " + str(len(kids)).encode() + b" >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, raw in enumerate(objects, start=1):
        offsets.append(len(out))
        out += str(number).encode() + b" 0 obj\n" + raw + b"\nendobj\n"

    start = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n"
    out += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (b"trailer\n<< /Size " + str(len(objects) + 1).encode()
            + b" /Root " + str(catalog).encode() + b" 0 R >>\nstartxref\n"
            + str(start).encode() + b"\n%%EOF\n")
    return bytes(out)


def text_pdf(path: Path | str, pages: list[list[str]]) -> Path:
    """Write a readable PDF at `path` and return it."""
    path = Path(path)
    path.write_bytes(build(pages))
    return path


def image_pdf(path: Path | str, pages: int = 5) -> Path:
    """Write a PDF with pages and no text — what a scan looks like.

    Not literally an image: an image would need a filter and a colour space and
    would prove nothing extra. What the reader keys on is that `extract_text()`
    comes back empty for every page, and this produces exactly that.

    It is also the case that caught a real bug: a scanned document yields one
    **empty string per page**, so `not pages` was False and the vision path was
    never taken.
    """
    return text_pdf(path, [[] for _ in range(pages)])
