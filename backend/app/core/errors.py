from __future__ import annotations


class BizError(Exception):
    """业务规则错误，统一转成 HTTP 400/409 返回给前端。"""

    def __init__(self, message: str, status_code: int = 400, data: dict | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.data = data or {}


class NotFound(BizError):
    def __init__(self, what: str):
        super().__init__(f"{what}不存在", 404)


class Conflict(BizError):
    def __init__(self, message: str, data: dict | None = None):
        super().__init__(message, 409, data)
