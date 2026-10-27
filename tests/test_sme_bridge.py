"""sme_bridge 测试：真实 stub uvicorn（随机端口）上验证缓存/入库/降级/过滤。"""

import socket
import time

import httpx

from autocondense.sme_bridge import SMEBridge


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_health_cached(sme_stub):
    stub, base = sme_stub
    bridge = SMEBridge(base, timeout_s=2.0, health_cache_s=0.2)
    assert bridge.health() is True
    assert bridge.health() is True
    assert stub.health_hits == 1  # 缓存期内第二次不发请求
    time.sleep(0.3)
    assert bridge.health() is True
    assert stub.health_hits == 2  # 缓存过期后重新探测


def test_store_digest(sme_stub):
    stub, base = sme_stub
    bridge = SMEBridge(base, timeout_s=2.0)
    r = bridge.store_digest("# 快照正文", "proj", "s1", "plan_done")
    assert r == {"stored": True, "memory_id": "mem-1", "error": None}
    m = stub.memories[0]
    assert m["text"].startswith("[会话快照|proj|plan_done]\n")
    assert "session_digest" in m["tags"] and "project" in m["tags"]
    assert m["importance"] == 0.7
    assert m["source"] == "session"
    assert m["metadata"] == {"tool": "autocondense", "session_id": "s1",
                             "project": "proj", "trigger": "plan_done"}


def test_search_filters_session_digest(sme_stub):
    stub, base = sme_stub
    bridge = SMEBridge(base, timeout_s=2.0)
    bridge.store_digest("# 快照正文 alpha", "proj", "s1", "plan_done")
    # 手动塞一条没有 session_digest 标签的记忆（trust_env=False）
    with httpx.Client(trust_env=False) as c:
        c.post(f"{base}/memories",
               json={"text": "alpha 其他来源的记忆", "tags": ["project"], "importance": 0.5})
    results = bridge.search("alpha", top_k=5)
    assert len(results) == 1
    assert set(results[0]) == {"id", "text", "score"}
    assert results[0]["id"] == "mem-1"


def test_offline_degrades_without_raising():
    bridge = SMEBridge(f"http://127.0.0.1:{_free_port()}", timeout_s=0.5)
    assert bridge.health() is False
    r = bridge.store_digest("# x", "p", "s", "t")
    assert r["stored"] is False and r["memory_id"] is None and r["error"]
    assert bridge.search("x") == []


def test_search_http_error_returns_empty(sme_stub):
    _stub, base = sme_stub
    bridge = SMEBridge(base, timeout_s=2.0)
    # /memories/search 存在但用 GET 打错方法 → 405 → 空结果不抛
    with httpx.Client(trust_env=False) as c:
        assert c.get(f"{base}/memories/search").status_code == 405
    assert bridge.search("anything", top_k=3) == []
