"""Describe the two Codex surfaces for one local English teaching page."""
import html
import json
import subprocess
from pathlib import Path

from .errors import ValidationError
from .util import atomic_write_bytes, bytes_sha256, text_sha256


REVISION_DIR = "english-lesson-revisions"


def check_lesson_output(page: Path) -> None:
    if REVISION_DIR in page.resolve().parent.parts:
        raise ValidationError("choose a working HTML path outside the saved lesson revisions")


def display_targets(page: Path, *, title: str | None = None, preview_root: Path | None = None,
                    fragment: bytes | None = None) -> dict:
    """Describe inline display and, when requested, a sandboxed loopback web preview."""
    if not page.is_absolute() or page.suffix.lower() != ".html" or not page.is_file():
        raise ValidationError("English lesson display requires an existing absolute HTML file")
    page = page.resolve(strict=True)
    data = page.read_bytes() if fragment is None else fragment
    digest = bytes_sha256(data)
    result = {}
    if preview_root is not None:
        from .lesson_preview import lesson_url
        try:
            url, shown = lesson_url(page, preview_root, fragment=fragment)
            if shown != digest:
                raise ValueError("the preview changed while selecting the lesson revision")
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            result.update(panel_status="preview_unavailable", panel_reason=str(exc))
            if fragment is None:
                return result
        else:
            result.update(panel_status="preview_ready", panel_revision=digest, panel_url=url,
                          open_in_codex={"target": {"type": "browser", "url": url}, "placement": "right"})

    result.update(inline_revision(page.parent, data, title=title))
    return result


def inline_revision(directory: Path, data: bytes, *, title: str | None = None) -> dict:
    """Keep the exact authorized fragment addressable from this chat and later Capture."""
    if not directory.is_absolute() or directory.is_symlink() or REVISION_DIR in directory.parts:
        raise ValidationError("invalid inline visualization directory")
    digest = bytes_sha256(data)
    folder = directory / REVISION_DIR
    revision = folder / f"{digest}.html"
    if folder.is_symlink() or revision.is_symlink():
        raise ValidationError("invalid lesson revision path")
    if revision.exists():
        if revision.read_bytes() != data:
            raise ValidationError("saved lesson revision differs from its digest")
    else:
        atomic_write_bytes(revision, data)
    source = {"path": str(revision)}
    if title:
        source["title"] = title
    return {"visualize_reference": "\ue200visualize\ue202" + json.dumps(source, ensure_ascii=False) + "\ue201",
            "revision_sha256": digest}


def export_reviewed_text(result: dict, page: Path, inline_root: Path) -> dict:
    """Display an already authorized text reading on both surfaces without constructing a diagram."""
    if (result.get("status") != "text_fallback" or result.get("reason") != "reviewed_text_sufficient"
            or result.get("freshness") != "current"):
        return result
    if (not inline_root.is_absolute() or not page.is_absolute() or page.is_symlink()
            or page.suffix != ".html" or not page.resolve().is_relative_to(inline_root.resolve())):
        raise ValidationError("English lesson page must be inside its inline directory")
    check_lesson_output(page)
    original, meaning = result["original_sentence"], result["translation"]
    focus = result.get("translation_focus")
    translated = html.escape(meaning)
    if focus and meaning.count(focus) == 1:
        left, right = meaning.split(focus, 1)
        translated = f"{html.escape(left)}<strong>{html.escape(focus)}</strong>{html.escape(right)}"
    cue = result.get("reading_cue")
    ident = "english-reading-" + text_sha256(str(page.resolve()))[:12]
    lines = [f'<section id="{ident}" lang="zh-CN"><p lang="en">{html.escape(original)}</p><p>{translated}</p>']
    if cue:
        lines.append(f"<p>{html.escape(cue)}</p>")
    lines.append("</section>")
    fragment = "\n".join(lines).encode()
    atomic_write_bytes(page, fragment)
    return {**result, "output_path": str(page.resolve()),
            **display_targets(page, title="原句与译文", preview_root=inline_root, fragment=fragment)}
