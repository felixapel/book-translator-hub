"""Minimal EPUB rebuild for translated chapters (stdlib only).

The reader overlay translates live without touching the source file. This
module packages already-translated paragraph text into a minimal, valid
EPUB 2 container (mimetype + container.xml + OPF + NCX + one XHTML
chapter) so the user can keep the translated chapter offline.

No EPUB is ever parsed here — output only, so there is no zip-slip or
entity-expansion surface. All text is XML-escaped on write.
"""
import re
import uuid
import zipfile
from io import BytesIO
from xml.sax.saxutils import escape

# Target-language names (the server's VALID_LANGUAGES) to BCP 47 tags for
# dc:language and the chapter's lang attributes. The server validates the
# name first; an unknown name can only reach here from a direct caller.
LANGUAGE_CODES = {
    "Afrikaans": "af", "Albanian": "sq", "Amharic": "am", "Arabic": "ar",
    "Aymara": "ay", "Basque": "eu", "Bengali": "bn", "Bosnian": "bs",
    "Bulgarian": "bg", "Burmese": "my", "Catalan": "ca", "Cebuano": "ceb",
    "Chewa": "ny", "Chinese": "zh", "Chinese (Traditional)": "zh-Hant",
    "Croatian": "hr", "Czech": "cs", "Danish": "da", "Dutch": "nl",
    "English": "en", "Esperanto": "eo", "Estonian": "et", "Finnish": "fi",
    "French": "fr", "Gaelic": "gd", "Galician": "gl", "Ganda": "lg",
    "German": "de", "Greek": "el", "Guarani": "gn", "Gujarati": "gu",
    "Hausa": "ha", "Hawaiian": "haw", "Hebrew": "he", "Hindi": "hi",
    "Hungarian": "hu", "Icelandic": "is", "Igbo": "ig", "Indonesian": "id",
    "Italian": "it", "Japanese": "ja", "Javanese": "jv", "Kannada": "kn",
    "Kazakh": "kk", "Khmer": "km", "Korean": "ko", "Kyrgyz": "ky",
    "Lao": "lo", "Latin": "la", "Latvian": "lv", "Lingala": "ln",
    "Lithuanian": "lt", "Macedonian": "mk", "Maithili": "mai",
    "Malagasy": "mg", "Malay": "ms", "Malayalam": "ml", "Maori": "mi",
    "Marathi": "mr", "Mongolian": "mn", "Nahuatl": "nah", "Navajo": "nv",
    "Nepali": "ne", "Norwegian": "no", "Odia": "or", "Oromo": "om",
    "Pashto": "ps", "Persian": "fa", "Polish": "pl", "Portuguese": "pt",
    "Punjabi": "pa", "Quechua": "qu", "Romanian": "ro", "Russian": "ru",
    "Samoan": "sm", "Serbian": "sr", "Shona": "sn", "Sindhi": "sd",
    "Sinhala": "si", "Slovak": "sk", "Slovenian": "sl", "Somali": "so",
    "Spanish": "es", "Sundanese": "su", "Swahili": "sw", "Swedish": "sv",
    "Tagalog": "tl", "Tajik": "tg", "Tamil": "ta", "Telugu": "te",
    "Thai": "th", "Tibetan": "bo", "Turkish": "tr", "Turkmen": "tk",
    "Ukrainian": "uk", "Urdu": "ur", "Uzbek": "uz", "Vietnamese": "vi",
    "Welsh": "cy", "Xhosa": "xh", "Yoruba": "yo", "Zulu": "zu",
}

# Characters XML 1.0 cannot represent at all, even escaped. Translated text
# may carry them from the source book; the EPUB drops them so every document
# stays well-formed.
_XML_INVALID_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


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
    if isinstance(title, str):
        title = _XML_INVALID_RE.sub("", title).strip()
    if not isinstance(title, str) or not title:
        raise ValueError("'title' must be a non-empty string")
    if len(title) > MAX_TITLE_CHARS:
        raise ValueError(
            f"'title' exceeds the {MAX_TITLE_CHARS}-character limit"
        )
    if not isinstance(paragraphs, list) or not paragraphs:
        raise ValueError("'paragraphs' must be a non-empty list")
    if not all(isinstance(paragraph, str) for paragraph in paragraphs):
        raise ValueError("All 'paragraphs' entries must be strings")
    bodies = [_XML_INVALID_RE.sub("", paragraph).strip() for paragraph in paragraphs]
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
        f'<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{language}" lang="{language}">\n'
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
