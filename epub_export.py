"""Minimal EPUB rebuild for translated chapters (stdlib only).

The reader overlay translates live without touching the source file. This
module packages already-translated paragraph text into a minimal, valid
EPUB 2 container (mimetype + container.xml + OPF + NCX + one XHTML
chapter) so the user can keep the translated chapter offline.

No EPUB is ever parsed here — output only, so there is no zip-slip or
entity-expansion surface. All text is XML-escaped on write.
"""
import uuid
import zipfile
from io import BytesIO
from xml.sax.saxutils import escape

# Best-effort target-language names (as sent by the reader UI) to BCP47
# codes for the OPF dc:language element. Unknown names fall back to "en";
# the code selects only metadata, never rendering behaviour.
LANGUAGE_CODES = {
    "English": "en",
    "Spanish": "es",
    "French": "fr",
    "German": "de",
    "Portuguese": "pt",
    "Chinese": "zh",
    "Chinese (Traditional)": "zh-Hant",
    "Japanese": "ja",
    "Korean": "ko",
    "Russian": "ru",
    "Arabic": "ar",
    "Hindi": "hi",
    "Italian": "it",
    "Dutch": "nl",
    "Polish": "pl",
    "Turkish": "tr",
    "Ukrainian": "uk",
    "Vietnamese": "vi",
    "Thai": "th",
    "Indonesian": "id",
}

MAX_TITLE_CHARS = 200


def epub_filename(title: str) -> str:
    """Derive a safe ASCII attachment filename from a chapter title."""
    cleaned = "".join(
        ch if ch.isascii() and (ch.isalnum() or ch in ("-", "_")) else "_"
        for ch in (title or "").strip().lower().replace(" ", "_")
    ).strip("_")[:80]
    if not cleaned:
        cleaned = "translation"
    return f"{cleaned}.epub"


def build_epub(
    title: str,
    paragraphs: list,
    *,
    target_lang: str = "Spanish",
    identifier: str | None = None,
) -> bytes:
    """Rebuild a minimal EPUB 2 document from translated paragraphs.

    Raises ValueError on structurally invalid input; size caps (paragraph
    count, per-paragraph and total characters) are enforced by the server
    endpoint per repo conventions, not here.
    """
    if not isinstance(title, str) or not title.strip():
        raise ValueError("'title' must be a non-empty string")
    title = title.strip()
    if len(title) > MAX_TITLE_CHARS:
        raise ValueError(
            f"'title' exceeds the {MAX_TITLE_CHARS}-character limit"
        )
    if not isinstance(paragraphs, list) or not paragraphs:
        raise ValueError("'paragraphs' must be a non-empty list")
    if not all(isinstance(paragraph, str) for paragraph in paragraphs):
        raise ValueError("All 'paragraphs' entries must be strings")
    bodies = [paragraph.strip() for paragraph in paragraphs]
    bodies = [body for body in bodies if body]
    if not bodies:
        raise ValueError("'paragraphs' must contain at least one non-empty entry")

    book_id = identifier or uuid.uuid4().hex
    language = LANGUAGE_CODES.get(target_lang, "en")
    esc_title = escape(title)
    items = "\n".join(f"    <p>{escape(body)}</p>" for body in bodies)

    container_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<container version="1.0" '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        "  <rootfiles>\n"
        '    <rootfile full-path="OEBPS/content.opf" '
        'media-type="application/oebps-package+xml"/>\n'
        "  </rootfiles>\n"
        "</container>\n"
    )
    content_opf = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<package version="2.0" '
        'xmlns="http://www.idpf.org/2007/opf" '
        f'unique-identifier="bookid">\n'
        "  <metadata "
        'xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f"    <dc:title>{esc_title}</dc:title>\n"
        f"    <dc:language>{language}</dc:language>\n"
        f'    <dc:identifier id="bookid">urn:uuid:{book_id}</dc:identifier>\n'
        "  </metadata>\n"
        "  <manifest>\n"
        '    <item id="chapter" href="chapter.xhtml" '
        'media-type="application/xhtml+xml"/>\n'
        '    <item id="ncx" href="toc.ncx" '
        'media-type="application/x-dtbncx+xml"/>\n'
        "  </manifest>\n"
        '  <spine toc="ncx">\n'
        '    <itemref idref="chapter"/>\n'
        "  </spine>\n"
        "</package>\n"
    )
    toc_ncx = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
        "  <head>\n"
        f'    <meta name="dtb:uid" content="urn:uuid:{book_id}"/>\n'
        "  </head>\n"
        f"  <docTitle><text>{esc_title}</text></docTitle>\n"
        "  <navMap>\n"
        '    <navPoint id="chapter" playOrder="1">\n'
        f"      <navLabel><text>{esc_title}</text></navLabel>\n"
        '      <content src="chapter.xhtml"/>\n'
        "    </navPoint>\n"
        "  </navMap>\n"
        "</ncx>\n"
    )
    chapter_xhtml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" '
        '"http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\n'
        '<html xmlns="http://www.w3.org/1999/xhtml">\n'
        "<head>\n"
        f"<title>{esc_title}</title>\n"
        "</head>\n"
        "<body>\n"
        f"<h1>{esc_title}</h1>\n"
        f"{items}\n"
        "</body>\n"
        "</html>\n"
    )

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        # The mimetype entry must be first and uncompressed per the EPUB spec.
        archive.writestr(
            "mimetype",
            "application/epub+zip",
            compress_type=zipfile.ZIP_STORED,
        )
        archive.writestr(
            "META-INF/container.xml",
            container_xml,
            compress_type=zipfile.ZIP_DEFLATED,
        )
        archive.writestr(
            "OEBPS/content.opf",
            content_opf,
            compress_type=zipfile.ZIP_DEFLATED,
        )
        archive.writestr(
            "OEBPS/toc.ncx",
            toc_ncx,
            compress_type=zipfile.ZIP_DEFLATED,
        )
        archive.writestr(
            "OEBPS/chapter.xhtml",
            chapter_xhtml,
            compress_type=zipfile.ZIP_DEFLATED,
        )
    return buffer.getvalue()
