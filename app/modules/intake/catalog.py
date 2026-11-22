import base64
import binascii

from pydantic import ValidationError

from app.config import MAX_FILE_BYTES, ROOT
from app.errors import FieldError
from app.modules.intake.contract import Package, Shift

CATALOG = {"shift": "值班混合样例", "clean": "全稳校验样例", "transient": "瞬时异常留存样例",
           "truncated": "截断图档样例"}


def load_sample(sample, root=ROOT / "sample_data"):
    if sample not in CATALOG:
        raise FieldError("UNKNOWN_SAMPLE", "样例未登记", 404)
    try:
        spec = Shift.model_validate_json((root / f"{sample}.json").read_bytes())
        if spec.sample != sample:
            raise ValueError("样例标识不一致")
        with (root / spec.csv).open("rb") as stream:
            blob = stream.read(MAX_FILE_BYTES + 1)
    except (OSError, ValidationError, ValueError) as exc:
        raise FieldError("BAD_INPUT", "样例契约或点位图档无法读取") from exc
    if len(blob) > MAX_FILE_BYTES:
        raise FieldError("TOO_LARGE", "点位图档超过 1 MiB", 413)
    return Package(spec, blob)


def restore(data):
    try:
        return Package(Shift.model_validate(data["contract"]), base64.b64decode(data["csv_base64"], validate=True))
    except (KeyError, TypeError, ValueError, binascii.Error) as exc:
        raise FieldError("ARCHIVE_DAMAGED", "归卷输入无法读取", 409) from exc


def restore_summary(data):
    """还原降级后的摘要型输入，不含全量原档，仅保留被引用原行。"""
    try:
        spec = Shift.model_validate(data["contract"])
        rows = data["retained_rows"]
        digest = data["original_file_sha256"]
    except (KeyError, TypeError, ValueError) as exc:
        raise FieldError("ARCHIVE_DAMAGED", "降级摘要无法读取", 409) from exc
    return spec, rows, digest
