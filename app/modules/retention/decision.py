"""闭馆季原档降级结论。

逐班给出 保留(KEEP) / 降级(DOWNGRADE) / 锁定(LOCK) 三种结论、依据和
空间释放量。结论只依赖归卷内固定的契约、原档与结果证据，可重复计算。
已降级的摘要档在后续结转中保持降级，不重复计算释放量。
"""

from app.errors import FieldError
from app.manifest import canonical, json_sha
from app.modules.archive.pipeline import evaluate
from app.modules.archive.proof import verify
from app.modules.intake.catalog import restore, restore_summary
from app.modules.retention.ledger import (
    _full_input_bytes,
    build_ledger,
    build_summary_source,
    retained_scope,
)

KEEP = "KEEP"
DOWNGRADE = "DOWNGRADE"
LOCK = "LOCK"
VERDICTS = (LOCK, KEEP, DOWNGRADE)
LABELS = {LOCK: "锁定", KEEP: "保留", DOWNGRADE: "降级"}

# 存储按 UTF-8 规范 JSON 计量，摘要与全档口径一致
def _stored_bytes(value):
    return len(canonical(value).encode("utf-8"))


def decide(archive):
    """对单班次归卷形成降级结论，不修改任何存储内容。

    返回 (conclusion, package_or_none, ledger)。已降级档 package 为 None。
    """
    manifest, evidence, source = archive
    if source.get("mode") == "summary":
        return _decide_summary(archive), None, build_ledger(evidence)

    package = verify(archive)
    spec = package.spec
    ledger = build_ledger(evidence)
    retained, reasons, referenced = retained_scope(spec, evidence)
    top = reasons[0]
    verdict = LOCK if top["keep"] == LOCK else (DOWNGRADE if top["keep"] == "DOWNGRADE" else KEEP)

    summary_source = build_summary_source(package, evidence, ledger, retained)
    summary_bytes = _stored_bytes(summary_source)
    # 释放量按库内实际口径比较：全量输入(契约+Base64 原档) 对 摘要输入
    original_bytes = _full_input_bytes(package)
    released = original_bytes - summary_bytes if verdict == DOWNGRADE else 0

    conclusion = {
        "run_id": manifest["id"],
        "shift": spec.shift,
        "label": spec.label,
        "mode": "full",
        "verdict": verdict,
        "verdict_label": LABELS[verdict],
        "reasons": reasons,
        "original_file": spec.csv,
        "original_file_sha256": manifest["hashes"]["file_sha256"],
        "original_blob_bytes": len(package.blob),
        "original_bytes": original_bytes,
        "summary_bytes": summary_bytes,
        "released_bytes": max(released, 0),
        "retained_row_count": len(retained),
        "total_row_count": len(evidence["rows"]),
        "ledger_sha256": ledger["ledger_sha256"],
        "result_sha256": manifest["result_sha256"],
        "referenced": {row_id: sorted(set(sources)) for row_id, sources in sorted(referenced.items())},
    }
    conclusion["conclusion_sha256"] = _digest(conclusion)
    return conclusion, package, ledger


def _decide_summary(archive):
    manifest, evidence, source = archive
    verify(archive)
    spec, retained_rows, original_digest = restore_summary(source)
    referenced = {row["row_id"]: ["summary:retained"] for row in retained_rows}
    reasons = [{"code": "ALREADY_DOWNGRADED", "keep": DOWNGRADE,
                "detail": "原档已在先前结转降为摘要，本次结转保持降级，不重复释放空间"}]
    conclusion = {
        "run_id": manifest["id"],
        "shift": spec.shift,
        "label": spec.label,
        "mode": "summary",
        "verdict": DOWNGRADE,
        "verdict_label": LABELS[DOWNGRADE],
        "reasons": reasons,
        "original_file": source["original_file"],
        "original_file_sha256": original_digest,
        "original_blob_bytes": source["original_blob_bytes"],
        "original_bytes": source["original_bytes"],
        "summary_bytes": _stored_bytes(source),
        "released_bytes": 0,
        "retained_row_count": len(retained_rows),
        "total_row_count": len(evidence["rows"]),
        "ledger_sha256": source["ledger_sha256"],
        "result_sha256": manifest["result_sha256"],
        "referenced": referenced,
    }
    conclusion["conclusion_sha256"] = _digest(conclusion)
    return conclusion


def _digest(conclusion):
    return json_sha({key: value for key, value in conclusion.items() if key != "conclusion_sha256"})


def apply_downgrade(archive, conclusion):
    """返回降级后应写入的 (evidence, source)；非全档降级结论返回 None。"""
    if conclusion["verdict"] != DOWNGRADE or conclusion["mode"] != "full":
        return None
    manifest, evidence, source = archive
    package = restore(source)
    # 降级前先复算，确认固定原档与结果摘要一致，避免在漂移证据上降级
    if json_sha(evaluate(package)) != manifest["result_sha256"]:
        raise FieldError("RULE_DRIFT", "复算结果摘要不一致，已保留旧版", 409)
    ledger = build_ledger(evidence)
    retained = set(conclusion["referenced"])
    summary_source = build_summary_source(package, evidence, ledger, retained)
    retained_evidence = {
        "mode": "summary", "ledger_sha256": ledger["ledger_sha256"],
        "retained_row_ids": sorted(retained),
        "original_file_sha256": conclusion["original_file_sha256"],
        "conclusion_sha256": conclusion["conclusion_sha256"],
    }
    return {**evidence, "retention": retained_evidence}, summary_source
