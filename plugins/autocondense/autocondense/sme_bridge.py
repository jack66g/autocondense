"""SME 记忆服务桥接：所有请求 trust_env=False，任何异常都不抛给调用方。"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger("autocondense")

DEFAULT_TAGS = ["project", "todo", "session_digest"]

# 去重阶段软预算：超时后跳过剩余去重，全部按新段入库（不中断提交）
DEDUP_BUDGET_S = 10.0


class SMEBridge:
    """SME（默认 127.0.0.1:8000，可配）的薄客户端。

    构造参数 base_url / timeout_s 之外，tags / importance / health_cache_s
    为带默认值的可选参数（默认值与 Config.sme 一致），便于配置注入与测试。
    """

    def __init__(self, base_url: str, timeout_s: float = 2.0,
                 tags: Optional[List[str]] = None, importance: float = 0.7,
                 health_cache_s: float = 60.0):
        self.base_url = (base_url or "").rstrip("/")
        self.timeout_s = timeout_s
        self.tags = list(tags or DEFAULT_TAGS)
        self.importance = importance
        self._health_cache_s = health_cache_s
        self._health_value: Optional[bool] = None
        self._health_at = 0.0
        self._lock = threading.Lock()

    def _client(self) -> httpx.Client:
        # 铁律：trust_env=False，避免系统代理劫持 localhost
        return httpx.Client(base_url=self.base_url, timeout=self.timeout_s, trust_env=False)

    # ------------------------------------------------------------------

    def health(self) -> bool:
        """GET /health，结果缓存 health_cache_s 秒（默认 60s）。"""
        now = time.monotonic()
        with self._lock:
            if self._health_value is not None and now - self._health_at < self._health_cache_s:
                return self._health_value
        value = False
        try:
            with self._client() as c:
                r = c.get("/health")
            value = r.status_code == 200
        except Exception as e:
            logger.debug("SME 健康探测失败：%s", e)
            value = False
        # 缓存计时从探测完成后起算：首次探测慢（冷启动可达数百毫秒）时，
        # 不能让缓存窗口在探测期间就流失掉（否则短缓存形同虚设、反复打点）
        with self._lock:
            self._health_value = value
            self._health_at = time.monotonic()
        return value

    def store_digest(self, digest_md: str, project: str, session_id: str,
                     trigger: str) -> Dict[str, Any]:
        """POST /memories 入库会话快照（整篇兜底），失败降级返回错误信息，不抛异常。"""
        body = {
            "text": f"[会话快照|{project}|{trigger}]\n{digest_md}",
            "tags": list(self.tags),
            "importance": self.importance,
            "source": "session",
            "metadata": {
                "tool": "autocondense",
                "session_id": session_id,
                "project": project,
                "trigger": trigger,
            },
        }
        try:
            with self._client() as c:
                r = c.post("/memories", json=body)
            if 200 <= r.status_code < 300:
                memory_id = None
                try:
                    data = r.json()
                    if isinstance(data, dict):
                        memory_id = data.get("id")
                except Exception:
                    memory_id = None
                return {"stored": True, "memory_id": memory_id, "error": None}
            return {"stored": False, "memory_id": None, "error": f"HTTP {r.status_code}"}
        except Exception as e:
            logger.debug("SME 入库请求失败：%s", e)
            return {"stored": False, "memory_id": None, "error": f"{type(e).__name__}: {e}"}

    def _segment_body(self, idx: int, seg: Any, metadata: Dict[str, Any]) -> Dict[str, Any]:
        """构造单段的 POST /memories（或 batch 元素）请求体。"""
        text = str(getattr(seg, "text", "") or "")
        tags = list(getattr(seg, "tags", []) or [])
        importance = getattr(seg, "importance", self.importance)
        seg_type = getattr(seg, "type", "misc")
        return {
            "text": text,
            "tags": tags,
            "importance": importance,
            "source": "session",
            "metadata": {
                "tool": "autocondense",
                "segment_type": seg_type,
                "segment_index": idx,
                **(metadata or {}),
            },
        }

    def _store_batch(self, segments: List[Any],
                     metadata: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """POST /memories/batch 一次往返批量入库；任何异常/非 2xx 返回 None（回落逐条）。

        SME 契约：body {"memories": [{"text","metadata","tags","importance","source"}, ...]}，
        响应 {"added": n, "dropped": n, "total": n}——不返回各条 id。因此批量模式下
        memory_ids 恒为空列表，/commit 聚合出的 sme.memory_id 为 null（可接受：
        索引回填 sme_memory_id 仅在拿到具体 id 时才做）；dropped>0 是 SME 提取管线
        主动去重/过滤，在 errors 里注明但不视为失败。
        """
        memories = [self._segment_body(i, seg, metadata)
                    for i, seg in enumerate(segments)]
        try:
            with self._client() as c:
                r = c.post("/memories/batch", json={"memories": memories})
            if not (200 <= r.status_code < 300):
                logger.debug("SME 批量入库不可用（HTTP %s），回落逐条", r.status_code)
                return None
            try:
                data = r.json()
            except Exception:
                data = {}
            if not isinstance(data, dict):
                return None

            def _count(key: str) -> int:
                v = data.get(key)
                return v if isinstance(v, int) and not isinstance(v, bool) else 0

            errors: List[str] = []
            dropped = _count("dropped")
            if dropped > 0:
                errors.append(f"SME 提取管线丢弃 {dropped} 条（重复/超限，不视为失败）")
            return {"stored": _count("added"), "failed": 0,
                    "memory_ids": [], "errors": errors}
        except Exception as e:
            logger.debug("SME 批量入库异常（回落逐条）：%s", e)
            return None

    def store_segments(self, segments: List[Any], metadata: Dict[str, Any]) -> Dict[str, Any]:
        """分段入库：≥2 段先试 batch（一次往返），失败回落逐条 POST /memories。

        - 批量成功 → stored=added、memory_ids=[]（batch 不返回各条 id）；
          dropped>0 只在 errors 注明，不算失败；
        - 批量任何异常/404/422 → 回落逐条路径（逐条才有 id）；
        - 单段失败记录到 errors 并继续，绝不 raise；
        - 返回 {stored, failed, memory_ids, errors} 成功/失败统计。
        """
        segs = list(segments or [])
        if len(segs) >= 2:
            batch_result = self._store_batch(segs, metadata)
            if batch_result is not None:
                return batch_result
            logger.info("SME 批量入库回落逐条提交（%d 段）", len(segs))

        ok = 0
        failed = 0
        memory_ids: List[Any] = []
        errors: List[str] = []
        for idx, seg in enumerate(segs):
            try:
                body = self._segment_body(idx, seg, metadata)
                with self._client() as c:
                    r = c.post("/memories", json=body)
                if 200 <= r.status_code < 300:
                    memory_id = None
                    try:
                        data = r.json()
                        if isinstance(data, dict):
                            memory_id = data.get("id")
                    except Exception:
                        memory_id = None
                    ok += 1
                    memory_ids.append(memory_id)
                else:
                    failed += 1
                    memory_ids.append(None)
                    errors.append(f"段 {idx}({getattr(seg, 'type', 'misc')}): HTTP {r.status_code}")
            except Exception as e:  # 单段失败不中断
                failed += 1
                memory_ids.append(None)
                errors.append(f"段 {idx}: {type(e).__name__}: {e}")
        return {"stored": ok, "failed": failed, "memory_ids": memory_ids,
                "errors": errors}

    # ------------------------------------------------------------------

    def _search_raw(self, query: str, top_k: int = 3) -> List[Dict[str, Any]]:
        """POST /memories/search 原始结果（含 metadata/score，不做标签过滤）。

        供去重判定用；任何异常返回空列表（调用方按「无重复」处理）。
        """
        try:
            with self._client() as c:
                r = c.post("/memories/search", json={"text": query, "top_k": top_k})
            if r.status_code != 200:
                return []
            data = r.json()
            items = data.get("results") if isinstance(data, dict) else None
            if not isinstance(items, list):
                return []
            return [it for it in items if isinstance(it, dict)]
        except Exception as e:
            logger.debug("SME 检索失败：%s", e)
            return []

    def dedupe_segments(self, segments: List[Any], project: str,
                        threshold: float = 0.92,
                        budget_s: Optional[float] = None
                        ) -> Tuple[List[Any], List[Any]]:
        """逐段查重：命中同项目同类型的高分旧段 → 强化（hit）而非重复入库。

        - 命中条件：score ≥ threshold 且 hit.metadata.project == project 且
          hit.metadata.segment_type == 段类型；
        - 命中 → POST /memories/{id}/hit 强化并记 id；未命中 → 留作新段；
        - search/hit 任何网络错误按「无重复」处理（不中断）；
        - 整个去重阶段有软预算（默认 ~10s），超时后剩余段跳过查重直接
          按新段入库（记日志），绝不阻塞提交。

        返回 (新段列表, 强化 id 列表)。
        """
        deadline = time.monotonic() + (DEDUP_BUDGET_S if budget_s is None else budget_s)
        new_segments: List[Any] = []
        reinforced: List[Any] = []
        remaining = list(segments or [])
        for seg in remaining:
            if time.monotonic() > deadline:
                logger.warning("SME 去重软预算（%.0fs）用尽，剩余 %d 段跳过查重直接入库",
                               DEDUP_BUDGET_S if budget_s is None else budget_s,
                               len(remaining) - len(new_segments) - len(reinforced))
                break
            text = str(getattr(seg, "text", "") or "")
            seg_type = getattr(seg, "type", "misc")
            matched_id = None
            for hit in self._search_raw(text[:160], top_k=3):
                score = hit.get("score")
                meta = hit.get("metadata") if isinstance(hit.get("metadata"), dict) else {}
                if (isinstance(score, (int, float)) and not isinstance(score, bool)
                        and score >= threshold
                        and meta.get("project") == project
                        and meta.get("segment_type") == seg_type):
                    matched_id = hit.get("id")
                    break
            if matched_id is None:
                new_segments.append(seg)
                continue
            try:
                with self._client() as c:
                    r = c.post(f"/memories/{matched_id}/hit")
                if 200 <= r.status_code < 300:
                    reinforced.append(matched_id)
                else:
                    # 强化失败按「无重复」处理：段照常入库，宁重不漏
                    new_segments.append(seg)
            except Exception as e:
                logger.debug("SME 强化失败（按无重复处理）：%s", e)
                new_segments.append(seg)
        if len(new_segments) + len(reinforced) < len(remaining):
            # 软预算用尽：剩余未查重段全部并入新段列表（全量入库）
            done = {id(seg) for seg in new_segments}
            for seg in remaining:
                if id(seg) not in done:
                    new_segments.append(seg)
        return new_segments, reinforced

    def search(self, query: str, top_k: int = 3) -> List[Dict[str, Any]]:
        """POST /memories/search 检索，客户端只保留 tags 含 session_digest 的结果。"""
        out: List[Dict[str, Any]] = []
        for it in self._search_raw(query, top_k):
            tags = it.get("tags") or []
            if isinstance(tags, list) and "session_digest" in tags:
                out.append({"id": it.get("id"), "text": it.get("text"), "score": it.get("score")})
        return out
