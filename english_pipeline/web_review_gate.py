"""Small admission gate shared by web review and the existing formal writer."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .util import file_sha256, load_json


def managed_package(state: Path, package: dict[str, Any]) -> bool:
    if package.get("source_identity", {}).get("review_route") == "web":
        return True
    hold = state / "web-review" / "holds" / (package["package_id"] + ".json")
    if not hold.exists():
        return False
    row = load_json(hold)
    if row.get("package_sha256") != package["package_canonical_sha256"]:
        raise ValidationError("web review hold package hash differs")
    return True


def approval(state: Path, review_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"EN-REVIEW-[A-F0-9]{24}", review_id):
        raise ValidationError("invalid English web review ID")
    path = state / "web-review" / "reviews" / review_id / "approval.json"
    row = load_json(path)
    original = path.parent / "review.json"
    if row.get("review_id") != review_id or file_sha256(original) != row.get("review_sha256"):
        raise ValidationError("web review approval binding differs")
    imported = state / "web-review" / "returns" / row["return_id"]
    if file_sha256(imported / "return.zip") != row.get("return_zip_sha256"):
        raise ValidationError("original web return bytes changed")
    return row


def allowed_packages(state: Path, review_id: str) -> dict[str, str]:
    row = approval(state, review_id)
    return {p["package_id"]: p["package_sha256"] for p in row["packages"]}


def validate_writer_admission(state: Path, manifest: dict[str, Any], actions_path: Path) -> None:
    if manifest.get("local_review_id"):
        if manifest.get("web_review_id"):
            raise ValidationError("cannot combine native local and web review admission")
        from .local_review import validate_local_writer_admission
        validate_local_writer_admission(state, manifest, actions_path)
        return
    from .packages import validate_conversation_package
    managed = [p for p in manifest.get("package_documents", [])
               if managed_package(state, validate_conversation_package(Path(p["path"])))]
    if not managed:
        return
    review_id = manifest.get("web_review_id")
    if not review_id:
        raise ValidationError("WAITING_WEB_REVIEW: package requires its reviewed web return")
    allowed = allowed_packages(state, review_id)
    for p in managed:
        if allowed.get(p["package_id"]) != p["package_sha256"]:
            raise ValidationError("web review did not approve this exact package")
    day = state / "web-review" / "reviews" / review_id / manifest["study_date"] / "prepared.json"
    prepared = load_json(day)
    if (prepared.get("actions_sha256") != file_sha256(actions_path)
            or prepared.get("batch_id") != manifest["batch_id"]):
        raise ValidationError("formal actions differ from the locally reviewed web return")
