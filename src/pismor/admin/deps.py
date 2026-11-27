"""管理后台的权限占位层。

本期没有真实认证：`get_principal` 恒返回本地管理员。以后接入认证时只替换
这个函数（或增加认证依赖），所有路由的 `require(...)` 和 capability 名称不变。
"""
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request


CAPABILITIES = frozenset({
    "review.read",
    "review.decide",
    "memory.read",
    "memory.write",
    "memory.delete",
    "system.read",
    "taxonomy.read",
    "taxonomy.write",
    "config.read",
    "config.write",
    "settings.read",
    "settings.write",
})


@dataclass(frozen=True)
class Principal:
    subject: str
    display_name: str
    roles: tuple[str, ...]
    capabilities: frozenset[str]

    def has(self, capability: str) -> bool:
        return capability in self.capabilities


LOCAL_ADMIN = Principal(
    subject="local-admin",
    display_name="本地管理员",
    roles=("admin",),
    capabilities=CAPABILITIES,
)


def get_principal(request: Request) -> Principal:
    """认证占位：未来从 Request 的认证结果取 Principal。"""
    return getattr(request.app.state, "principal", LOCAL_ADMIN)


def get_settings(request: Request):
    """读当前运行配置；配置中心 replace 后立即对新请求生效。"""
    store = getattr(request.app.state, "settings_store", None)
    if store is not None:
        return store.current()
    return request.app.state.settings


def get_taxonomy(request: Request):
    """读当前词表；配置中心保存后立即对新请求生效。"""
    store = getattr(request.app.state, "taxonomy_store", None)
    if store is not None:
        return store.current()
    return request.app.state.taxonomy


def require(*capabilities: str):
    """要求全部 capability 的依赖工厂。"""
    unknown = [cap for cap in capabilities if cap not in CAPABILITIES]
    if unknown:
        raise ValueError(f"unknown capability: {unknown}")

    def dependency(principal: Principal = Depends(get_principal)) -> Principal:
        for capability in capabilities:
            if not principal.has(capability):
                raise HTTPException(
                    status_code=403,
                    detail=f"missing capability: {capability}",
                )
        return principal

    return dependency
