"""Z.AI OAuth 登录流程。

主要供 CLI `login zai` 使用：发起 OAuth → 轮询 → 兑换 API Key。
"""

from __future__ import annotations

import secrets

import httpx

from . import constants, settings


class ZaiAuthFlow:
    """api_base / exchange_origin 可注入（测试指向 Mock 上游）；
    默认值来自 settings（其缺省又来自 constants 收口）。"""

    def __init__(self, api_base: str | None = None, exchange_origin: str | None = None) -> None:
        self.api_base = api_base or getattr(settings, "OAUTH_API_BASE", None)             or constants.ZCODE_ORIGIN + "/api/v1"
        self.exchange_origin = exchange_origin or getattr(settings, "ZAI_EXCHANGE_ORIGIN", None)             or constants.ZAI_API_ORIGIN
        self.poll_token = secrets.token_hex(32)

    async def init(self) -> tuple[str, str]:
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(
                f"{self.api_base}/oauth/cli/init",
                headers={
                    "Authorization": f"Bearer {self.poll_token}",
                    "Content-Type": "application/json",
                },
                json={"provider": "zai"},
            )
        res.raise_for_status()
        data = res.json().get("data") or {}
        flow_id, authorize_url = data.get("flow_id"), data.get("authorize_url")
        if not flow_id or not authorize_url:
            raise RuntimeError("返回的 OAuth 流程数据不完整")
        return flow_id, authorize_url

    async def poll(self, flow_id: str) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.get(
                f"{self.api_base}/oauth/cli/poll/{flow_id}",
                headers={"Authorization": f"Bearer {self.poll_token}"},
            )
        res.raise_for_status()
        return res.json().get("data") or {}

    async def exchange_api_key(self, access_token: str) -> str:
        """OAuth access_token → 业务 token → 机构/项目 → API Key。"""
        async with httpx.AsyncClient(timeout=30) as client:
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
