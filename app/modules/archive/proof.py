from app.errors import FieldError
from app.manifest import canonical, identify, json_sha
from app.modules.intake.catalog import restore, restore_summary
from app.modules.retention.ledger import row_hash


def verify(archive):
    manifest, evidence, source = archive
    if source.get("mode") == "summary":
        verify_summary(archive)
        # 摘要档无法重放全量管线，返回 None 表示仅完成摘要核验
        return None
    package = restore(source)
    identity, hashes = identify(package, manifest["policy"])
    if (identity != manifest["id"] or hashes != manifest["hashes"] or json_sha(evidence) != manifest["result_sha256"]):
        raise FieldError("ARCHIVE_DAMAGED", "归卷摘要不符", 409)
    return package


def verify_summary(archive):
    """降级档核验：原档摘要、台账摘要、保留原行内容与结论须互相吻合。"""
    manifest, evidence, source = archive
    try:
        spec, rows, original_digest = restore_summary(source)
    except FieldError:
        raise
    if original_digest != manifest["hashes"]["file_sha256"]:
        raise FieldError("ARCHIVE_DAMAGED", "降级档原档摘要不符", 409)
    retention = evidence.get("retention")
    if not retention or retention.get("mode") != "summary":
        raise FieldError("ARCHIVE_DAMAGED", "降级标记缺失", 409)
    if retention["original_file_sha256"] != original_digest:
        raise FieldError("ARCHIVE_DAMAGED", "降级结论原档摘要不符", 409)
    if source["ledger_sha256"] != retention["ledger_sha256"]:
        raise FieldError("ARCHIVE_DAMAGED", "证据台账摘要不符", 409)
    retained_ids = set(retention["retained_row_ids"])
    if {row["row_id"] for row in rows} != retained_ids:
        raise FieldError("ARCHIVE_DAMAGED", "保留原行与台账不一致", 409)
    ledger_rows = {item["row_id"]: item for item in _ledger_rows(evidence)}
    for row in rows:
        entry = ledger_rows.get(row["row_id"])
        if entry is None or entry["row_sha256"] != row["row_sha256"]:
            raise FieldError("ARCHIVE_DAMAGED", f"保留原行 {row['row_id']} 摘要不符", 409)
        # 重算行摘要，确认取出的原读数未被改写
        rebuilt = {"id": row["row_id"], "line": row["line"], "end_line": row["end_line"],
                   "raw": row["raw"], "disposition": row["disposition"], "code": row["code"]}
        if row_hash(rebuilt) != row["row_sha256"]:
            raise FieldError("ARCHIVE_DAMAGED", f"原行 {row['row_id']} 已被改写", 409)
    return True


def _ledger_rows(evidence):
    from app.modules.retention.ledger import build_ledger
    return build_ledger(evidence)["rows"]


def export(archive):
    verify(archive)
    manifest, evidence, source = archive
    return canonical({"format": "museum-cabinet-evidence-v1", "manifest": manifest,
                      "input": source, "evidence": evidence}).encode("utf-8")
