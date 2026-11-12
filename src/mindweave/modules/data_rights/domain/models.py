"""Pure domain models, GB 45438-2025 provenance builder, Markdown journal renderer, and ZIP packager.

Hexagonal domain layer: 100% pure Python stdlib + `mindweave.shared.enums` ONLY.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import re
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

from mindweave.shared.enums import UNASSIGNED_PERSON_SLUG, Tier

EXPORT_SCHEMA_VERSION = "1.0"
AIGC_STANDARD_GB45438 = "GB 45438-2025"
JOURNAL_AIGC_DISCLOSURE_FOOTER = "手账内容含 AI 基于您的单方记录整理生成，仅作个人私密备忘"

TIER_HEADINGS: dict[str, str] = {
    Tier.GOLD.value: "金箭 · 记爱",
    Tier.SILVER.value: "银箭 · 记恨",
    Tier.CRYSTAL.value: "水晶箭 · 记繁琐",
}

FACT_TYPE_LABELS: dict[str, str] = {
    "preference": "喜好",
    "taboo": "忌口与雷区",
    "date": "重要日子",
    "wish": "心愿单",
    "promise": "我的承诺",
    "event": "共同点滴",
    "note": "随手备忘",
}

RECORD_KIND_LABELS: dict[str, str] = {
    "photo": "拍立得",
    "note": "纸质便签",
    "voice": "原声签",
}

_RAW_CITATION_RE = re.compile(r"\s*(?:\[rec_[^\]]+\]|【rec_[^】]+】)")


@dataclass(frozen=True, slots=True)
class ExportMediaItem:
    object_key: str
    archive_path: str
    content_type: str
    data: bytes


@dataclass(frozen=True, slots=True)
class ExportArchiveBundle:
    zip_bytes: bytes
    zip_sha256_hex: str
    content_sha256_hex: str
    data_json: dict[str, Any]
    journal_markdown: str


def sanitize_journal_text(text: str) -> str:
    """Strip internal citation tags (`[rec_...]`) so journal prose stays human-friendly (Invariant I6)."""
    cleaned = _RAW_CITATION_RE.sub("", text)
    return cleaned.strip()


def allocate_media_archive_path(object_key: str, used_paths: set[str]) -> str:
    """Deterministically assign a safe `media/<filename>` path inside the export ZIP."""
    normalized = object_key.replace("\\", "/").strip("/")
    raw_name = PurePosixPath(normalized).name if normalized else "blob.bin"
    safe_name = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff]", "_", raw_name).strip("._")
    if not safe_name:
        safe_name = "blob.bin"

    candidate = f"media/{safe_name}"
    if candidate not in used_paths:
        used_paths.add(candidate)
        return candidate

    stem = PurePosixPath(safe_name).stem or "blob"
    suffix = PurePosixPath(safe_name).suffix
    idx = 2
    while True:
        alt = f"media/{stem}_{idx}{suffix}"
        if alt not in used_paths:
            used_paths.add(alt)
            return alt
        idx += 1


def build_aigc_provenance_metadata(
    *,
    export_id: str,
    exported_at_iso: str,
    canonical_payload_bytes: bytes,
) -> dict[str, Any]:
    """Construct GB 45438-2025 implicit AIGC provenance label for exported content."""
    content_sha256 = hashlib.sha256(canonical_payload_bytes).hexdigest()
    return {
        "standard": AIGC_STANDARD_GB45438,
        "contentGeneratedByAI": True,
        "provider": "MindWeave",
        "serviceName": "心络 MindWeave",
        "labelType": "implicit_provenance",
        "exportId": export_id,
        "exportedAt": exported_at_iso,
        "contentSha256": content_sha256,
    }


def render_journal_markdown(
    *,
    exported_at_iso: str,
    persons: Sequence[Mapping[str, Any]],
    materials: Sequence[Mapping[str, Any]],
    facts: Sequence[Mapping[str, Any]],
    reminders: Sequence[Mapping[str, Any]],
    chat_messages: Sequence[Mapping[str, Any]],
) -> str:
    """Render a warm, human-readable Markdown journal organized by Arrow Tier and Person."""
    visible_persons: list[Mapping[str, Any]] = []
    excluded_person_ids: set[str] = set()
    for person in persons:
        if str(person.get("slug") or "") == UNASSIGNED_PERSON_SLUG:
            if person.get("id") is not None:
                excluded_person_ids.add(str(person["id"]))
            continue
        visible_persons.append(person)

    known_person_ids = {str(p["id"]) for p in visible_persons if p.get("id") is not None}

    lines: list[str] = [
        "# 心络 · 私密手账导出卷",
        "",
        f"- 导出时间：{exported_at_iso}",
        f"- 收录人物：{len(visible_persons)} 位",
        "",
    ]

    persons_by_tier: dict[str, list[Mapping[str, Any]]] = {
        Tier.GOLD.value: [],
        Tier.SILVER.value: [],
        Tier.CRYSTAL.value: [],
    }
    for person in visible_persons:
        tier_key = str(person.get("tier", Tier.CRYSTAL.value))
        persons_by_tier.setdefault(tier_key, []).append(person)

    facts_by_person: dict[str, list[Mapping[str, Any]]] = {}
    unassigned_facts: list[Mapping[str, Any]] = []
    for fact in facts:
        if fact.get("status") == "rejected":
            continue
        raw_pid = fact.get("personId")
        pid_str = str(raw_pid).strip() if raw_pid is not None else ""
        if not pid_str or pid_str in excluded_person_ids:
            unassigned_facts.append(fact)
        elif pid_str in known_person_ids:
            facts_by_person.setdefault(pid_str, []).append(fact)

    materials_by_person: dict[str, list[Mapping[str, Any]]] = {}
    unassigned_materials: list[Mapping[str, Any]] = []
    for mat in materials:
        if mat.get("reviewState") == "held":
            continue
        raw_pid = mat.get("personId")
        pid_str = str(raw_pid).strip() if raw_pid is not None else ""
        if not pid_str or pid_str in excluded_person_ids:
            unassigned_materials.append(mat)
        elif pid_str in known_person_ids:
            materials_by_person.setdefault(pid_str, []).append(mat)

    reminders_by_person: dict[str, list[Mapping[str, Any]]] = {}
    for rem in reminders:
        if rem.get("status") == "cancelled":
            continue
        pid = rem.get("personId")
        if pid and str(pid) in known_person_ids and str(pid) not in excluded_person_ids:
            reminders_by_person.setdefault(str(pid), []).append(rem)

    messages_by_person: dict[str, list[Mapping[str, Any]]] = {}
    for msg in chat_messages:
        pid = msg.get("personId")
        if pid and str(pid) in known_person_ids and str(pid) not in excluded_person_ids:
            messages_by_person.setdefault(str(pid), []).append(msg)

    for tier_enum in (Tier.GOLD, Tier.SILVER, Tier.CRYSTAL):
        tier_val = tier_enum.value
        heading = TIER_HEADINGS[tier_val]
        lines.append(f"## {heading}")
        lines.append("")
        tier_people = sorted(
            persons_by_tier.get(tier_val, []),
            key=lambda p: str(p.get("rank", "")),
        )
        if not tier_people:
            lines.append("_暂无收录人物_")
            lines.append("")
            continue

        for person in tier_people:
            pid = str(person["id"])
            display_name = sanitize_journal_text(
                str(person.get("displayName") or person.get("monogram") or "未命名")
            )
            role_tag = sanitize_journal_text(str(person.get("roleTag") or ""))
            title_suffix = f"（{role_tag}）" if role_tag else ""
            lines.append(f"### {display_name}{title_suffix}")
            lines.append("")

            short_memo = sanitize_journal_text(str(person.get("shortMemo") or ""))
            if short_memo:
                lines.append(f"> {short_memo}")
                lines.append("")

            p_facts = facts_by_person.get(pid, [])
            if p_facts:
                lines.append("#### 点滴备忘")
                for f in p_facts:
                    ftype_label = FACT_TYPE_LABELS.get(str(f.get("type", "")), "备忘")
                    content = sanitize_journal_text(str(f.get("content", "")))
                    status_note = "（待确认）" if f.get("status") == "pending" else ""
                    date_note = f" · {f['triggerDate']}" if f.get("triggerDate") else ""
                    lines.append(f"- **{ftype_label}**{status_note}：{content}{date_note}")
                lines.append("")

            p_mats = materials_by_person.get(pid, [])
            if p_mats:
                lines.append("#### 原始手札")
                for m in p_mats:
                    kind_label = RECORD_KIND_LABELS.get(str(m.get("kind", "")), "便签")
                    m_title = sanitize_journal_text(str(m.get("title", "")))
                    m_body = sanitize_journal_text(str(m.get("body", "")))
                    when_str = f"（{m['happenedOn']}）" if m.get("happenedOn") else ""
                    lines.append(f"- [{kind_label}] **{m_title}**{when_str}：{m_body}")
                lines.append("")

            p_rems = reminders_by_person.get(pid, [])
            if p_rems:
                lines.append("#### 守约提醒")
                for r in p_rems:
                    r_title = sanitize_journal_text(str(r.get("title", "")))
                    r_rule = "每年" if r.get("rule") == "yearly" else "单次"
                    lines.append(f"- {r_title}（{r_rule} · {r.get('status', 'active')}）")
                lines.append("")

            p_msgs = messages_by_person.get(pid, [])
            if p_msgs:
                lines.append("#### 树洞对话摘录")
                for msg in p_msgs:
                    speaker = "我" if msg.get("role") == "user" else "心络"
                    m_content = sanitize_journal_text(str(msg.get("content", "")))
                    lines.append(f"- **{speaker}**：{m_content}")
                lines.append("")

    if unassigned_materials or unassigned_facts:
        lines.append("## 未归属随手记")
        lines.append("")
        for m in unassigned_materials:
            kind_label = RECORD_KIND_LABELS.get(str(m.get("kind", "")), "便签")
            m_title = sanitize_journal_text(str(m.get("title", "")))
            m_body = sanitize_journal_text(str(m.get("body", "")))
            when_str = f"（{m['happenedOn']}）" if m.get("happenedOn") else ""
            lines.append(f"- [{kind_label}] **{m_title}**{when_str}：{m_body}")
        for f in unassigned_facts:
            ftype_label = FACT_TYPE_LABELS.get(str(f.get("type", "")), "备忘")
            content = sanitize_journal_text(str(f.get("content", "")))
            status_note = "（待确认）" if f.get("status") == "pending" else ""
            lines.append(f"- **{ftype_label}**{status_note}：{content}")
        lines.append("")

    lines.extend(
        [
            "---",
            "",
            f"*{JOURNAL_AIGC_DISCLOSURE_FOOTER}*",
            "",
        ]
    )
    return "\n".join(lines)


def _sanitize_export_sections(sections: Mapping[str, Any]) -> dict[str, Any]:
    cleaned = dict(sections)
    raw_persons = cleaned.get("persons")
    if not isinstance(raw_persons, Sequence) or isinstance(raw_persons, str | bytes):
        return cleaned

    unassigned_ids: set[str] = set()
    visible_persons: list[Any] = []
    for p in raw_persons:
        if isinstance(p, Mapping) and str(p.get("slug") or "") == UNASSIGNED_PERSON_SLUG:
            if p.get("id") is not None:
                unassigned_ids.add(str(p["id"]))
            continue
        visible_persons.append(p)

    if not unassigned_ids:
        return cleaned

    cleaned["persons"] = visible_persons
    if isinstance(cleaned.get("personAliases"), list):
        cleaned["personAliases"] = [
            a
            for a in cleaned["personAliases"]
            if not (isinstance(a, Mapping) and str(a.get("personId") or "") in unassigned_ids)
        ]
    for key in ("materials", "facts", "inboxItems"):
        if isinstance(cleaned.get(key), list):
            remapped: list[Any] = []
            for entry in cleaned[key]:
                if (
                    isinstance(entry, Mapping)
                    and str(entry.get("personId") or "") in unassigned_ids
                ):
                    item_copy = dict(entry)
                    item_copy["personId"] = None
                    remapped.append(item_copy)
                else:
                    remapped.append(entry)
            cleaned[key] = remapped
    return cleaned


def package_export_zip(
    *,
    export_id: str,
    user_id: str,
    exported_at_iso: str,
    sections: Mapping[str, Any],
    media_items: Sequence[ExportMediaItem],
) -> ExportArchiveBundle:
    """Build portable in-memory ZIP archive with `data.json`, `journal.md`, and `media/*`."""
    sanitized_sections = _sanitize_export_sections(sections)
    media_manifest = [
        {
            "objectKey": item.object_key,
            "archivePath": item.archive_path,
            "contentType": item.content_type,
            "byteSize": len(item.data),
            "sha256": hashlib.sha256(item.data).hexdigest(),
        }
        for item in media_items
    ]

    core_payload: dict[str, Any] = {
        "schemaVersion": EXPORT_SCHEMA_VERSION,
        "exportId": export_id,
        "userId": user_id,
        "exportedAt": exported_at_iso,
        "mediaFiles": media_manifest,
        **sanitized_sections,
    }

    canonical_bytes = json.dumps(
        core_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    aigc_meta = build_aigc_provenance_metadata(
        export_id=export_id,
        exported_at_iso=exported_at_iso,
        canonical_payload_bytes=canonical_bytes,
    )

    data_json_doc: dict[str, Any] = {
        "schemaVersion": EXPORT_SCHEMA_VERSION,
        "_aigcMetadata": aigc_meta,
        "exportId": export_id,
        "userId": user_id,
        "exportedAt": exported_at_iso,
        "mediaFiles": media_manifest,
        **sanitized_sections,
    }
    data_json_bytes = json.dumps(
        data_json_doc,
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")

    journal_md = render_journal_markdown(
        exported_at_iso=exported_at_iso,
        persons=sanitized_sections.get("persons", []),
        materials=sanitized_sections.get("materials", []),
        facts=sanitized_sections.get("facts", []),
        reminders=sanitized_sections.get("reminders", []),
        chat_messages=sanitized_sections.get("chatMessages", []),
    )
    journal_bytes = journal_md.encode("utf-8")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.comment = json.dumps(
            {"_aigcMetadata": aigc_meta},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        zf.writestr("data.json", data_json_bytes)
        zf.writestr("journal.md", journal_bytes)
        for item in media_items:
            zf.writestr(item.archive_path, item.data)

    zip_bytes = buf.getvalue()
    zip_sha256_hex = hashlib.sha256(zip_bytes).hexdigest()
    return ExportArchiveBundle(
        zip_bytes=zip_bytes,
        zip_sha256_hex=zip_sha256_hex,
        content_sha256_hex=str(aigc_meta["contentSha256"]),
        data_json=data_json_doc,
        journal_markdown=journal_md,
    )


def verify_one_time_download_token(
    *,
    provided_token: str,
    stored_token_hash: bytes | None,
    status: str,
    expires_at: datetime | None,
    downloaded_at: datetime | None,
    now: datetime,
) -> str | None:
    """Verify download token in constant time; returns `None` if valid, else rejection code."""
    cleaned = provided_token.strip()
    if not cleaned:
        return "missing_token"
    if status != "completed":
        return "export_not_ready"
    if downloaded_at is not None:
        return "already_downloaded"
    now_utc = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    if expires_at is None:
        return "expired"
    exp_utc = (
        expires_at.replace(tzinfo=UTC) if expires_at.tzinfo is None else expires_at.astimezone(UTC)
    )
    if exp_utc <= now_utc:
        return "expired"
    if stored_token_hash is None:
        return "invalid_token"
    candidate_hash = hashlib.sha256(cleaned.encode("utf-8")).digest()
    if not hmac.compare_digest(candidate_hash, stored_token_hash):
        return "invalid_token"
    return None
