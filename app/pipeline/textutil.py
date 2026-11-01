"""文本安全工具：清理模型输出里混入的非法 Unicode 字符。

为什么需要：LLM（尤其 Opus/Claude 走 Anthropic 协议）输出长 HTML 时，
emoji 或特殊符号偶尔被拦腰截断，留下「孤立代理字符」（surrogate，
U+D800–U+DFFF）。这种半截字符 UTF-8 无法编码，`str.encode('utf-8')` 和
`Path.write_text(encoding='utf-8')` 会直接抛 UnicodeEncodeError。

处理原则：宁可丢一个坏字符，也不能让整篇文章/整条视频落盘失败。
所以用 errors='ignore' 在 utf-8 边界上过一遍，坏字符直接剔除。
"""

from __future__ import annotations


def clean_text(s: str) -> str:
    """剔除非法 surrogate 字符，返回可安全 utf-8 编码的文本。

    对正常文本零副作用（无 surrogate 时原样返回等价字符串）。
    """
    if s is None:
        return ""
    try:
        return s.encode("utf-8", errors="ignore").decode("utf-8", errors="ignore")
    except Exception:
        return str(s)
