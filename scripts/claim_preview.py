"""套餐投放探针 —— 「上游还有没有可领取的套餐」的唯一轻量答案。

背景（2026-10-31）：三个账号权益全部过期、`billing/preview` 实测 `plans=0`，
`/v1/messages` 只会回 `{"code":1005}`。此刻**任何 messages 探测都无意义**，
真正要盯的是「上游有没有重新投放」。

设计约束（刻意为之）：
  - 只打 `GET billing/preview`（业务端点、`trust_env=False` 直连），不碰
    messages / captcha —— 不产生风控面上的流量；
  - **不落库**：`--allow-disabled` 只在进程内临时置 active，用于绕过
    `billing_block_reason` 的禁用门（否则禁用号一律查不到投放）；
  - 只打印套餐 id/名称/模型，不打印任何凭证；
  - 退出码：0 = 查完（有没有投放看输出）；1 = 全部失败（网络/鉴权）。

用法（在 pxed 上，代码在 /data/zcode-hub/app/app）::

    cd /data/zcode-hub/app && ZCODE_DATA_DIR=/data/zcode-hub/data \
      PYTHONPATH=/data/zcode-hub/app \
      /data/zcode-hub/venv/bin/python /tmp/claim_preview.py --allow-disabled

⚠️ 路径坑：`/data/zcode-hub/app` 本身既是**陈旧副本**、又是真包 `app/` 的父目录。
`cd /data/zcode-hub/app`（让 `''` 命中真包）或 `PYTHONPATH=/data/zcode-hub/app`
（同时命中 `/data/zcode-hub/app/app/__init__.py`），**不能**把
`PYTHONPATH` 写成 `/data/zcode-hub/app/app`（那会去找 `.../app/app/app`）。

发现 `plans>0` 后再走领取（需验证码）：

    cd /data/zcode-hub/app && ZCODE_DATA_DIR=/data/zcode-hub/data \
      PYTHONPATH=/data/zcode-hub/app \
      /data/zcode-hub/venv/bin/python /tmp/claim_preview.py --allow-disabled --claim
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# 允许在仓库内直接运行（pxed 上代码在 /data/zcode-hub/app/app，脚本放在
# /data/zcode-hub/app/scripts 时同样成立）；从 /tmp 运行时靠 PYTHONPATH。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.claim import ClaimError, claim, preview_plans  # noqa: E402
from app.store import store  # noqa: E402


def _describe(account) -> str:
    return f"{account.name}（{account.status}，enabled={account.enabled}）"


async def _preview(account, *, allow_disabled: bool) -> list[dict]:
    if allow_disabled:
        # 仅进程内生效：build 通道判定靠 status，禁用号默认查不到投放
        account.status = "active"
        account.enabled = True
    return await preview_plans(account)


async def _run(args: argparse.Namespace) -> int:
    accounts = store.list_accounts("zai")
    if args.account:
        accounts = [a for a in accounts if a.name == args.account]
    if not accounts:
        print("没有匹配的账号")
        return 1

    failures = 0
    total_plans = 0
    for account in accounts:
        label = _describe(account)
        try:
            plans = await _preview(account, allow_disabled=args.allow_disabled)
        except ClaimError as err:
            failures += 1
            print(f"[{label}] 查询失败 code={getattr(err, 'code', None)}：{err}")
            continue
        except Exception as err:  # noqa: BLE001 - 探针不因单号异常中断
            failures += 1
            print(f"[{label}] {type(err).__name__}：{err}")
            continue

        total_plans += len(plans)
        print(f"[{label}] plans={len(plans)}")
        for plan in plans:
            models = ",".join(
                str(g.get("model") or g.get("show_name") or "?") for g in plan.get("grants", [])
            )
            print(f"    - {plan.get('plan_id')} · {plan.get('name') or '-'} · {models}")

        if plans and args.claim:
            try:
                outcome = await claim(account, plan_id=plans[0]["plan_id"])
            except ClaimError as err:
                print(f"    领取失败 code={getattr(err, 'code', None)}：{err}")
            except Exception as err:  # noqa: BLE001
                print(f"    领取异常 {type(err).__name__}：{err}")
            else:
                print(f"    领取结果：{outcome.get('message') or outcome}")

    print(f"合计 plans={total_plans}（失败 {failures} 个号）")
    return 1 if failures == len(accounts) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="查询上游当前可领取的套餐（不产生 messages 流量）")
    parser.add_argument("--account", help="只看某个账号（默认全部 zai 账号）")
    parser.add_argument("--allow-disabled", action="store_true",
                        help="绕过禁用门只做查询（进程内临时，不落库）")
    parser.add_argument("--claim", action="store_true",
                        help="发现投放后立即领取优先级最高的套餐（会用到验证码）")
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
