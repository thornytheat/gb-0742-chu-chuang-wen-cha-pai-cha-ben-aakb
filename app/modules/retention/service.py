"""闭馆季结转服务：形成降级结论并在同一事务内落地。"""

from datetime import datetime, timezone

from app.errors import FieldError
from app.manifest import json_sha
from app.modules.retention.decision import apply_downgrade, decide


class RetentionService:
    def __init__(self, store):
        self.store = store

    def _batch_id(self, season):
        # 批次身份取决于结转季与归卷的稳定身份（归卷号、结果摘要、原档摘要）。
        # 降级只移除全量原档、不改这些摘要，故同库同季重算得到同一批次号（幂等）；
        # 归卷内容被替换则摘要变化、批次号变化，不会误复用旧结论。
        fingerprints = [(archive[0]["id"], archive[0]["result_sha256"],
                         archive[0]["hashes"]["file_sha256"]) for archive in self.store.all_archives()]
        return json_sha(["retention", season, fingerprints])

    def carry_over(self, season):
        """对当前全部班次形成结论；同一次结转重跑不新增结论。"""
        batch_id = self._batch_id(season)
        try:
            prior = self.store.fetch_batch(batch_id)
            return {"batch": prior, "reused": True}
        except FieldError as exc:
            if exc.code != "UNKNOWN_BATCH":
                raise

        archives = self.store.all_archives()
        conclusions, downgrades = [], []
        # 实际库存口径：未降级班次按全量输入计，已降级班次按摘要输入计
        stored_before = stored_after = total_released = 0
        for archive in archives:
            conclusion, package, ledger = decide(archive)
            conclusions.append(conclusion)
            if conclusion["mode"] == "summary":
                stored_before += conclusion["original_bytes"]
                stored_after += conclusion["summary_bytes"]
            elif conclusion["verdict"] == "DOWNGRADE":
                stored_before += conclusion["original_bytes"]
                stored_after += conclusion["summary_bytes"]
                total_released += conclusion["released_bytes"]
            else:
                # 保留/锁定：全档维持在库，库存前后都按全量输入计
                stored_before += conclusion["original_bytes"]
                stored_after += conclusion["original_bytes"]
            applied = apply_downgrade(archive, conclusion)
            if applied is not None:
                downgrades.append((conclusion["run_id"], applied[0], applied[1]))

        batch = {
            "id": batch_id,
            "season": season,
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "verdict_counts": {verdict: sum(1 for item in conclusions if item["verdict"] == verdict)
                               for verdict in ("LOCK", "KEEP", "DOWNGRADE")},
            "totals": {"shifts": len(conclusions), "stored_bytes_before": stored_before,
                       "stored_bytes_after": stored_after, "released_bytes": total_released},
            "conclusions": conclusions,
        }
        batch["batch_sha256"] = json_sha({key: value for key, value in batch.items()
                                          if key != "batch_sha256"})
        # 事务内提交；失败抛错时旧版归卷与旧批次保持原样
        created = self.store.commit_retention(batch, downgrades)
        if not created:
            return {"batch": self.store.fetch_batch(batch_id), "reused": True}
        return {"batch": batch, "reused": False}

    def batch(self, batch_id):
        return self.store.fetch_batch(batch_id)

    def batches(self):
        return self.store.list_batches()
