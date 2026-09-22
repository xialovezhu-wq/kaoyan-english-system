from __future__ import annotations

import mimetypes
import re
from pathlib import Path
from typing import Any

from .errors import IdempotencyConflict, ValidationError
from .packages import _write_immutable, validate_conversation_package
from .util import (
    atomic_write_json,
    atomic_write_text,
    file_sha256,
    load_json,
    object_sha256,
    utc_now,
)


DISPLAY_PLAN_SCHEMA_VERSION = "english_display_asset_plan_v1"
DISPLAY_RECEIPT_SCHEMA_VERSION = "english_display_asset_closure_receipt_v1"

PRACTICE_SAFE_ROLES = {
    "question_image",
    "source_article_image",
    "user_work_image",
}
PROTECTED_ROLES = {"solution_image", "explanation_image"}
ARCHIVE_ONLY_ROLES = {"other_attachment"}

_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")
_BEGIN = "<!-- english-display-assets:begin {package_id} -->"
_END = "<!-- english-display-assets:end {package_id} -->"


class SimulatedDisplayAssetFailure(RuntimeError):
    """Test-only failure injected at a durable display-asset boundary."""


def _safe_id(value: str, *, label: str) -> str:
    cleaned = _SAFE_ID_RE.sub("-", value.strip()).strip(".-")
    if not cleaned:
        raise ValidationError(f"{label} cannot produce a stable path component")
    return cleaned


def _repo_relative_file(repo_root: Path, locator: Any, *, required: bool) -> Path | None:
    if locator in {None, ""}:
        if required:
            raise ValidationError("display asset target article is required")
        return None
    if not isinstance(locator, str):
        raise ValidationError("display asset target article locator must be a string")
    relative = Path(locator)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValidationError("display asset target article must be repository-relative")
    root = repo_root.resolve(strict=True)
    try:
        resolved = (root / relative).resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ValidationError("display asset target article is outside the repository") from exc
    if resolved.is_symlink() or not resolved.is_file():
        raise ValidationError("display asset target article must be a regular file")
    return resolved


def _extension(row: dict[str, Any]) -> str:
    original = Path(str(row.get("path", ""))).suffix.lower()
    if re.fullmatch(r"\.[a-z0-9]{1,8}", original):
        return original
    guessed = mimetypes.guess_extension(str(row.get("mime_type", "")))
    return guessed if guessed and re.fullmatch(r"\.[a-z0-9]{1,8}", guessed) else ".bin"


def _host_for_visibility(
    repo_root: Path,
    *,
    visibility: str,
    source_id: str,
    package_id: str,
    article_path: Path | None,
) -> Path:
    if visibility == "practice_safe":
        if article_path is None:
            raise ValidationError("practice-safe display asset has no package-bound article")
        return article_path
    if visibility == "protected":
        return (
            repo_root
            / "raw"
            / "protected"
            / "conversation-assets"
            / source_id
            / package_id
            / "index.md"
        )
    raise ValidationError("display asset visibility is invalid")


def build_display_asset_plan(
    state_dir: Path,
    repo_root: Path,
    package_root: Path,
) -> tuple[Path, dict[str, Any]]:
    """Create a package-external deterministic display plan without changing the package."""
    state_root = state_dir.resolve()
    repo = repo_root.resolve(strict=True)
    package = package_root.resolve(strict=True)
    manifest = validate_conversation_package(package)
    source = load_json(package / "source.json").get("identity", {})
    if not isinstance(source, dict):
        raise ValidationError("conversation package source identity is invalid")
    source_id = _safe_id(str(source.get("source_id") or manifest["package_id"]), label="source_id")
    article_path = _repo_relative_file(repo, source.get("source_article"), required=False)
    article_relative = article_path.relative_to(repo).as_posix() if article_path else None
    rows: list[dict[str, Any]] = []
    for attachment in manifest.get("attachments", []):
        if not isinstance(attachment, dict):
            raise ValidationError("conversation package attachment descriptor is invalid")
        role = attachment.get("role")
        source_path = package / str(attachment.get("path", ""))
        if not source_path.is_file() or file_sha256(source_path) != attachment.get("sha256"):
            raise ValidationError("display asset source attachment differs from package manifest")
        protected_package = (
            source.get("answer_exposure") != "answer_free"
            or source.get("source_kind") == "explanation"
        )
        if role in PROTECTED_ROLES or (protected_package and role in PRACTICE_SAFE_ROLES):
            visibility = "protected"
            disposition = "promoted"
            base = repo / "raw" / "protected" / "conversation-assets" / source_id / manifest["package_id"]
        elif role in PRACTICE_SAFE_ROLES and article_path is not None:
            visibility = "practice_safe"
            disposition = "promoted"
            base = repo / "raw" / "articles" / "_display_assets" / source_id / manifest["package_id"]
        elif role in PRACTICE_SAFE_ROLES | ARCHIVE_ONLY_ROLES:
            visibility = "archive_only"
            disposition = "archive_only"
            base = None
        else:
            raise ValidationError(f"unsupported display attachment role: {role}")
        stable_path: str | None = None
        host_path: str | None = None
        if base is not None:
            stable = base / (
                f"{_safe_id(str(attachment['attachment_id']), label='attachment_id')}-"
                f"{str(attachment['sha256'])[:12]}{_extension(attachment)}"
            )
            stable_path = stable.relative_to(repo).as_posix()
            host = _host_for_visibility(
                repo,
                visibility=visibility,
                source_id=source_id,
                package_id=manifest["package_id"],
                article_path=article_path,
            )
            host_path = host.relative_to(repo).as_posix()
        rows.append(
            {
                "attachment_id": attachment["attachment_id"],
                "attachment_role": role,
                "package_relative_path": attachment["path"],
                "mime_type": attachment["mime_type"],
                "bytes": attachment["bytes"],
                "source_sha256": attachment["sha256"],
                "stable_vault_relative_path": stable_path,
                "host_markdown_relative_path": host_path,
                "visibility": visibility,
                "disposition": disposition,
            }
        )
    promoted = [row for row in rows if row["disposition"] == "promoted"]
    core = {
        "schema_version": DISPLAY_PLAN_SCHEMA_VERSION,
        "package_id": manifest["package_id"],
        "package_sha256": manifest["package_canonical_sha256"],
        "manifest_sha256": file_sha256(package / "manifest.json"),
        "study_date": manifest["study_date"],
        "source_id": str(source.get("source_id") or ""),
        "article_id": str(source.get("article_id") or source.get("source_id") or ""),
        "question_id": str(source.get("question_id") or ""),
        "target_article_relative_path": article_relative,
        "temporary_package_path": str(package),
        "assets": rows,
        "stable_asset_count": len(promoted),
        "no_display": not promoted,
        "formal_write_count": 0,
    }
    plan_id = "EN-DISPLAY-PLAN-" + object_sha256(core)[:16].upper()
    document = {**core, "plan_id": plan_id}
    path = state_root / "display-assets" / "plans" / manifest["study_date"] / f"{manifest['package_id']}.json"
    if path.exists():
        existing = load_json(path)
        if existing != document:
            raise IdempotencyConflict("display asset plan already binds different package evidence")
        return path, existing
    atomic_write_json(path, document)
    if load_json(path) != document:
        raise ValidationError("display asset plan failed durable reopen")
    return path, document


def _replace_managed_block(text: str, package_id: str, block: str) -> str:
    begin = _BEGIN.format(package_id=package_id)
    end = _END.format(package_id=package_id)
    pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end) + r"\n?", re.DOTALL)
    matches = list(pattern.finditer(text))
    if len(matches) > 1:
        raise ValidationError("article contains duplicate managed display blocks")
    if matches:
        return text[: matches[0].start()] + block + text[matches[0].end() :]
    return text.rstrip() + "\n\n" + block


def _render_article_block(plan: dict[str, Any]) -> str:
    safe = [row for row in plan["assets"] if row["visibility"] == "practice_safe"]
    begin = _BEGIN.format(package_id=plan["package_id"])
    end = _END.format(package_id=plan["package_id"])
    lines = [begin, "### 原始资料与归档", ""]
    lines.extend(f"![[{row['stable_vault_relative_path']}]]" for row in safe)
    lines.extend(["", f"- raw_archive_locator：[[wiki/raw_archives/{plan['package_id']}]]"])
    if any(row["visibility"] == "protected" for row in plan["assets"]):
        safe_source = _safe_id(plan["source_id"] or plan["package_id"], label="source_id")
        lines.append(
            "- protected_assets：[[raw/protected/conversation-assets/"
            f"{safe_source}/{plan['package_id']}/index]]"
        )
    lines.extend([end, ""])
    return "\n".join(lines)


def _render_protected_page(plan: dict[str, Any]) -> str:
    protected = [row for row in plan["assets"] if row["visibility"] == "protected"]
    lines = [
        "---",
        "type: english_protected_conversation_assets",
        f"package_id: {plan['package_id']}",
        f"source_id: {plan['source_id']}",
        "visibility: protected",
        "---",
        "",
        "# 受保护原始资料与归档",
        "",
        *[f"![[{row['stable_vault_relative_path']}]]" for row in protected],
        "",
        f"- raw_archive_locator：[[wiki/raw_archives/{plan['package_id']}]]",
        "",
    ]
    return "\n".join(lines)


def _scan_package_references(
    repo_root: Path,
    package_root: Path,
    plan: dict[str, Any],
    host_paths: list[Path],
) -> dict[str, Any]:
    temporary_path = str(plan.get("temporary_package_path") or package_root)
    needles = {temporary_path, "/Volumes/T9-Data"}
    try:
        needles.add(Path(temporary_path).resolve().relative_to(repo_root).as_posix())
    except (OSError, ValueError):
        pass
    violations: list[dict[str, str]] = []
    formal_files = set(host_paths)
    for relative_root in ("articles", "raw", "bank", "review", "wiki"):
        root = repo_root / relative_root
        if not root.is_dir():
            continue
        formal_files.update(
            path
            for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() in {".md", ".csv", ".json"}
        )
    for host in sorted(formal_files):
        try:
            text = host.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for needle in sorted(needles):
            if needle and needle in text:
                violations.append(
                    {"host_markdown_relative_path": host.relative_to(repo_root).as_posix(), "reference": needle}
                )
    core = {
        "package_id": plan["package_id"],
        "host_markdown_relative_paths": sorted(
            host.relative_to(repo_root).as_posix() for host in host_paths
        ),
        "violations": violations,
        "status": "PASS" if not violations else "FAIL",
    }
    return {**core, "scan_sha256": object_sha256(core)}


def publish_display_assets(
    state_dir: Path,
    repo_root: Path,
    package_root: Path,
    *,
    fault_at: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    repo = repo_root.resolve(strict=True)
    package = package_root.resolve(strict=True)
    plan_path, plan = build_display_asset_plan(state_dir, repo, package)
    receipt_path = (
        state_dir.resolve()
        / "receipts"
        / "display-assets"
        / plan["study_date"]
        / f"{plan['package_id']}.json"
    )
    if receipt_path.is_file():
        validate_display_asset_closure(state_dir, repo, package, receipt_path)
        return receipt_path, load_json(receipt_path)
    promoted = [row for row in plan["assets"] if row["disposition"] == "promoted"]
    for row in promoted:
        source = package / row["package_relative_path"]
        destination = repo / row["stable_vault_relative_path"]
        if destination.exists():
            if not destination.is_file() or file_sha256(destination) != row["source_sha256"]:
                raise IdempotencyConflict("stable display asset path contains different bytes")
            row["disposition"] = "already_stable"
        else:
            _write_immutable(destination, source.read_bytes())
        if file_sha256(destination) != row["source_sha256"] or destination.stat().st_size != row["bytes"]:
            raise ValidationError("stable display asset verification failed")
    if fault_at == "after_stable_copy":
        raise SimulatedDisplayAssetFailure(str(plan_path))

    host_paths: list[Path] = []
    article_relative = plan.get("target_article_relative_path")
    if article_relative:
        article = _repo_relative_file(repo, article_relative, required=True)
        assert article is not None
        original = article.read_text(encoding="utf-8")
        updated = _replace_managed_block(original, plan["package_id"], _render_article_block(plan))
        if updated != original:
            atomic_write_text(article, updated)
        host_paths.append(article)
    protected_rows = [row for row in promoted if row["visibility"] == "protected"]
    if protected_rows:
        protected_page = repo / protected_rows[0]["host_markdown_relative_path"]
        rendered = _render_protected_page(plan)
        if protected_page.exists() and protected_page.read_text(encoding="utf-8") != rendered:
            raise IdempotencyConflict("protected display page already binds different evidence")
        if not protected_page.exists():
            atomic_write_text(protected_page, rendered)
        host_paths.append(protected_page)
    if fault_at == "after_markdown_write":
        raise SimulatedDisplayAssetFailure(str(plan_path))

    asset_receipts: list[dict[str, Any]] = []
    for row in plan["assets"]:
        stable_sha: str | None = None
        embed_resolved = row["disposition"] == "archive_only"
        if row["stable_vault_relative_path"]:
            stable = repo / row["stable_vault_relative_path"]
            stable_sha = file_sha256(stable)
            host = repo / row["host_markdown_relative_path"]
            embed = f"![[{row['stable_vault_relative_path']}]]"
            embed_resolved = host.is_file() and embed in host.read_text(encoding="utf-8")
        if not embed_resolved:
            raise ValidationError("stable display asset is not embedded by its managed host")
        asset_receipts.append(
            {
                **row,
                "stable_file_sha256": stable_sha,
                "embed_resolved": embed_resolved,
            }
        )
    scan = _scan_package_references(repo, package, plan, host_paths)
    if scan["status"] != "PASS":
        raise ValidationError("formal display surface still references temporary or T9 paths")
    host_bindings = [
        {
            "markdown_relative_path": path.relative_to(repo).as_posix(),
            "markdown_sha256": file_sha256(path),
        }
        for path in sorted(set(host_paths))
    ]
    core = {
        "schema_version": DISPLAY_RECEIPT_SCHEMA_VERSION,
        "status": "PASS",
        "plan_id": plan["plan_id"],
        "plan_path": str(plan_path),
        "plan_sha256": file_sha256(plan_path),
        "package_id": plan["package_id"],
        "package_sha256": plan["package_sha256"],
        "manifest_sha256": plan["manifest_sha256"],
        "source_id": plan["source_id"],
        "article_id": plan["article_id"],
        "question_id": plan["question_id"],
        "assets": asset_receipts,
        "stable_asset_count": plan["stable_asset_count"],
        "no_display": plan["no_display"],
        "host_markdown": host_bindings,
        "formal_reference_scan": scan,
        "obsidian_reread": "PASS",
        "formal_write_count": 0,
    }
    receipt_id = "EN-DISPLAY-CLOSURE-" + object_sha256(core)[:16].upper()
    receipt = {**core, "receipt_id": receipt_id, "verified_at": utc_now()}
    if receipt_path.exists():
        existing = load_json(receipt_path)
        comparable_existing = {key: value for key, value in existing.items() if key != "verified_at"}
        comparable_receipt = {key: value for key, value in receipt.items() if key != "verified_at"}
        if comparable_existing != comparable_receipt:
            raise IdempotencyConflict("display closure receipt already binds different display evidence")
        receipt = existing
    else:
        atomic_write_json(receipt_path, receipt)
    validate_display_asset_closure(state_dir, repo, package, receipt_path)
    if fault_at == "after_display_receipt":
        raise SimulatedDisplayAssetFailure(str(receipt_path))
    return receipt_path, receipt


def validate_display_asset_closure(
    state_dir: Path,
    repo_root: Path,
    package_root: Path,
    receipt_path: Path,
) -> dict[str, Any]:
    del state_dir
    repo = repo_root.resolve(strict=True)
    package = package_root.resolve(strict=True)
    manifest = validate_conversation_package(package)
    receipt = load_json(receipt_path.resolve(strict=True))
    if (
        receipt.get("schema_version") != DISPLAY_RECEIPT_SCHEMA_VERSION
        or receipt.get("status") != "PASS"
        or receipt.get("package_id") != manifest["package_id"]
        or receipt.get("package_sha256") != manifest["package_canonical_sha256"]
        or receipt.get("manifest_sha256") != file_sha256(package / "manifest.json")
        or receipt.get("obsidian_reread") != "PASS"
    ):
        raise ValidationError("display closure receipt package binding mismatch")
    plan_path = Path(str(receipt.get("plan_path", ""))).resolve(strict=True)
    if file_sha256(plan_path) != receipt.get("plan_sha256"):
        raise ValidationError("display closure plan hash mismatch")
    plan = load_json(plan_path)
    if plan.get("plan_id") != receipt.get("plan_id"):
        raise ValidationError("display closure plan identity mismatch")
    stable_count = 0
    hosts: list[Path] = []
    for host in receipt.get("host_markdown", []):
        path = repo / str(host.get("markdown_relative_path", ""))
        path.resolve(strict=True).relative_to(repo)
        if not path.is_file():
            raise ValidationError("display closure host Markdown is missing")
        hosts.append(path)
    for row in receipt.get("assets", []):
        if row.get("disposition") == "archive_only":
            continue
        stable = repo / str(row.get("stable_vault_relative_path", ""))
        host = repo / str(row.get("host_markdown_relative_path", ""))
        if (
            not stable.is_file()
            or file_sha256(stable) != row.get("source_sha256")
            or row.get("stable_file_sha256") != row.get("source_sha256")
            or stable.stat().st_size != row.get("bytes")
            or not host.is_file()
            or f"![[{row['stable_vault_relative_path']}]]" not in host.read_text(encoding="utf-8")
        ):
            raise ValidationError("display closure stable asset or embed drift")
        stable_count += 1
    if stable_count != receipt.get("stable_asset_count"):
        raise ValidationError("display closure stable asset count mismatch")
    if bool(stable_count == 0) != bool(receipt.get("no_display")):
        raise ValidationError("display closure no_display proof mismatch")
    scan = _scan_package_references(repo, package, plan, hosts)
    if scan.get("status") != "PASS":
        raise ValidationError("display closure formal reference scan failed")
    return {
        "receipt_id": receipt["receipt_id"],
        "receipt_path": str(receipt_path.resolve(strict=True)),
        "receipt_sha256": file_sha256(receipt_path.resolve(strict=True)),
        "formal_reference_scan_sha256": scan["scan_sha256"],
        "stable_asset_count": stable_count,
        "no_display": receipt["no_display"],
    }
