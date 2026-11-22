"""可核验证据台账。

异常段、工单与归卷复算都通过行号引用同一份原读数；台账对每条保留原行
给出内容摘要（row_sha256），降级后仍可核对取出的原行未被改写。
"""

import base64

from app.manifest import canonical, json_sha, sha

# 工单中仍可能被审计抽查的勾稽结果（含误报、缺证等差项）
TICKET_REVIEW_CODES = {"MATCHED", "DUPLICATE_ALARM", "TRANSIENT_ONLY", "MISSING_EVIDENCE", "STABLE_AT_ALARM"}


def row_hash(row):
    """对原读数行的可核验内容做摘要：物理行号、原始字段与处置结论。"""
    payload = {"id": row["id"], "line": row["line"], "end_line": row["end_line"],
               "raw": row["raw"], "disposition": row["disposition"], "code": row["code"]}
    return json_sha(payload)


def build_ledger(evidence):
    """按行号登记全部原读数，供异常段、工单与复算共用同一引用。"""
    entries = []
    for row in evidence["rows"]:
        entries.append({"row_id": row["id"], "line": row["line"], "end_line": row["end_line"],
                        "probe": row.get("probe"), "disposition": row["disposition"],
                        "code": row["code"], "row_sha256": row_hash(row)})
    return {"ledger_sha256": json_sha(entries), "rows": entries}


def evidence_references(evidence):
    """汇总异常段与工单引用到的原行号，保证三处指向同一份证据。"""
    refs = {}

    def point(row_id, source):
        refs.setdefault(row_id, []).append(source)

    for episode in evidence["episodes"]:
        for slot in episode["evidence"]:
            for level in ("upper", "lower"):
                if slot[level]:
                    point(slot[level], f"episode:{episode['id']}")
    for ticket in evidence["audit"]["tickets"]:
        for row_id in ticket["evidence"]:
            point(row_id, f"ticket:{ticket['id']}")
    return refs


def retained_scope(spec, evidence):
    """返回 (保留行号集合, 逐档依据)。依据按优先级给出，首条即锁定原因。"""
    reasons = []
    loan = {item.id for item in spec.cabinet_hours if item.loan_cabinet}
    if loan:
        reasons.append({"code": "LOAN_CABINET", "keep": "LOCK",
                        "detail": f"借展柜 {sorted(loan)} 原档须整体留存，不参与降级"})

    sustained = [item["id"] for item in evidence["episodes"] if item["kind"] == "SUSTAINED"]
    if sustained:
        reasons.append({"code": "SUSTAINED_EPISODE", "keep": "LOCK",
                        "detail": f"持续异常段 {sustained} 仍在审计抽查范围，原读数须锁定"})

    unreported = evidence["audit"].get("unreported", [])
    if unreported:
        reasons.append({"code": "UNREPORTED_SUSTAINED", "keep": "LOCK",
                        "detail": f"持续段 {unreported} 无已核销工单，关联证据须锁定"})

    conflicts = evidence.get("deployment_conflicts") or []
    if conflicts:
        reasons.append({"code": "DEPLOYMENT_CONFLICT", "keep": "LOCK",
                        "detail": f"存在 {len(conflicts)} 段部署重叠，相关原读数须锁定待查"})
    if "READING_CONFLICT" in evidence.get("isolation", {}):
        reasons.append({"code": "READING_CONFLICT", "keep": "LOCK",
                        "detail": "同探头同刻矛盾读数已隔离，原档须锁定备查"})

    locked = any(item["keep"] == "LOCK" for item in reasons)
    referenced = evidence_references(evidence)
    if locked:
        # 锁定班次：全部原读数留存，依据已在 reasons 中列明
        return {row["id"] for row in evidence["rows"]}, reasons, referenced

    transient = [item["id"] for item in evidence["episodes"] if item["kind"] == "TRANSIENT"]
    if transient:
        reasons.append({"code": "TRANSIENT_EPISODE", "keep": "KEEP",
                        "detail": f"瞬时异常段 {transient} 抽查原读数，保留关联原档"})
    reviewed = [t["id"] for t in evidence["audit"]["tickets"] if t["code"] in TICKET_REVIEW_CODES]
    if reviewed:
        reasons.append({"code": "TICKET_REVIEW", "keep": "KEEP",
                        "detail": f"工单 {reviewed} 勾稽原读数仍会被抽查，保留关联原档"})

    if referenced:
        reasons.append({"code": "EVIDENCE_REFERENCED", "keep": "KEEP",
                        "detail": f"异常段或工单引用 {len(referenced)} 条原读数，按台账保留"})
    if not reasons:
        reasons.append({"code": "NO_OPEN_EVIDENCE", "keep": "DOWNGRADE",
                        "detail": "无异常段、无工单关联、无隔离冲突，原档可降为摘要"})
    return set(referenced), reasons, referenced


def build_summary_source(package, evidence, ledger, retained):
    """构造降级后的摘要型输入：契约与原档摘要保留，仅嵌入被引用的原行。"""
    rows_by_id = {row["id"]: row for row in evidence["rows"]}
    hash_by_id = {item["row_id"]: item["row_sha256"] for item in ledger["rows"]}
    excerpts = []
    for row_id in sorted(retained):
        row = rows_by_id[row_id]
        excerpts.append({"row_id": row["id"], "line": row["line"], "end_line": row["end_line"],
                         "raw": row["raw"], "disposition": row["disposition"], "code": row["code"],
                         "row_sha256": hash_by_id[row_id]})
    return {
        "mode": "summary",
        "contract": package.spec.model_dump(mode="json"),
        "original_file": package.spec.csv,
        "original_file_sha256": sha(package.blob),
        "original_blob_bytes": len(package.blob),
        "original_bytes": _full_input_bytes(package),
        "ledger_sha256": ledger["ledger_sha256"],
        "retained_rows": excerpts,
    }


def _full_input_bytes(package):
    full = {"contract": package.spec.model_dump(mode="json"),
            "csv_base64": base64.b64encode(package.blob).decode("ascii")}
    return len(canonical(full).encode("utf-8"))
