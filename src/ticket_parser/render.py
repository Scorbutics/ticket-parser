"""Jinja2 -> self-contained HTML -> Chromium PDF.

Chromium rather than WeasyPrint because WhatsApp content is emoji-heavy and
needs colour emoji rendering. Images are inlined as base64 data URIs so the
intermediate HTML stands alone and can be opened in a browser for debugging.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .models import Batch, ChatMessage

log = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"
TEMPLATE_NAME = "report.html.j2"

HEIC_TYPES = {"image/heic", "image/heif"}


class RenderError(RuntimeError):
    pass


def _convert_heic(path: Path) -> tuple[bytes, str] | None:
    """Chromium will not render HEIC; transcode to JPEG or give up loudly."""
    try:
        import pillow_heif
        from PIL import Image

        pillow_heif.register_heif_opener()
        import io

        with Image.open(path) as image:
            buffer = io.BytesIO()
            image.convert("RGB").save(buffer, format="JPEG", quality=85)
            return buffer.getvalue(), "image/jpeg"
    except Exception as exc:
        log.error("HEIC conversion failed", extra={"file": str(path), "error": str(exc)})
        return None


def data_uri_for(media_path: Path, mimetype: str = "") -> str | None:
    if not media_path.is_file():
        return None
    guessed = mimetype or mimetypes.guess_type(media_path.name)[0] or ""
    if guessed.lower() in HEIC_TYPES or media_path.suffix.lower() in {".heic", ".heif"}:
        converted = _convert_heic(media_path)
        if converted is None:
            return None
        payload, guessed = converted
    else:
        payload = media_path.read_bytes()
    if not guessed.startswith("image/"):
        return None
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:{guessed};base64,{encoded}"


def build_data_uris(messages: list[ChatMessage]) -> dict[str, str]:
    uris: dict[str, str] = {}
    for message in messages:
        if message.media is None or message.media.filename in uris:
            continue
        if not message.media.is_image:
            # Audio isn't playable in a PDF -- its transcript is rendered
            # instead, so there is nothing to inline here.
            continue
        uri = data_uri_for(message.media.path, message.media.mimetype)
        if uri:
            uris[message.media.filename] = uri
        else:
            log.warning(
                "attachment could not be inlined",
                extra={"file": message.media.filename},
            )
    return uris


def document_segments(message: ChatMessage, text: str) -> list[dict]:
    """Document text with its figures inlined, for the template.

    [{"text": …}] and [{"image": name, "uri": data-URI or ""}] in order. A
    figure whose file is missing keeps its place, as a marked gap.
    """
    out: list[dict] = []
    for kind, value in message.segments(text):
        if kind == "text":
            out.append({"text": value})
            continue
        path = message.image_path(value)
        uri = data_uri_for(path, mimetypes.guess_type(value)[0] or "") if path else None
        out.append({"image": value, "uri": uri or ""})
    return out


def render_html(batch: Batch, template_dir: Path | None = None) -> str:
    env = Environment(
        loader=FileSystemLoader(str(template_dir or TEMPLATE_DIR)),
        # Unconditional, not select_autoescape(): that keys off the file
        # extension, and ".html.j2" ends in ".j2", so it would silently leave
        # escaping off and inject message text and model prose raw.
        autoescape=True,
        undefined=StrictUndefined,
    )
    env.globals["segments"] = document_segments
    template = env.get_template(TEMPLATE_NAME)
    return template.render(batch=batch, data_uris=build_data_uris(batch.messages))


def html_to_pdf(html: str, output_path: Path) -> Path:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise RenderError(
            "playwright is not installed; run `pip install -e .` "
            "and `playwright install chromium`"
        ) from exc

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content(html, wait_until="load")
            page.pdf(
                path=str(output_path),
                format="A4",
                print_background=True,
                margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
            )
        finally:
            browser.close()
    return output_path


def render_batch(
    batch: Batch, out_dir: Path, *, dry_run: bool = False
) -> tuple[Path | None, str]:
    """Render a batch. Returns (pdf_path, html). No PDF is written on a dry run."""
    html = render_html(batch)
    if dry_run:
        log.info("dry-run: PDF not written", extra={"batch": batch.batch_id})
        return None, html
    pdf_path = out_dir / f"{batch.batch_id}-report.pdf"
    html_to_pdf(html, pdf_path)
    log.info(
        "pdf written",
        extra={"path": str(pdf_path), "pages": len(batch.issues) + 1},
    )
    return pdf_path, html
