"""请求体变换 —— 对齐 zapi body-transformer.ts 在 anthropic 通道上的变换集。

coding-plan 通道（本服务 JWT 通道）应用三项：
  1. system 身份块：前置 ZCode 官方 system 块（CLI Prefix / Agent Identity /
     Environment Info）+ 动态 currentModel 块。网关做内容审查，请求缺这些
     身份块会被拒为 3012 "method not allowed"（镜像 zapi buildStartPlanSystem）。
  2. cache_control：最后一条非 system 消息的最后一个 content block 追加
     `cache_control: {"type": "ephemeral"}`（镜像 ZCode bundle 的 HLr，
     "finalizeLatestNonSystemCacheControl"）。Anthropic API 对低于缓存门槛的
     请求静默忽略 cache_control，因此无条件追加是安全的。
  3. metadata.user_id：JWT 账号存在 user_id 时注入（镜像 bundle 的
     `user_id: B.metadata.userId`）。user_id 每次从 JWT payload（sub / user_id
     字段）实时解出，token 刷新后自动跟随。

所有变换对畸形输入保持 no-op：解析失败返回原样，坏 body 永远不会被这里放大。
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import time
import uuid
from pathlib import Path

# ── ZCode 官方 system 身份块（对齐 zapi zcode_system.json，从官方客户端 bundle 提取）──
_SYSTEM_JSON = Path(__file__).with_name("zcode_system.json")
try:
    _ZCODE_SYSTEM_BLOCKS: list[dict] = json.loads(_SYSTEM_JSON.read_text("utf-8"))
except (OSError, ValueError):
    _ZCODE_SYSTEM_BLOCKS = []


def _is_plain_dict(v: object) -> bool:
    return isinstance(v, dict)


def _normalize_user_system(system: object) -> list[dict]:
    """把客户端原有 system 归一为 text block 列表（镜像 zapi normalizeUserSystem）。"""
    if system is None:
        return []
    if isinstance(system, str):
        text = system.strip()
        return [{"type": "text", "text": system}] if text else []
    if not isinstance(system, list):
        return []
    out: list[dict] = []
    for item in system:
        if isinstance(item, str):
            if item.strip():
                out.append({"type": "text", "text": item})
        elif isinstance(item, dict):
            if item.get("type") == "text" and isinstance(item.get("text"), str) and item["text"].strip():
                block = {"type": "text", "text": item["text"]}
                cc = item.get("cache_control")
                if isinstance(cc, dict):
                    block["cache_control"] = cc
                out.append(block)
    return out


def apply_start_plan_system(body: dict, model: str | None = None) -> bool:
    """前置 ZCode 官方 system 身份块，并把 powered-by / 客户端 system 并入最后一块。幂等。

    **严格三块**（2026-11-02 修正，此前为四块+，全池 3012）：
      - 网关校验的是 system 的**块结构**（cli_prefix 必须独立成首块），
        而非逐字内容——实测末块内容可任意（空串 / 仅前缀 / 无前缀均 200）。
      - 官方 harness 的 system 恰好 3 块，trial/start-plan 下外来 system 文本
        **只许并入末块内部**；追加成第 4 块、或把客户端 system 另起成独立块，
        都会破坏形态 → 405 3012 "unusual activity"。
    """
    if not _ZCODE_SYSTEM_BLOCKS:
        return False
    existing = body.get("system")
    # 幂等：首块已是官方标识则视为已注入
    if isinstance(existing, list) and existing:
        first = existing[0]
        if isinstance(first, dict) and first.get("text") == _ZCODE_SYSTEM_BLOCKS[0].get("text"):
            return False

    # 第 1、2 块原样；其余一切文本合并进第 3 块
    official = [copy.deepcopy(b) for b in _ZCODE_SYSTEM_BLOCKS[:2]]
    tail: list[str] = []
    for block in _ZCODE_SYSTEM_BLOCKS[2:]:
        text = block.get("text") if isinstance(block, dict) else None
        if isinstance(text, str) and text.strip():
            tail.append(text)
    if isinstance(model, str) and model.strip():
        tail.append(f"- You are powered by the model named {model}.")
    user_text = "\n\n".join(
        b["text"] for b in _normalize_user_system(existing)
        if isinstance(b.get("text"), str) and b["text"].strip()
    )
    if user_text:
        tail.append(user_text)

    official.append({
        "type": "text",
        "text": "\n\n".join(tail),
        "cache_control": {"type": "ephemeral"},
    })
    body["system"] = official
    return True


def apply_current_date_turn(body: dict) -> bool:
    """把官方 meta_user 的「当前日期」附件补进首条 user 轮（3.17.1 起不在 system 块里）。

    形态：`<system-reminder>\nToday's date is YYYY-MM-DD.\n</system-reminder>` 的独立 user 轮。
    幂等：首条消息已是该形态则跳过。
    """
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return False
    first = messages[0]
    if isinstance(first, dict) and first.get("role") == "user":
        content = first.get("content")
        if isinstance(content, list) and content and isinstance(content[0], dict):
            text = content[0].get("text")
            if isinstance(text, str) and "<system-reminder>" in text and "Today's date is" in text:
                return False
    date_text = f"<system-reminder>\nToday's date is {time.strftime('%Y-%m-%d')}.\n</system-reminder>"
    body["messages"] = [
        {"role": "user", "content": [{"type": "text", "text": date_text}]},
        *messages,
    ]
    return True


def apply_cache_control(body: dict) -> bool:
    """最后一条非 system 消息的最后一个 block 加 ephemeral 缓存标记。幂等。"""
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return False

    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "system":
            continue

        content = msg.get("content")
        if isinstance(content, str):
            msg["content"] = [{
                "type": "text",
                "text": content,
                "cache_control": {"type": "ephemeral"},
            }]
            return True
        if isinstance(content, list) and content:
            last_block = content[-1]
            if isinstance(last_block, dict) and not last_block.get("cache_control"):
                last_block["cache_control"] = {"type": "ephemeral"}
                return True
        return False
    return False


def apply_user_id(body: dict, user_id: str) -> bool:
    """注入 metadata.user_id（保留已有 metadata 其它字段）。幂等。"""
    existing = body.get("metadata")
    if _is_plain_dict(existing) and existing.get("user_id") == user_id:
        return False
    merged = dict(existing) if _is_plain_dict(existing) else {}
    merged["user_id"] = user_id
    body["metadata"] = merged
    return True


def attribution_user_id(device_mid: str | None, session_id: str | None) -> str:
    """官方 attribution 的 metadata.user_id 形态：JSON 字符串 blob。

    bundle `Bdt` / vlelyanin transform.js 实证：
      `{"device_id": <mid>, "account_uuid": "", "session_id": <sid>}`
    （旧版直塞 JWT sub 是错误形态）
    """
    blob = {
        "device_id": device_mid or "",
        "account_uuid": "",
        "session_id": session_id or "",
    }
    return json.dumps(blob, separators=(",", ":"))


def conversation_session_id(body: dict, account_id: str = "") -> str | None:
    """同一会话的稳定 session_id（同首轮 → 同 id），无首轮文本返回 None。

    官方客户端的 `x-session-id` 与 metadata blob 的 `session_id` 在会话内
    稳定，prompt cache 的命中依赖它。hub 无会话态，用「会话首条 user 文本」
    做指纹：多轮请求里首轮文本不变，因此同一会话自然拿到同一 id。

    加入 account_id 隔离命名空间，避免不同账号/不同租户撞同一个 id
    （id 会随请求上报给上游，跨号相同是额外的关联信号）。
    """
    messages = body.get("messages")
    if not isinstance(messages, list):
        return None
    text = ""
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text" \
                        and isinstance(block.get("text"), str):
                    text = block["text"]
                    break
        break
    if not text.strip():
        return None
    digest = hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:32]
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"zcode-hub:{account_id}:{digest}"))


def jwt_user_id(jwt_token: str | None) -> str | None:
    """从 JWT payload 解 user_id（sub / user_id 字段），失败返回 None。

    JWT 是账号凭证（短期有效），user_id 跟随 token 变化，因此不跨进程缓存。
    """
    if not jwt_token or jwt_token.count(".") != 2:
        return None
    try:
        payload_b64 = jwt_token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    except (ValueError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    user_id = payload.get("user_id") or payload.get("sub")
    return str(user_id) if user_id else None


def transform_body(
    body: dict,
    user_id: str | None = None,
    model: str | None = None,
    device_mid: str | None = None,
    session_id: str | None = None,
) -> dict:
    """按 anthropic 通道变换 body（原地修改并返回）。变换失败静默保持原样。

    user_id 为 JWT sub（仅用于兜底）；attribution 优先用 device_mid/session_id 组 blob。
    """
    try:
        apply_start_plan_system(body, model)
        apply_cache_control(body)
        apply_current_date_turn(body)
        attribution = attribution_user_id(device_mid, session_id) if (device_mid or session_id) else None
        if attribution:
            apply_user_id(body, attribution)
        elif user_id:
            apply_user_id(body, user_id)
    except Exception:  # noqa: BLE001 - 变换永不放大请求失败
        pass
    return body
