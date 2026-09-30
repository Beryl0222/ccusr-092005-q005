"""轻量令牌鉴权。

每个用户持有不透明 Bearer 令牌；角色决定可执行命令，org_id 决定数据归属。
生产环境应替换为统一身份认证（OAuth2/JWT），接口保持 ``authenticate(token)`` 不变。
"""

from __future__ import annotations

import secrets as _secrets
from dataclasses import dataclass

from .errors import PermissionDeniedError
from .eventstore import Actor


@dataclass(frozen=True)
class User:
    user_id: str
    role: str
    org_id: str | None = None
    display_name: str = ""
    token: str = ""

    def to_actor(self) -> Actor:
        return Actor(
            user_id=self.user_id,
            role=self.role,
            org_id=self.org_id,
            display_name=self.display_name,
        )


class AuthRegistry:
    def __init__(self) -> None:
        self._users: dict[str, User] = {}
        self._tokens: dict[str, str] = {}

    def register(self, user: User, *, token: str | None = None) -> str:
        token = token or f"tok-{_secrets.token_hex(16)}"
        issued = User(
            user_id=user.user_id,
            role=user.role,
            org_id=user.org_id,
            display_name=user.display_name,
            token=token,
        )
        self._users[user.user_id] = issued
        self._tokens[token] = user.user_id
        return token

    def authenticate(self, token: str | None) -> Actor:
        if not token:
            raise PermissionDeniedError("缺少 Authorization: Bearer 令牌")
        user_id = self._tokens.get(token.removeprefix("Bearer ").removeprefix("bearer "))
        if user_id is None:
            raise PermissionDeniedError("令牌无效或已注销")
        return self._users[user_id].to_actor()

    def user(self, user_id: str) -> User | None:
        return self._users.get(user_id)

    def all_users(self) -> list[User]:
        return list(self._users.values())
