"""管理查询共用的小工具。"""

# SQLite 的 LIKE 默认没有转义符；所有模糊匹配统一用反斜杠。
LIKE_ESCAPE = " ESCAPE '\\'"


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def like_expr(column: str) -> str:
    """单列模糊匹配片段；参数传 like_pattern(...) 的结果。"""
    return f"{column} LIKE ?{LIKE_ESCAPE}"


def like_any(*columns: str) -> str:
    """多列 OR 模糊匹配，参数按列顺序逐个传 like_pattern(...) 的结果。"""
    return " OR ".join(like_expr(column) for column in columns)


def like_pattern(value: str) -> str:
    return f"%{escape_like(value)}%"