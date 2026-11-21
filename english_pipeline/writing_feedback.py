"""Source-bound formative feedback. These private comparisons never create a learner attempt."""
from datetime import datetime
from pathlib import Path

from .util import atomic_write_json, exclusive_lock, load_json, object_sha256, parse_iso_date
from .web_review import package_root
from .writing_sources import digest_message, valid

OUTCOMES = {"addressed": "改到了反馈指出的问题", "partial": "改到了一部分", "missed": "这次修改仍未解决", "no_revision": "还没有实际改稿"}


def comparison(repo, state, owner, item):
    """Resolve exact excerpts within the named lesson. All coordinates count Unicode code points."""
    task = load_json(owner / "source.json")["identity"]["writing_session"]
    def resolve(anchor, role):
        root = package_root(state, repo, anchor["package_id"])
        receipt = load_json(root / "receipt.json")
        binding = load_json(root / "source.json")["identity"].get("writing_session", {})
        valid(all(binding.get(k) == task[k] for k in ("host", "session_id", "attempt_id")), "反馈、前稿和改稿须属于同一次作答")
        rows = load_json(root / "conversation.json")["messages"]
        row = next((m for m in rows if m["message_id"] == anchor["message_id"] and m["role"] == role), None)
        valid(row is not None, "反馈对照的原消息或作者不符")
        quote = anchor.get("quote")
        valid(isinstance(quote, str) and quote.strip(), "缺少要对照的原句")
        attachment = anchor.get("attachment_sha256")
        if attachment:
            assets = load_json(root / "manifest.json")["attachments"]
            valid(role == "user" and anchor.get("checked_original") is True and
                  any(a["attachment_id"] in row["attachment_refs"] and a["sha256"] == attachment for a in assets), "手写引句需对应核过的原稿附件")
            location = {"attachment_sha256": attachment, "checked_original": True}
        else:
            value = row["content"]
            start = anchor.get("offset")
            if start is None:
                valid(value.count(quote) == 1, "引句须在原消息中唯一，重复时指定实际字符位置")
                start = value.index(quote)
            valid(type(start) is int and start >= 0 and value[start:start + len(quote)] == quote, "引句与原消息位置不符")
            location = {"offset": start, "length": len(quote)}
        version = binding.get("message_versions", {}).get(row["message_id"], {})
        at = version.get("source_updated_at") or row["created_at"]
        return dict(package_id=receipt["package_id"], package_sha256=receipt["package_sha256"], message_id=row["message_id"],
                    original_sha256=digest_message(row), role=role, at=at, quote=quote, **location)

    from .writing_personal import skill_key
    point = skill_key(item["point_key"])
    result = item.get("result")
    valid(result in OUTCOMES and isinstance(item.get("reason"), str) and item["reason"].strip(), "反馈需要实际修改结果与判断依据")
    before = resolve(item["original"], "user")
    advice = resolve(item["feedback"], "assistant")
    after = resolve(item["revision"], "user") if item.get("revision") else None
    valid((after is None) == (result == "no_revision"), "有真实改稿才可评价是否改到点上")
    time_of = lambda anchor: datetime.fromisoformat(anchor["at"].replace("Z", "+00:00"))
    valid(time_of(before) <= time_of(advice), "讲评早于所指原句")
    if after:
        valid(time_of(advice) <= time_of(after) and (before["message_id"],before["original_sha256"]) !=
              (after["message_id"],after["original_sha256"]), "实际改稿须有后续的本人原件")
    owner_id=load_json(owner/"receipt.json")["package_id"]
    valid(owner_id in {advice["package_id"], (after or advice)["package_id"]}, "反馈对照应从本包的讲评或实际改稿提交")
    def identity_of(anchor):
        return None if anchor is None else {k:anchor[k] for k in ("message_id","original_sha256","offset","quote","attachment_sha256") if k in anchor}
    identity = object_sha256([task["host"], task["session_id"], task["attempt_id"], point,
                              identity_of(before), identity_of(advice), identity_of(after)])
    return dict(version="writing-feedback/1", comparison_id=identity, point_key=point, original=before,
                feedback=advice, revision=after, result=result, reason=item["reason"].strip(),
                study_date=parse_iso_date((after or advice)["at"]))


def publish_reviewed(state, writer, reviewed):
    """Index the private comparisons from the verified local author's immutable decisions."""
    folder = state / "writing-sessions/feedback-days"
    for pid, row in reviewed.items():
        if writer["package_dispositions"].get(pid) != "completed":
            continue
        for item in row["review"].get("feedback_reviews", []):
            key = item["comparison_id"]
            path = folder / item["study_date"] / (key + ".json")
            with exclusive_lock(folder / ".lock"):
                if path.is_file():
                    original=load_json(path)["comparison"]
                    valid(original["comparison_id"]==key and original["result"]==item["result"] and
                          original["reason"]==item["reason"], "同一组原件的反馈判断发生冲突，需沿原回执更正")
                    continue
                atomic_write_json(path, {"comparison": item, "sha256": object_sha256(item),
                    "writer_receipt": writer["receipt_id"], "actions_sha256": writer["actions_sha256"],
                    "event_ids": [r["assigned_id"] for r in writer["action_results"] if r.get("writing_binding",{}).get("point_key")==item["point_key"]]})
