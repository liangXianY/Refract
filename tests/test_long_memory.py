"""长期记忆适配层：没配 key 时必须完全静默，不能拖累主流程。

mem0 是可选依赖，多数环境（含 CI）没装。这个测试保证：即使 `import mem0`
直接炸，项目照常跑；配了 key 但 mem0 没装，也只是关掉而不是 500。
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents import long_memory  # noqa: E402


def test_off_by_default() -> None:
    """没配 MEM0_ENABLED + MEM0_EMBED_KEY 时必须是关的。"""
    for k in ("MEM0_ENABLED", "MEM0_EMBED_KEY"):
        os.environ.pop(k, None)
    st = long_memory.status()
    assert st["enabled"] is False
    assert "reason" in st
    # 关着的时候，读写都必须是空操作，不能抛
    assert long_memory.recall("u1", "写小红书") == []
    long_memory.note_turn("u1", "我叫小梁")


def test_enabled_needs_key() -> None:
    """只开开关不给 key 也不算开——半开状态最危险（写进去读不出来）。"""
    os.environ["MEM0_ENABLED"] = "1"
    os.environ.pop("MEM0_EMBED_KEY", None)
    try:
        assert long_memory.enabled() is False
    finally:
        os.environ.pop("MEM0_ENABLED", None)


def test_client_missing_lib_is_silent() -> None:
    """mem0 没装（或者装了但初始化失败）时，单例要记住失败而不是每次重试。"""
    os.environ["MEM0_ENABLED"] = "1"
    os.environ["MEM0_EMBED_KEY"] = "sk-test"
    os.environ["DATA_DIR"] = tempfile.mkdtemp()
    # 单例在前两个用例里已经标记过「试过了」，这里重置才能真的再走一遍初始化
    long_memory._TRIED = False
    long_memory._CLIENT = None
    # 强制走一次初始化：装了 mem0 的机器上会真连向量库，
    # 没装的会走 except 分支，两条路都不能抛异常到调用方
    try:
        long_memory._client()
    except Exception as exc:  # noqa: BLE001 - 适配层自己吞掉，测试只确认不往上冒
        raise AssertionError(f"初始化失败不该往上抛：{exc}") from exc
    finally:
        os.environ.pop("MEM0_ENABLED", None)
        os.environ.pop("MEM0_EMBED_KEY", None)


if __name__ == "__main__":
    test_off_by_default()
    test_enabled_needs_key()
    test_client_missing_lib_is_silent()
    print("OK long_memory 降级路径安全")
