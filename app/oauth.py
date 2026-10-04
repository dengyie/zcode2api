"""Z.AI / BigModel（国内版）OAuth 登录流程。

主要供 CLI `login zai` 与后台「授权登录」使用：发起 OAuth → 轮询 → 兑换 API Key。

双区域（constants.OAUTH_REALMS，对齐官方客户端 3.14.x 实证）：
- intl（zai）：init `{"provider":"zai"}`，授权页 chat.z.ai；poll 的
  `data.zai.access_token` 是 OAuth 会话凭证，需经 `api.z.ai/api/auth/z/login`
  换业务 token 后再走机构/项目/Key 兑换链（业务域 api.z.ai）；
- cn（bigmodel）：init `{"provider":"bigmodel"}`，授权页 bigmodel.cn/login
  （服务端回调）；poll 的 `data.bigmodel.access_token` 本身就是业务 token
  （官方客户端 BigModel adapter 不做二次换取），业务链在 bigmodel.cn 域。
两个区域的 `data.token` 都是 zcodejwttoken（Plan 通道 Bearer），messages /
billing / claim 端点 intl/cn 完全相同，无需分域。
"""

from __future__ import annotations

import secrets

import httpx

from . import constants, settings

REALM_INTL = "intl"
REALM_CN = "cn"


class ZaiAuthFlow:
    """一次 OAuth CLI 登录会话；realm 决定 provider 参数、业务 token 块与业务域。

    api_base / exchange_origin 可注入（测试指向 Mock 上游）；
    默认值来自 settings（其缺省又来自 constants 收口）。

    官方 CLI 规范（对齐 zcode.cjs createZaiCliOAuthClient）：
    - init 仅带 Authorization: Bearer <pollToken> 与 Content-Type: application/json
    - poll 仅带 Authorization: Bearer <pollToken>
    不携带额外伪装头，避免上游服务端对 OAuth 会话产生异常的设备/上下文绑定限制。

    2026-09 起官方协议跟进（zcode-switch oauth.rs 同形）：init 响应 data.poll_token
    由服务端下发，后续 poll 必须采用该值；仅当服务端未返回时回落自造 token
    （保留旧协议兼容）。
    """

    def __init__(self, api_base: str | None = None, exchange_origin: str | None = None,
                 realm: str = REALM_INTL) -> None:
        realm = str(realm or REALM_INTL).strip().lower()
        self.realm = realm if realm in constants.OAUTH_REALMS else REALM_INTL
        self.oauth_provider = constants.OAUTH_REALMS[self.realm]["oauth_provider"]
        self.api_base = api_base or settings.OAUTH_API_BASE
        if exchange_origin:
            self.exchange_origin = exchange_origin
        else:
            self.exchange_origin = (
                settings.BIGMODEL_EXCHANGE_ORIGIN if self.realm == REALM_CN
                else settings.ZAI_EXCHANGE_ORIGIN
            )
        self.poll_token = secrets.token_hex(32)

    async def init(self) -> tuple[str, str]:
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(
                f"{self.api_base}/oauth/cli/init",
                headers={
                    "Authorization": f"Bearer {self.poll_token}",
                    "Content-Type": "application/json",
                },
                json={"provider": self.oauth_provider},
            )
        res.raise_for_status()
        body = res.json()
        code = body.get("code")
        if code is not None and code != 0:
            raise RuntimeError(f"OAuth init 被上游拒绝（code={code}）")
        data = body.get("data") or {}
        flow_id, authorize_url = data.get("flow_id"), data.get("authorize_url")
        if not flow_id or not authorize_url:
            raise RuntimeError("返回的 OAuth 流程数据不完整")
        server_poll_token = (data.get("poll_token") or "").strip()
        if server_poll_token:
            self.poll_token = server_poll_token
        return flow_id, authorize_url

    async def poll(self, flow_id: str) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.get(
                f"{self.api_base}/oauth/cli/poll/{flow_id}",
                headers={"Authorization": f"Bearer {self.poll_token}"},
            )
        res.raise_for_status()
        return res.json().get("data") or {}

    def business_access_token(self, data: dict) -> str:
        """从 poll 结果提取业务 access_token；块名随区域（intl=data.zai，cn=data.bigmodel）。"""
        block = data.get(constants.OAUTH_PROVIDER_BLOCKS[self.realm])
        if isinstance(block, dict):
            return str(block.get("access_token") or "").strip()
        return ""

    async def _resolve_business_token(self, client: httpx.AsyncClient, access_token: str) -> str:
        """OAuth access_token → 业务 token。

        cn（bigmodel）：poll 下发的 access_token 本身就是业务 token，直接使用；
        intl（zai）：需经 exchange_origin 的 /api/auth/z/login 换业务 token。
        """
        if self.realm == REALM_CN:
            return access_token
        login = await client.post(
            f"{self.exchange_origin}/api/auth/z/login",
            headers={"Content-Type": "application/json"},
            json={"token": access_token},
        )
        login.raise_for_status()
        biz = (login.json().get("data") or {})
        biz_token = biz.get("access_token") or biz.get("accessToken")
        if not biz_token:
            raise RuntimeError("返回数据中不含业务凭证")
        return biz_token

    async def exchange_api_key(self, access_token: str) -> str:
        """OAuth access_token → 业务 token → 机构/项目 → API Key（业务域随区域）。"""
        if not access_token:
            raise RuntimeError("缺少 access_token，无法兑换 API Key")
        async with httpx.AsyncClient(timeout=30) as client:
            biz_token = await self._resolve_business_token(client, access_token)

            info = await client.get(
                f"{self.exchange_origin}/api/biz/customer/getCustomerInfo",
                headers={"Authorization": f"Bearer {biz_token}"},
            )
            info.raise_for_status()
            orgs = (info.json().get("data") or {}).get("organizations") or []
            org = next((o for o in orgs if "默认机构" in (o.get("organizationName") or "")), None) or (orgs[0] if orgs else None)
            if not org:
                raise RuntimeError("找不到可用的机构")
            projects = org.get("projects") or []
            proj = next((p for p in projects if "默认项目" in (p.get("projectName") or "")), None) or (projects[0] if projects else None)
            if not proj:
                raise RuntimeError("找不到可用的项目")

            org_id, proj_id = org["organizationId"], proj["projectId"]
            key_url = (f"{self.exchange_origin}/api/biz/v1/organization/"
                       f"{org_id}/projects/{proj_id}/api_keys")

            keys_res = await client.get(key_url, headers={"Authorization": f"Bearer {biz_token}"})
            keys_res.raise_for_status()
            keys = keys_res.json().get("data") or []
            key_obj = next((k for k in keys if k.get("name") == "zcode-api-key"), None)
            if not key_obj:
                create = await client.post(
                    key_url,
                    headers={"Authorization": f"Bearer {biz_token}", "Content-Type": "application/json"},
                    json={"name": "zcode-api-key"},
                )
                create.raise_for_status()
                key_obj = create.json().get("data")

            api_key = (key_obj or {}).get("apiKey")
            if not api_key:
                raise RuntimeError("获取 API Key 失败")

            copy = await client.get(
                f"{key_url}/copy/{api_key}",
                headers={"Authorization": f"Bearer {biz_token}"},
            )
            copy.raise_for_status()
            secret_key = (copy.json().get("data") or {}).get("secretKey")
            if not secret_key:
                raise RuntimeError("未能解密 Secret Key")
        return f"{api_key}.{secret_key}"
