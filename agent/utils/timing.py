"""Server-Timing 响应头小工具。

给 dashboard 端点附加 ``Server-Timing`` 头,让浏览器 devtools 能看到
服务端各阶段耗时(如线程列表查询),不增加额外日志。
"""

# 单个 timing 项上限(毫秒精度),避免超大数值
_MAX_VALUE_MS = 1_000_000.0


def server_timing_header(value: float, label: str = "api") -> str:
    """构造 ``Server-Timing`` 头的值。

    参数:
        value: 耗时(毫秒,负数按 0 处理)。
        label: timing 项的名字。

    返回:
        形如 ``"api;dur=12.3"`` 的字符串。
    """
    # 限制在 [0, _MAX_VALUE_MS] 内,保留 3 位小数
    clamped = min(max(value, 0.0), _MAX_VALUE_MS)
    return f"{label};dur={clamped:.3f}"
