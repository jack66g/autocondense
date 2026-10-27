"""pytest 公共夹具：应用工厂 + 线程内真起的 SME stub 服务。"""

from __future__ import annotations

import threading
import time

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse


class StubSMEApp:
    """SME 记忆服务的最小桩：/health /memories /memories/batch /memories/search
    /memories/{id}/hit。

    fail_markers：POST /memories（含 batch 元素）的 text 含任一标记时返回 500
    （模拟单段/整批失败）。
    可调开关：
    - reject_batch=True → POST /memories/batch 返回 404（测批量回落逐条）
    - batch_dropped=True → batch 响应 dropped=1（测「丢弃不视为失败」）
    - search_score → 检索结果统一分数基准（默认 0.9 < 去重阈值 0.92，
      旧测试不受去重影响；调高可测去重命中）
    - search_fail=True → 检索 500（测去重容错）
    - hit_fail=True → 强化 500（测强化失败按无重复处理）
    - hits 列表记录 POST /memories/{id}/hit 收到的 id

    检索打分：去掉 `[autocondense|...]` 前缀后取查询前 40 字符做包含匹配
    （命中 → search_score），否则退化为前 3 字符弱匹配（search_score-0.3）。
    这样同一摘要二次提交时各段内容 probe 命中旧段，可真实走出去重路径。
    """

    def __init__(self):
        self.health_hits = 0
        self.memories = []
        self.fail_markers: list = []
        self.reject_batch = False
        self.batch_dropped = False
        self.batch_calls = 0
        self.hits: list = []
        self.search_score = 0.9
        self.search_fail = False
        self.hit_fail = False
        app = FastAPI()

        @app.get("/health")
        def _health():
            self.health_hits += 1
            return {"status": "ok"}

        @app.post("/memories")
        def _create(body: dict):
            text = str(body.get("text") or "")
            if any(m in text for m in self.fail_markers):
                return JSONResponse(status_code=500, content={"error": "stub boom"})
            mid = f"mem-{len(self.memories) + 1}"
            self.memories.append({"id": mid, **body})
            return {"id": mid, "status": "stored"}

        @app.post("/memories/batch")
        def _batch(body: dict):
            self.batch_calls += 1
            if self.reject_batch:
                return JSONResponse(status_code=404, content={"error": "no batch"})
            items = body.get("memories") if isinstance(body, dict) else None
            if not isinstance(items, list) or not all(isinstance(m, dict) for m in items):
                return JSONResponse(status_code=422, content={"error": "bad batch body"})
            texts = [str(m.get("text") or "") for m in items]
            if any(any(mk in t for mk in self.fail_markers) for t in texts):
                return JSONResponse(status_code=500, content={"error": "stub boom"})
            for m in items:
                self.memories.append({"id": f"mem-{len(self.memories) + 1}", **m})
            dropped = 1 if self.batch_dropped else 0
            return {"added": len(items) - dropped, "dropped": dropped,
                    "total": len(items)}

        @app.post("/memories/{mid}/hit")
        def _hit(mid: str):
            if self.hit_fail:
                return JSONResponse(status_code=500, content={"error": "hit boom"})
            self.hits.append(mid)
            return {"reinforced": mid}

        @app.post("/memories/search")
        def _search(body: dict):
            if self.search_fail:
                return JSONResponse(status_code=500, content={"error": "search boom"})
            import re as _re
            q = str(body.get("text") or "").lower()
            core = _re.sub(r"^\[[^\]]*\]\s*", "", q)   # 去掉 [autocondense|...] 前缀
            probe = (core or q)[:40]
            weak = q[:3]
            scored = []
            for m in self.memories:
                text = str(m.get("text", "")).lower()
                mcore = _re.sub(r"^\[[^\]]*\]\s*", "", text)
                if probe and (probe in text or probe in mcore):
                    score = float(self.search_score)
                elif weak and weak in text:
                    score = round(float(self.search_score) - 0.3, 3)
                else:
                    continue
                scored.append({"id": m["id"], "text": m.get("text", ""),
                               "score": score, "tags": m.get("tags", []),
                               "metadata": m.get("metadata", {})})
            scored.sort(key=lambda r: -r["score"])
            top_k = body.get("top_k") if isinstance(body.get("top_k"), int) else 3
            return {"results": scored[:max(1, top_k)]}

        self.app = app


@pytest.fixture
def sme_stub():
    """线程内真起一个 stub uvicorn（随机端口），返回 (stub, base_url)。"""
    stub = StubSMEApp()
    config = uvicorn.Config(stub.app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "stub SME 服务启动失败"
    port = server.servers[0].sockets[0].getsockname()[1]
    yield stub, f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def make_app():
    """工厂：make_app(tmp_path, sme_base_url=None) → FastAPI 应用。

    显式传 data_dir（避免依赖环境变量），测试绝不碰真实 data/。
    未显式给 sme_base_url 时一律指向秒拒端口（127.0.0.1:9）——历史教训：
    默认 SME 端口本机可能真有服务在监听，测试 commit 会把测试段写进
    用户真实记忆库；不传 stub 的用例必须真实走到「SME 不在线」降级路径。
    """
    def _make(tmp_path, sme_base_url=None):
        from autocondense.service import create_app
        return create_app(data_dir=tmp_path / "data",
                          sme_base_url=sme_base_url or "http://127.0.0.1:9")

    return _make
