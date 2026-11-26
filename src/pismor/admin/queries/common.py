"""管理查询共用的小工具。"""


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def like_pattern(value: str) -> str:
    return f"%{escape_like(value)}%"
