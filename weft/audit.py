"""编辑审计链（企业功能 `audit`）。

企业客户问的第一个合规问题不是"你们加密了吗"，而是
**"谁在什么时候改了哪块地、账对得上吗"**。没有可验证的编辑历史，
私有化部署过不了审计。

设计：
- 追加式（append-only）JSONL，每行为一条记录；
- 每条 `hash = sha256(prev_hash || canonical(record))`，另存链头锚点文件；
- 改动/删除任意一条，`verify()` 立刻定位到断点；
- 导出即为原始 JSONL，审计方可离线重算，不需要信任本工具。

与 weft 文件的关系：审计链记录"对文件做了什么"（导入、合并、加密、重分片），
不复制文件内容——只存内容哈希，避免二次泄漏。
"""

from __future__ import annotations

import hashlib
import json
import os
import time

from .license import License

GENESIS = "0" * 64


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _hash(prev: str, record: dict) -> str:
    h = hashlib.sha256()
    h.update(prev.encode("ascii"))
    h.update(_canon(record))
    return h.hexdigest()


class AuditLog:
    """文件级审计链。

    用法：
        log = AuditLog("ops/audit.jsonl", lic=lic)   # 需要 audit 功能
        log.append("agent:42", "weft.build", "site.weft", {"bytes": 453})
        log.verify()      # {'ok': True, 'length': 1, ...}
    """

    def __init__(self, path: str, lic: License | None = None):
        from .enterprise import _require

        _require(lic, "audit")
        self.path = path
        self.head_path = path + ".head"
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)

    # ---- 读写 ----
    def _read_records(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def append(self, actor: str, action: str, target: str = "",
               payload: dict | None = None) -> dict:
        records = self._read_records()
        prev = records[-1]["hash"] if records else GENESIS
        record = {
            "seq": len(records) + 1,
            "ts": round(time.time(), 6),
            "actor": actor,
            "action": action,
            "target": target,
            "payload_sha256": hashlib.sha256(_canon(payload)).hexdigest() if payload else "",
            "prev": prev,
        }
        record["hash"] = _hash(prev, record)
        with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        with open(self.head_path, "w", encoding="utf-8") as fh:
            json.dump({"length": record["seq"], "head": record["hash"]}, fh)
        return record

    def verify(self) -> dict:
        records = self._read_records()
        prev = GENESIS
        for i, r in enumerate(records, 1):
            body = {k: v for k, v in r.items() if k != "hash"}
            if r.get("seq") != i:
                return {"ok": False, "length": len(records), "broken_at": i,
                        "reason": "序号不连续（有记录被删除）"}
            if r.get("prev") != prev:
                return {"ok": False, "length": len(records), "broken_at": i,
                        "reason": "prev 与前一条不衔接"}
            if _hash(prev, body) != r.get("hash"):
                return {"ok": False, "length": len(records), "broken_at": i,
                        "reason": "记录内容被改动（hash 不匹配）"}
            prev = r["hash"]

        anchor = None
        if os.path.exists(self.head_path):
            try:
                with open(self.head_path, encoding="utf-8") as fh:
                    anchor = json.load(fh)
            except Exception:
                anchor = None
        if anchor:
            if anchor.get("length") != len(records):
                return {"ok": False, "length": len(records), "broken_at": None,
                        "reason": f"条数与锚点不符（锚点 {anchor.get('length')}，实际 {len(records)}）"}
            if anchor.get("head") != prev:
                return {"ok": False, "length": len(records), "broken_at": None,
                        "reason": "链头与锚点不符（末尾被篡改）"}
        return {"ok": True, "length": len(records), "broken_at": None, "reason": "",
                "head": prev}

    def export(self) -> str:
        """原始 JSONL：交给外部审计独立重算，不需要信任本工具。"""
        if not os.path.exists(self.path):
            return ""
        with open(self.path, encoding="utf-8") as fh:
            return fh.read()
