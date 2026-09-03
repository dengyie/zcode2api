"""日志工具 — ANSI 彩色终端输出 (Windows GBK 兼容)"""

_R = "\033[0m"
_G = "\033[32m"
_C = "\033[36m"
_Y = "\033[33m"
_RED = "\033[31m"
_DIM = "\033[90m"
_B = "\033[1m"
_MAG = "\033[35m"
_W = "\033[37m"


def ok(module: str, msg: str):
    print(f"  {_G}[+]{_R} {_DIM}{module}{_R} {msg}")


def step(module: str, msg: str):
    print(f"  {_C}[>]{_R} {_DIM}{module}{_R} {msg}")


def info(module: str, msg: str):
    print(f"  {_DIM}[-] {module} {msg}{_R}")


def warn(module: str, msg: str):
    print(f"  {_Y}[~]{_R} {_DIM}{module}{_R} {msg}")


def err(module: str, msg: str):
    print(f"  {_RED}[!]{_R} {_DIM}{module}{_R} {msg}")


def header(module: str, msg: str):
    print(f"  {_B}{_MAG}{module}{_R} {msg}")


def req(req_id: str, model: str, stream: bool, last_msg: str):
    """请求日志 — 一行显示关键信息"""
    s = "stream" if stream else "sync"
    msg_preview = last_msg[:40] + ("..." if len(last_msg) > 40 else "")
    print(f"  {_C}>>>{_R} {_DIM}{req_id}{_R}  {_W}{model}{_R}  {_DIM}{s}{_R}  {_DIM}\"{msg_preview}\"{_R}")


def req_ok(req_id: str, tokens: int = 0):
    """请求完成"""
    t = f"  {_DIM}{tokens}tok{_R}" if tokens else ""
    print(f"  {_G}<<<{_R} {_DIM}{req_id}{_R}{t}")


def req_err(req_id: str, msg: str):
    """请求错误 — 只显示 req_id + 简短原因，不泄露上游"""
    print(f"  {_RED}<!>{_R} {_DIM}{req_id}{_R}  {msg}")


def banner(lines: list[str]):
    import re

    strip_ansi = re.compile(r"\033\[[0-9;]*m")
    w = max(len(strip_ansi.sub("", line)) for line in lines) + 4
    border = f"{_DIM}{'=' * w}{_R}"
    print(f"\n{border}")
    for line in lines:
        print(f"  {line}")
    print(f"{border}\n")
