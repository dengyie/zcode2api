#!/usr/bin/env python
"""ZCode Hub

用法:
  python cli.py serve [--port 3000]        启动网关 + 后台 UI
  python cli.py login zai [--no-browser]   通过 OAuth 登录 Z.AI 并自动加入账号池
  python cli.py add-account zai <name> <jwt|key>   添加轮询账号
  python cli.py accounts [zai|bigmodel]    查看账号列表
  python cli.py remove-account <provider> <id|name>
  python cli.py quota                      查看各账号实时额度
  python cli.py status                     查看配置概览
  python cli.py set-admin-key <key>        设置后台密码
  python cli.py export [file]              导出账号
  python cli.py import <file>              导入账号
"""

from __future__ import annotations

import asyncio
import json
import sys

from app import settings
from app.oauth import ZaiAuthFlow
from app.quota import fetch_quota
from app.store import store

C = {
    "reset": "\033[0m", "green": "\033[32m", "yellow": "\033[33m",
    "blue": "\033[34m", "red": "\033[31m", "cyan": "\033[36m", "bold": "\033[1m",
}


def c(text: str, color: str) -> str:
    return f"{C[color]}{text}{C['reset']}"


def usage() -> None:
    print(__doc__)


# ── serve ────────────────────────────────────────────────────────────────────
def cmd_serve(args: list[str]) -> None:
    port = settings.PORT
    if "--port" in args:
        i = args.index("--port")
        if i + 1 < len(args):
            port = int(args[i + 1])
    settings.PORT = port
    import uvicorn

    uvicorn.run("app.main:app", host=settings.HOST, port=port, log_level="info")


# ── login ────────────────────────────────────────────────────────────────────
async def cmd_login(args: list[str]) -> None:
    if not args or args[0] != "zai":
        print(c("目前仅支持: python cli.py login zai", "red"))
        return
    flow = ZaiAuthFlow()
    try:
        flow_id, authorize_url = await flow.init()
    except Exception as err:  # noqa: BLE001
        print(c(f"❌ 登录初始化失败: {err}", "red"))
        return

    print(c("\n✔ OAuth 初始化成功！请在浏览器中打开下面链接完成授权：", "green"))
    print(c(authorize_url, "blue"))

    if "--no-browser" not in args:
        try:
            import webbrowser
            webbrowser.open(authorize_url)
        except Exception:  # noqa: BLE001
            pass

    print("正在等待授权...")
    for _ in range(100):
        await asyncio.sleep(2)
        try:
            data = await flow.poll(flow_id)
        except Exception:  # noqa: BLE001
            continue
        status = data.get("status")
        if status == "ready":
            access_token = (data.get("zai") or {}).get("access_token")
            zcode_jwt = data.get("token")
            if zcode_jwt:
                acc = store.add_account("zai", "oauth-login", zcode_jwt)
                print(c(f"\n✔ 已保存 Coding Plan JWT 账号: {acc.name} ({acc.id})", "green"))
                await cli_auto_claim(acc)
            if access_token:
                try:
                    key = await flow.exchange_api_key(access_token)
                    store.add_account("zai", "oauth-apikey", key)
                    print(c(f"✔ 已兑换并保存 API Key: {key[:8]}...", "green"))
                except Exception as err:  # noqa: BLE001
                    print(c(f"⚠️ 兑换 API Key 失败: {err}", "yellow"))
            return
        if status == "failed":
            print(c("❌ 授权失败或被拒绝。", "red"))
            return
    print(c("❌ 登录超时，请重试。", "red"))


async def cli_auto_claim(acc) -> None:
    """入池自动领取（激活上报 + 全量可领套餐），失败仅提示不阻断。"""
    from app.claim import auto_claim_all_plans

    try:
        outcomes = await auto_claim_all_plans(acc)
        for o in outcomes:
            if o["ok"]:
                print(c(f"🎁 自动领取成功: {o.get('plan_name') or o.get('plan_id')}", "green"))
            else:
                print(c(f"⚠️ 自动领取失败: {o.get('message')}", "yellow"))
    except Exception as err:  # noqa: BLE001
        print(c(f"⚠️ 自动领取异常: {err}", "yellow"))


# ── 账号管理 ─────────────────────────────────────────────────────────────────
def cmd_add_account(args: list[str]) -> None:
    if len(args) < 3:
        print(c("格式: python cli.py add-account <zai|bigmodel> <name> <jwt|key>", "red"))
        return
    provider, name, secret = args[0], args[1], args[2]
    acc = store.add_account(provider, name, secret)
    print(c(f"✔ 已添加账号 {acc.name} ({acc.id}) 模式={acc.mode}", "green"))
    if acc.mode == "jwt" and acc.jwt_token:
        asyncio.run(cli_auto_claim(acc))


def cmd_accounts(args: list[str]) -> None:
    provider = args[0] if args and args[0] in ("zai", "bigmodel") else None
    accounts = store.list_accounts(provider)
    if not accounts:
        print("无账号")
        return
    print(c(f"\n--- 账号列表 ({provider or '全部'}) ---", "cyan"))
    for a in accounts:
        st = a.effective_status()
        print(f"{a.id}  {a.provider}  {a.mode}  {st}  {a.name}")


def cmd_remove_account(args: list[str]) -> None:
    if len(args) < 2:
        print(c("格式: python cli.py remove-account <provider> <id|name>", "red"))
        return
    if store.remove_account(args[0], args[1]):
        print(c(f"✔ 已删除账号 {args[1]}", "green"))
    else:
        print(c("⚠️ 未找到指定账号", "yellow"))


def cmd_set_admin_key(args: list[str]) -> None:
    if not args:
        print(c("格式: python cli.py set-admin-key <key>", "red"))
        return
    store.set_setting("admin_key", args[0])
    print(c("✔ 已更新后台密码", "green"))


def cmd_status() -> None:
    print(c("\n--- zcode-hub 状态 ---", "cyan"))
    print(f"数据库      : {c(str(settings.DB_PATH), 'blue')}")
    print(f"默认端口    : {c(str(settings.PORT), 'blue')}")
    print(f"后台密码    : {'已设置' if store.admin_key() else c('未设置', 'yellow')}")
    print(f"网关 API Key: {'已设置' if store.gateway_key() else '未设置（不校验）'}")
    for p in ("zai", "bigmodel"):
        accounts = store.list_accounts(p)
        active = sum(1 for a in accounts if a.is_selectable())
        print(f"{p:9s}  : {len(accounts)} 个账号，{active} 个可用")


async def cmd_quota() -> None:
    accounts = [a for a in store.list_accounts("zai") if a.mode == "jwt"]
    if not accounts:
        print(c("无 Coding Plan (JWT) 账号可查询额度。", "yellow"))
        return
    print(c("\n正在拉取各账号实时额度...", "cyan"))
    for a in accounts:
        await fetch_quota(a)
        print(c(f"\n账号: {a.name} ({a.effective_status()})", "bold"))
        if not a.quota:
            print("  无额度数据")
        for model, q in a.quota.items():
            rem, tot = q.get("remaining") or 0, q.get("total") or 0
            print(f"  {c(model, 'cyan')}: 剩余 {rem:,} / 总额 {tot:,}")


def cmd_export(args: list[str]) -> None:
    out = args[0] if args else "zcode-accounts.json"
    data = store.export()
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(c(f"✔ 已导出到 {out}", "green"))


def cmd_import(args: list[str]) -> None:
    if not args:
        print(c("格式: python cli.py import <file>", "red"))
        return
    with open(args[0], encoding="utf-8") as f:
        payload = json.load(f)
    count = store.import_accounts(payload)
    print(c(f"✔ 已导入 {count} 个账号", "green"))


# ── 分发 ─────────────────────────────────────────────────────────────────────
def main() -> None:
    argv = sys.argv[1:]
    if not argv:
        usage()
        return
    cmd, rest = argv[0], argv[1:]

    if cmd in ("help", "-h", "--help"):
        usage()
    elif cmd == "serve":
        cmd_serve(rest)
    elif cmd == "login":
        asyncio.run(cmd_login(rest))
    elif cmd == "add-account":
        cmd_add_account(rest)
    elif cmd == "accounts":
        cmd_accounts(rest)
    elif cmd == "remove-account":
        cmd_remove_account(rest)
    elif cmd == "set-admin-key":
        cmd_set_admin_key(rest)
    elif cmd == "status":
        cmd_status()
    elif cmd == "quota":
        asyncio.run(cmd_quota())
    elif cmd == "export":
        cmd_export(rest)
    elif cmd == "import":
        cmd_import(rest)
    else:
        print(c(f"未知命令: {cmd}", "red"))
        usage()


if __name__ == "__main__":
    main()
