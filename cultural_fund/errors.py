"""领域错误类型。"""

from __future__ import annotations


class DomainError(Exception):
    """所有可预期业务错误的基类。"""

    code = "domain_error"
    http_status = 400


class ValidationError(DomainError):
    """输入不满足领域约束。"""

    code = "validation_error"
    http_status = 422


class ConcurrencyError(DomainError):
    """事件流版本冲突（乐观锁）。"""

    code = "concurrency_error"
    http_status = 409


class NotFoundError(DomainError):
    """聚合或资源不存在。"""

    code = "not_found"
    http_status = 404


class IllegalStateError(DomainError):
    """当前状态不允许该操作（如阶段已锁定、争议未决）。"""

    code = "illegal_state"
    http_status = 409


class PermissionDeniedError(DomainError):
    """操作者角色或归属不允许该操作。"""

    code = "permission_denied"
    http_status = 403


class PaymentFailedError(DomainError):
    """支付网关返回失败（可重试，不是系统异常）。"""

    code = "payment_failed"
    http_status = 502
