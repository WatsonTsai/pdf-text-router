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


# Maps the printable ASCII range onto U+F020..U+F07E, the private-use area
# that symbol fonts without a real encoding land in. Every engine honours a
# ToUnicode CMap, so this is the portable way to fake such a text layer.
_PUA_CMAP = b"""/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CMapName /Adobe-Identity-UCS def
/CMapType 2 def
1 begincodespacerange
<00> <FF>
endcodespacerange
1 beginbfrange
<20> <7E> <F020>
endbfrange
endcmap
CMapName currentdict /CMap defineresource pop
end
end
"""


PAGE_W, PAGE_H = 595, 842   # A4 in points

# One grey pixel, uncompressed. Drawn through a `cm` matrix it becomes a
# rectangle of any size, which is all the image-area measurement looks at.
_IMAGE_XOBJECT = (b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 "
                  b"/ColorSpace /DeviceGray /BitsPerComponent 8 /Length 1 >>\n"
                  b"stream\n\x80\nendstream")


def _image_draw(fraction):
    """Content that paints /Im1 as a centred rectangle covering `fraction`
    of the page area (same aspect ratio as the page)."""
    k = fraction ** 0.5
    w, h = PAGE_W * k, PAGE_H * k
    x, y = (PAGE_W - w) / 2.0, (PAGE_H - h) / 2.0
    return b"q %.2f 0 0 %.2f %.2f %.2f cm /Im1 Do Q\n" % (w, h, x, y)


def text_pdf(pages=1, line="The quick brown fox jumps over the lazy dog. ",
             lines_per_page=6, blank=(), pua=False, images=None, lines_on=None):
    """A PDF with a genuine text layer.

    blank:    1-based page numbers that get no text (an empty content
              stream unless the page also has an image).
    pua:      route the text through a ToUnicode CMap into U+F0xx, the
              private-use area, so the "text layer" is unusable.
    images:   {page: fraction} -- draw one image covering that share of the
              page area (0.7 -> 70%). Combine with `blank` for an image-only
              page, with `lines_on` for a figure with a short caption.
    lines_on: {page: n} -- override lines_per_page for those pages.
    """
    kids = " ".join("%d 0 R" % (3 + i * 2) for i in range(pages))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids.encode(), pages),
    ]
    font_num = 3 + pages * 2
    image_num = font_num + (2 if pua else 1)
    blank = set(blank)
    images = images or {}
    lines_on = lines_on or {}
    for i in range(pages):
        pno = i + 1
        content = b""
        resources = []
        if images.get(pno):
            content += _image_draw(images[pno])
            resources.append(b"/XObject << /Im1 %d 0 R >>" % image_num)
        if pno not in blank:
            content += b"BT /F1 12 Tf\n"
            for n in range(lines_on.get(pno, lines_per_page)):
                content += b"1 0 0 1 50 %d Tm (%s) Tj\n" % (
                    800 - n * 20, ("p%d %s" % (pno, line)).encode("latin-1"))
            content += b"ET\n"
            resources.append(b"/Font << /F1 %d 0 R >>" % font_num)
        res = (b" /Resources << %s >>" % b" ".join(resources)) if resources else b""
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d]%s /Contents %d 0 R >>"
            % (PAGE_W, PAGE_H, res, 4 + i * 2))
        objects.append(b"<< /Length %d >>\nstream\n" % len(content)
                       + content + b"endstream")
    if pua:
        objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                       b"/ToUnicode %d 0 R >>" % (font_num + 1))
        objects.append(b"<< /Length %d >>\nstream\n" % len(_PUA_CMAP)
                       + _PUA_CMAP + b"endstream")
    else:
        objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    if images:
        objects.append(_IMAGE_XOBJECT)
    return _build(objects)


def mixed_pdf():
    """Four pages, one of each class: 1 text, 2 blank, 3 image (70% picture,
    no text), 4 figure (70% picture, one ~50-char caption)."""
    return text_pdf(4, blank=[2, 3], images={3: 0.7, 4: 0.7}, lines_on={4: 1})


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


def encrypted_pdf(data, user_password, owner_password="owner"):
    """Encrypt an in-memory PDF with pypdf (RC4-128, no extra dependency).
    An empty user_password gives an owner-password-only file, which every
    engine opens and extracts without asking."""
    import io
    from pypdf import PdfReader, PdfWriter
    w = PdfWriter()
    w.append(PdfReader(io.BytesIO(data)))
    w.encrypt(user_password=user_password, owner_password=owner_password)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def write(tmpdir, name, data):
    import os
    path = os.path.join(tmpdir, name)
    with open(path, "wb") as fh:
        fh.write(data)
    return path
