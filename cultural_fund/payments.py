"""支付网关适配。

生产环境对接国库集中支付/银行接口；这里给出可配置失败次数的模拟网关，
用于演示与测试“失败支付重试安全闭环”。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class GatewayResult:
    ok: bool
    gateway_ref: str = ""
    error: str = ""


class SimulatedPaymentGateway:
    """按 payment_id 配置“前 N 次失败”，之后成功；记录全部扣款请求。"""

    def __init__(self, fail_times: dict[str, int] | None = None) -> None:
        self._lock = threading.RLock()
        self._fail_remaining = dict(fail_times or {})
        self.calls: list[tuple[str, str, int]] = []

    def set_failure(self, payment_id: str, times: int) -> None:
        with self._lock:
            self._fail_remaining[payment_id] = times

    def charge(self, payment_id: str, amount: str, attempt_no: int) -> GatewayResult:
        with self._lock:
            self.calls.append((payment_id, amount, attempt_no))
            remaining = self._fail_remaining.get(payment_id, 0)
            if remaining > 0:
                self._fail_remaining[payment_id] = remaining - 1
                return GatewayResult(
                    ok=False,
                    error=f"模拟网关临时失败（剩余失败次数 {remaining - 1}）",
                )
            return GatewayResult(ok=True, gateway_ref=f"gw-{payment_id}-ok-{attempt_no}")
