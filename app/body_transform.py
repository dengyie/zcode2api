"""请求体变换 —— anthropic 通道上的「官方 ZCode 客户端形态」变换集。

system 三块 + 日期轮 + cache_control + metadata.user_id 的构造形态，逐字节
对齐 Magpie packages/zcode/index.mjs 的 dress()（ZCode 3.17.2，见 zcode_system.json）：
  1. system 身份块：严格三块（cliPrefix / stableSections / dynamic），客户端
     自身 system 文本只许并入末块。网关做内容审查，缺官方块或多出第 4 块会被
     拒为 405 3012 "unusual activity"；11-03 起上游对 body 文本锁版本，前两块
     与版本头不等价即拒。
  2. cache_control：最后一条非 system 消息的最后一个 content block 追加
     `cache_control: {"type": "ephemeral"}`。Anthropic API 对低于缓存门槛的请求
     静默忽略 cache_control，因此无条件追加是安全的。
  3. metadata.user_id：注入 attribution blob
     `{"device_id": mid, "account_uuid": "", "session_id": sid}`（device_mid /
     session_id 优先）；JWT 账号无 device 档案时兜底用 JWT sub。

所有变换对畸形输入保持 no-op：解析失败返回原样，坏 body 不会被这里放大。
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from pathlib import Path

from . import constants, logs

# ── ZCode 官方 client 提示词（3.17.2，逐字节移植自 Magpie packages/zcode/prompt.mjs）──
# 结构：{cliPrefix, stableSections[2], dynamicBehavior, sessionGuidance,
#        contextManagement, poweredBy, environment{...}, context{...}}。
# 出处：ZCode 3.17.2 源码（zai-org/ZCode@a9fe401）经 Magpie plugins 仓库
# packages/zcode/prompt.mjs 归档，start.test.mjs 对其逐段做 SHA-256 前 16 hex 校验。
_PROMPT_JSON = Path(__file__).with_name("zcode_system.json")
try:
    _ZCODE_PROMPT: dict = json.loads(_PROMPT_JSON.read_text("utf-8"))
except (OSError, ValueError) as exc:
    _ZCODE_PROMPT = {}
    # 静默退化 = 「神秘 3012」无证据（2026-11-14 review）：官方形状装配失败必须留痕
    logs.warn("transform", f"官方提示词加载失败（{exc}）——请求将以非官方形状发出")


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


def _environment_text() -> str:
    """构建 environment 动态块（对齐 Magpie packages/zcode/index.mjs environment()）。

    hub 是网关、不掌握下游 agent 的 cwd/git/shell，故按 Magpie 在「未知 place」
    时的形态省略 cwd/git，只保留 platform / OS Version / untrustedNote 三行；
    platform 与 OS Version 直接来自本服务伪装的身份常量，与 X-Platform /
    X-Os-Version 头同源。
    """
    e = _ZCODE_PROMPT.get("environment") or {}
    plat_and_arch = (constants.CLIENT_PLATFORM or "darwin-arm64").split("-", 1)
    plat = plat_and_arch[0] or "darwin"
    arch = plat_and_arch[1] if len(plat_and_arch) > 1 else "arm64"
    lines = [
        e.get("heading", "# Environment"),
        e.get("invokedLine", "You have been invoked in the following environment: "),
        f" - {e.get('platformLabel', 'Platform')}: {plat}",
        f" - {e.get('osVersionLabel', 'OS Version')}: {plat} {constants.IDENTITY_OS_VERSION} {arch}",
        f" - {e.get('untrustedNote', '')}",
    ]
    return "\n".join(lines)


def apply_start_plan_system(body: dict, model: str | None = None) -> bool:
    """前置 ZCode 官方 system 三块，并把客户端自身 system 并入末块。幂等。

    严格三块 = dress() 逐字节同构（Magpie packages/zcode/index.mjs，ZCode 3.17.2）：
      block[0] = cliPrefix
      block[1] = stableSections 两段以 "\\n\\n" 拼接（官方前两块必须逐字节一致）
      block[2] = "\\n\\n" + join("\\n\\n", [dynamicBehavior, (有 Skill 工具时
                 sessionGuidance), environment, (有 model 时 poweredBy),
                 contextManagement, ...客户端自身 system 文本])
    trial/start-plan 下外来 system 文本只许并入末块内部；追加成第 4 块会破坏
    形态 → 405 3012 "unusual activity"。11-03 起上游对 body 文本锁版本，前两块
    与版本头不等价即拒（prompt.mjs 注释）。
    """
    cli_prefix = _ZCODE_PROMPT.get("cliPrefix")
    stable = _ZCODE_PROMPT.get("stableSections")
    if not cli_prefix or not isinstance(stable, list) or len(stable) < 2:
        return False
    existing = body.get("system")
    # 幂等：首块已是 cliPrefix 则视为已注入
    if isinstance(existing, list) and existing:
        first = existing[0]
        if isinstance(first, dict) and first.get("text") == cli_prefix:
            return False

    def cached(text: str) -> dict:
        return {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}

    own = [
        b["text"] for b in _normalize_user_system(existing)
        if isinstance(b.get("text"), str) and b["text"].strip()
    ]
    has_skill = any(
        isinstance(t, dict) and t.get("name") == "Skill"
        for t in (body.get("tools") or [])
    )

    dynamic: list[str] = [str(_ZCODE_PROMPT.get("dynamicBehavior") or "")]
    if has_skill and _ZCODE_PROMPT.get("sessionGuidance"):
        dynamic.append(str(_ZCODE_PROMPT["sessionGuidance"]))
    dynamic.append(_environment_text())
    if isinstance(model, str) and model.strip():
        powered = str(_ZCODE_PROMPT.get("poweredBy") or "")
        if powered:
            dynamic.append(
                powered.replace("{provider}", "zai-api").replace("{model}", model.strip())
            )
    dynamic.append(str(_ZCODE_PROMPT.get("contextManagement") or ""))
    dynamic.extend(own)

    body["system"] = [
        cached(cli_prefix),
        cached("\n\n".join(str(s) for s in stable)),
        cached("\n\n" + "\n\n".join(dynamic)),
    ]
    return True


def _is_date_turn_text(text: object) -> bool:
    """注入的「当前日期」user 轮的标记（幂等判定与会话指纹共用同一真相源）。"""
    return isinstance(text, str) and "<system-reminder>" in text and "Today's date is" in text


def apply_current_date_turn(body: dict) -> bool:
    """把官方「当前日期」提醒补进首条 user 轮（contextPrefix，Magpie 3.17.2 形态）。

    形态：`<system-reminder>Today's date is YYYY-MM-DD.</system-reminder>` 的独立
    user 轮（无换行，逐字节对齐 dress() 的 contextPrefix）。幂等：首条消息已是
    该形态则跳过。
    """
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return False
    first = messages[0]
    if isinstance(first, dict) and first.get("role") == "user":
        content = first.get("content")
        if isinstance(content, list) and content and isinstance(content[0], dict):
            if _is_date_turn_text(content[0].get("text")):
                return False
    ctx = _ZCODE_PROMPT.get("context") or {}
    line = str(ctx.get("currentDateLine") or "Today's date is {date}.")
    date_text = f"<system-reminder>{line.replace('{date}', time.strftime('%Y-%m-%d'))}</system-reminder>"
    body["messages"] = [
        {"role": "user", "content": [{"type": "text", "text": date_text}]},
        *messages,
    ]
    return True


def apply_cache_control(body: dict) -> bool:
    """官方缓存标记形态（Magpie packages/zcode dress 同构，2026-11-11 对齐）：

    1. 清除客户端自带的全部 cache_control——非 system 消息的每个 block、每个
       tool（官方客户端不发这些；客户端自设断点会改变上游 prompt-cache 的
       前缀边界，是额外的形状信号）；
    2. 只给最后一条非 system 消息打 ephemeral：字符串 content 包装成单 block
       （空串跳过，避免上游拒空 text block），list content 取最后一个 block。

    system 三块的 ephemeral 由 apply_start_plan_system 负责。幂等：二次调用零改动。
    """
    changed = False
    messages = body.get("messages")
    last = None
    if isinstance(messages, list):
        for msg in messages:
            if not isinstance(msg, dict) or msg.get("role") == "system":
                continue
            last = msg
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.pop("cache_control", None) is not None:
                        changed = True
    if last is not None:
        content = last.get("content")
        if isinstance(content, str):
            if content:
                last["content"] = [{
                    "type": "text",
                    "text": content,
                    "cache_control": {"type": "ephemeral"},
                }]
                changed = True
        elif isinstance(content, list) and content:
            last_block = content[-1]
            if isinstance(last_block, dict) and not last_block.get("cache_control"):
                last_block["cache_control"] = {"type": "ephemeral"}
                changed = True
    tools = body.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if isinstance(tool, dict) and tool.pop("cache_control", None) is not None:
                changed = True
    return changed


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


def _message_reads_as_query(msg: dict) -> bool:
    """这条 user 消息是否算一次「用户提问」（Magpie queryOf 的计数口径）。

    官方口径：content 为非空字符串，或块列表里存在任何非 tool_result 块——
    只带工具结果的 user 轮是模型的工具回传，不是新提问。日期注入轮不算。
    """
    content = msg.get("content")
    if isinstance(content, str):
        return bool(content.strip()) and not _is_date_turn_text(content)
    if not isinstance(content, list):
        return False
    for block in content:
        if not isinstance(block, dict) or block.get("type") == "tool_result":
            continue
        if block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip() and not _is_date_turn_text(text):
                return True
            continue
        return True  # image 等其它块同算一次提问
    return False


def conversation_query_id(body: dict, session_id: str | None) -> str | None:
    """轮次 attribution 的 x-query-id（Magpie queryOf 同构，轮内稳定）。

    官方 3.17.2 createModelRequestAttributionHeaders：一次用户提问带出的全部
    请求（含其工具循环）共用同一个 query-id，下一次提问换新 id。Anthropic
    协议每次带全量历史，「第几轮」= 数一遍带提问语义的 user 消息即可，天然
    确定性；日期注入轮跳过，重试/换号重复调用结果一致（幂等）。
    无提问轮返回 None——官方对无可归因的请求**不发** x-query-id。
    """
    turns = 0
    messages = body.get("messages")
    if isinstance(messages, list):
        for msg in messages:
            if isinstance(msg, dict) and msg.get("role") == "user" and _message_reads_as_query(msg):
                turns += 1
    if not turns:
        return None
    seed = f"zcode-query\0{session_id or ''}\0{turns}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


def extract_first_user_text(body: dict) -> str:
    """提取会话首条非日期注入的用户文本。无有效文本返回空字符串。"""
    messages = body.get("messages")
    if not isinstance(messages, list):
        return ""
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            if not _is_date_turn_text(content) and content.strip():
                return content.strip()
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text" \
                        and isinstance(block.get("text"), str):
                    candidate = block["text"]
                    if not _is_date_turn_text(candidate) and candidate.strip():
                        return candidate.strip()
    return ""


def conversation_session_id(body: dict, account_id: str = "") -> str | None:
    """同一会话的稳定 session_id（同首轮 → 同 id），无首轮文本返回 None。

    官方客户端的 `x-session-id` 与 metadata blob 的 `session_id` 在会话内
    稳定，prompt cache 的命中依赖它。hub 无会话态，用「会话首条 user 文本」
    做指纹：多轮请求里首轮文本不变，因此同一会话自然拿到同一 id。

    加入 account_id 隔离命名空间，避免不同账号/不同租户撞同一个 id
    （id 会随请求上报给上游，跨号相同是额外的关联信号）。

    ⚠️ 必须跳过注入的日期轮（2026-11-03 复现的缺陷）：`transform_body` 会把
    日期轮插到 messages[0] 且**原地**改写 body，而重试/换号路径会对同一个
    body 对象再次调用 `build_request`——不跳过的话，第二次起指纹就落在日期
    文本上，同一账号同一天所有会话撞成同一个 id（跨会话关联信号 + cache 键
    漂移）。跳过规则对「前插任意个匹配轮」幂等，指纹与首次调用一致。
    """
    text = extract_first_user_text(body)
    if not text:
        return None
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
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
    except Exception as exc:  # noqa: BLE001 - 变换永不放大请求失败，但退化必须留痕
        logs.warn("transform", f"body 变换异常（按原样发出，3012 风险）: {exc!r}")
    return body


# ── ToolSearch 别名（对齐 Magpie packages/zcode/index.mjs searchless）─────────
# 唯一应用点：agent.build_request（全通道、序列化前）；响应侧反向还原在
# gateway._Upstream.to_streaming。
# Claude Code 提供 ToolSearch 工具搜索（defer_loading 延迟加载工具）；官方
# ZCode 不发这两个概念。上游 Anthropic 端点见到**原名** ToolSearch 时会把
# 其它工具全部藏起（Bash/Read/Write 在列，仅 tool_reference 加载过的可见），
# 模型只能先「搜索」才能用基础工具。处置：改名成别名上架、剥离 defer_loading、
# 丢弃占位工具；响应侧把模型对别名的调用还原回原名（网关流式透传里做，
# 见 gateway._alias_reverse_iter）。api.z.ai / zcode.z.ai 同症，全通道生效。
TOOL_SEARCH = "ToolSearch"
TOOL_SEARCH_ALIAS = "ToolSearchTool"
TOOL_PLACEHOLDER = "DeferredToolPlaceholder"


def apply_tool_search_alias(body: dict) -> bool:
    """改名 ToolSearch→别名、剥离 defer_loading、丢弃占位工具。幂等。

    覆盖 tools 数组与 messages 历史里的 tool_use 调用名，保证上游看到的
    工具史自洽（历史里半新半旧是额外的形状信号）。客户端已自带别名的
    时候不改名（别名已被占用，改了会撞名）——Magpie searchless 同款守卫。
    """
    changed = False
    tools = body.get("tools")
    if isinstance(tools, list):
        # 别名已被占用则不改名（防撞名），其余清理照做
        alias_taken = any(
            isinstance(t, dict) and t.get("name") == TOOL_SEARCH_ALIAS for t in tools
        )
        kept: list = []
        for tool in tools:
            if not isinstance(tool, dict):
                kept.append(tool)
                continue
            if tool.get("name") == TOOL_PLACEHOLDER:
                changed = True
                continue  # 占位工具整个丢弃（官方不发）
            if "defer_loading" in tool:
                tool = {k: v for k, v in tool.items() if k != "defer_loading"}
                changed = True
            if not alias_taken and tool.get("name") == TOOL_SEARCH:
                tool = {**tool, "name": TOOL_SEARCH_ALIAS}
                changed = True
            kept.append(tool)
        if changed:
            body["tools"] = kept

    messages = body.get("messages")
    if isinstance(messages, list):
        for msg in messages:
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use" \
                        and block.get("name") == TOOL_SEARCH:
                    block["name"] = TOOL_SEARCH_ALIAS
                    changed = True
    return changed
