"""Build minimal PDFs in memory so the test suite needs no sample files.

Offsets in the xref table are computed rather than hard-coded, because a
wrong xref makes some parsers silently rebuild the file and others reject it
outright -- either way the test would stop testing what it claims to.
"""


def _build(objects):
    header = b"%PDF-1.4\n"
    body, offsets = b"", []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(header) + len(body))
        body += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    start = len(header) + len(body)
    xref = b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        xref += b"%010d 00000 n \n" % off
    trailer = (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
               % (len(objects) + 1, start))
    return header + body + xref + trailer


def text_pdf(pages=1, line="The quick brown fox jumps over the lazy dog. ",
             lines_per_page=6):
    """A PDF with a genuine text layer."""
    kids = " ".join("%d 0 R" % (3 + i * 2) for i in range(pages))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids.encode(), pages),
    ]
    font_num = 3 + pages * 2
    for i in range(pages):
        content = b"BT /F1 12 Tf\n"
        for n in range(lines_per_page):
            content += b"1 0 0 1 50 %d Tm (%s) Tj\n" % (
                800 - n * 20, ("p%d %s" % (i + 1, line)).encode("latin-1"))
        content += b"ET\n"
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
            % (font_num, 4 + i * 2))
        objects.append(b"<< /Length %d >>\nstream\n" % len(content)
                       + content + b"endstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    return _build(objects)


def blank_pdf(pages=1):
    """A PDF with no text layer at all -- what a scan looks like to a parser."""
    kids = " ".join("%d 0 R" % (3 + i) for i in range(pages))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids.encode(), pages),
    ]
    for _ in range(pages):
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] >>")
    return _build(objects)


def write(tmpdir, name, data):
    import os
    path = os.path.join(tmpdir, name)
    with open(path, "wb") as fh:
        fh.write(data)
    return path
