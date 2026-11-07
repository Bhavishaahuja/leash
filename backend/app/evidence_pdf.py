# A small, plain PDF of the evidence packet, for PayPal's provide-evidence file part.
#
# Hand-rolled like spike 3's, so there's no PDF library to install: Helvetica text, wrapped
# lines, as many letter-size pages as it takes.
#
# PayPal's file rules (developer.paypal.com/disputes/supported-file-sizes-types):
#   .jpg .jpeg .gif .png .pdf only, under 10 MB per file, up to 50 MB per call.
# document.name in the Disputes v1 schema: letters, digits, - _ , and spaces, then exactly one
# dot and a letters-only extension.

import re
import textwrap

MAX_FILE_BYTES = 10 * 1024 * 1024
# about what fits across a letter page in 10pt Helvetica with inch margins
WRAP_AT = 95
LINES_PER_PAGE = 52
FONT_SIZE = 10
LINE_HEIGHT = 13
TOP_Y = 740
LEFT_X = 60

FILE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9\-_,\s]+[.]{1}[A-Za-z]+$")


class EvidenceFileTooBig(Exception):
    pass


def evidence_file_name(paypal_dispute_id):
    # "leash-evidence-PP-R-ABC-10001234.pdf". anything outside the allowed characters becomes -
    safe_id = re.sub(r"[^A-Za-z0-9\-_]", "-", paypal_dispute_id)
    name = "leash-evidence-" + safe_id + ".pdf"
    # can't fail with the substitution above, this is a guard for whoever edits it next
    if not FILE_NAME_PATTERN.match(name):
        raise ValueError("Evidence file name doesn't fit PayPal's pattern: " + name)
    return name


def pdf_escape(text):
    # backslash and parens are special inside a PDF string
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def latin1_safe(text):
    # the built-in Helvetica only has latin-1. a smart quote or an emoji in the shopper's words
    # becomes ? instead of breaking the file
    return text.encode("latin-1", errors="replace").decode("latin-1")


def wrap_lines(lines):
    wrapped = []
    for line in lines:
        line = latin1_safe(line)
        if not line.strip():
            wrapped.append("")
            continue
        # keep the indent on continuation lines so nested fields still read as nested
        indent = len(line) - len(line.lstrip(" "))
        pieces = textwrap.wrap(
            line,
            width=WRAP_AT,
            subsequent_indent=" " * (indent + 2),
            break_long_words=True,
            break_on_hyphens=False,
        )
        for piece in pieces:
            wrapped.append(piece)
    return wrapped


def page_stream(page_lines):
    text_ops = (
        "BT /F1 " + str(FONT_SIZE) + " Tf " + str(LEFT_X) + " " + str(TOP_Y) + " Td "
        + str(LINE_HEIGHT) + " TL\n"
    )
    for line in page_lines:
        text_ops += "(" + pdf_escape(line) + ") Tj T*\n"
    text_ops += "ET"
    return text_ops.encode("latin-1")


def build_pdf(lines):
    wrapped = wrap_lines(lines)
    pages = []
    for start in range(0, max(len(wrapped), 1), LINES_PER_PAGE):
        pages.append(wrapped[start:start + LINES_PER_PAGE])

    # objects: 1 catalog, 2 pages, 3 font, then a page object and a content stream per page
    page_count = len(pages)
    kids = []
    for index in range(page_count):
        kids.append(str(4 + index * 2) + " 0 R")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [" + " ".join(kids).encode() + b"] /Count "
        + str(page_count).encode() + b" >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    for index, page_lines in enumerate(pages):
        content_number = 5 + index * 2
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents "
            + str(content_number).encode() + b" 0 R /Resources << /Font << /F1 3 0 R >> >> >>"
        )
        stream = page_stream(page_lines)
        objects.append(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream
            + b"\nendstream"
        )

    pdf = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf += str(number).encode() + b" 0 obj\n" + body + b"\nendobj\n"

    # the xref table is the byte offset of each object, 20 bytes per entry
    xref_start = len(pdf)
    pdf += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n"
    pdf += b"0000000000 65535 f \n"
    for offset in offsets:
        pdf += ("%010d 00000 n \n" % offset).encode()
    pdf += b"trailer\n<< /Size " + str(len(objects) + 1).encode() + b" /Root 1 0 R >>\n"
    pdf += b"startxref\n" + str(xref_start).encode() + b"\n%%EOF\n"

    # a packet is a few KB. this would take tens of thousands of pages, but PayPal would reject
    # it, so say so here with a clear message
    if len(pdf) >= MAX_FILE_BYTES:
        raise EvidenceFileTooBig("Evidence PDF is " + str(len(pdf)) + " bytes, PayPal's limit is "
                                 "under 10 MB")
    return pdf
