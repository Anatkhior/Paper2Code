"""BYOK 的唯一抽象层。

铁律：**协议差异只允许存在于这个文件里**。
Agent 循环与工具代码永远只看到 OpenAI 形状的 messages / tools，
换 provider 不需要改任何一行 Agent 代码。

同时这里有 §4 要求的「启动自检」：smoke_test()。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import deque
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Literal

import litellm
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import settings

# ---------------------------------------------------------------------------
# litellm 全局开关
# ---------------------------------------------------------------------------
litellm.telemetry = False          # 不外发遥测
litellm.drop_params = True         # 端点不支持的参数（如 stream_options）自动丢弃，而不是直接报错
litellm.suppress_debug_info = True
litellm.set_verbose = False


# ---------------------------------------------------------------------------
# provider 描述符
# ---------------------------------------------------------------------------
class ProviderConfig(BaseModel):
    """用户在前端填的那三个框。

    api_key 标记为 repr=False：即使有人不小心 print(model) 也不会把 key 打进日志。
    """

    model_config = ConfigDict(extra="forbid")

    protocol: Literal["openai-compatible", "anthropic"] = "openai-compatible"
    base_url: str | None = None
    api_key: str = Field(min_length=1, repr=False)
    model: str = Field(min_length=1)
    label: str | None = None  # 仅供 UI 显示，如 "DeepSeek 官方"

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        v = v.rstrip("/")
        if not v.startswith(("http://", "https://")):
            raise ValueError("base_url 必须以 http:// 或 https:// 开头")
        return v

    def litellm_model(self) -> str:
        """把 (protocol, model) 翻译成 litellm 的模型串。

        openai-compatible -> "openai/<model>"   （litellm 用这个前缀表示"OpenAI 协议形状"）
        anthropic         -> "anthropic/<model>"
        """
        if "/" in self.model:
            # 用户已经自己写了前缀（例如 "openai/gpt-4o" 或 "deepseek/deepseek-chat"），尊重它
            return self.model
        prefix = "openai" if self.protocol == "openai-compatible" else "anthropic"
        return f"{prefix}/{self.model}"

    def safe_label(self) -> str:
        """可以安全写进日志/前端的描述（绝对不含 key）。"""
        host = "官方端点"
        if self.base_url:
            host = self.base_url.split("//", 1)[-1].split("/", 1)[0]
        return f"{self.protocol} · {host} · {self.model}"


class ToolCall(BaseModel):
    id: str
    name: str
    arguments_raw: str = ""

    def arguments(self) -> dict[str, Any]:
        """解析参数。解析失败返回空 dict —— 调用方必须自己判断并回一条 tool 错误。"""
        if not self.arguments_raw.strip():
            return {}
        try:
            value = json.loads(self.arguments_raw)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}


class TurnResult(BaseModel):
    text: str = ""
    tool_calls: list[ToolCall] = []
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    deltas_seen: int = 0
    latency_ms: int = 0
    # 用量是端点报的还是我们估的 —— 必须诚实标注（§8）
    token_source: Literal["provider", "estimate"] = "estimate"

    def assistant_message(self) -> dict[str, Any]:
        """转成 OpenAI 形状的 assistant 消息，塞回 messages。"""
        msg: dict[str, Any] = {"role": "assistant", "content": self.text or None}
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments_raw or "{}"},
                }
                for tc in self.tool_calls
            ]
        return msg


# ---------------------------------------------------------------------------
# token 估算（不依赖 tiktoken 的离线下载）
# ---------------------------------------------------------------------------
def estimate_tokens(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> int:
    """粗估输入 token：约 4 字符 / token。

    为什么不用 litellm.token_counter：它会拉 tiktoken 的 BPE 文件（需要联网），
    而在本地跑、或者在别人的机器上第一次跑，这一步可能直接失败。
    预算护栏只需要一个量级正确的数字，宁可粗估也不要为了精确而崩掉。
    """
    payload = json.dumps(messages, ensure_ascii=False, default=str)
    if tools:
        payload += json.dumps(tools, ensure_ascii=False)
    return max(1, len(payload) // 4)


# ---------------------------------------------------------------------------
# 流式累积器
# ---------------------------------------------------------------------------
class _Accumulator:
    """把流式 chunk 拼成一条完整的 assistant 消息。

    这是整个后端最容易出错的地方，也是最值得写测试的地方：
    工具调用参数一定会在 chunk 边界被切断（例如 '{"pa' + 'ge": 1}'），
    所以要按 index 累积，而不是假设每个 chunk 是完整的。
    """

    def __init__(self) -> None:
        self.text_parts: list[str] = []
        self.calls: dict[int, dict[str, str]] = {}
        self.finish_reason: str | None = None
        self.usage: dict[str, Any] | None = None
        self.deltas_seen = 0

    def absorb(self, chunk: Any) -> str:
        """吸收一个 chunk，返回本 chunk 新增的文本（供 SSE 实时推送）。"""
        self.deltas_seen += 1
        new_text = ""

        usage = getattr(chunk, "usage", None)
        if usage is not None:
            self.usage = usage if isinstance(usage, dict) else _to_dict(usage)

        choices = getattr(chunk, "choices", None) or []
        if not choices:
            return ""  # include_usage 的最后一个 chunk 只有 usage，没有 choices

        choice = choices[0]
        finish = getattr(choice, "finish_reason", None)
        if finish:
            self.finish_reason = finish

        delta = getattr(choice, "delta", None)
        if delta is None:
            return ""

        content = getattr(delta, "content", None)
        if content:
            self.text_parts.append(content)
            new_text = content

        for frag in getattr(delta, "tool_calls", None) or []:
            idx = getattr(frag, "index", 0) or 0
            slot = self.calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
            fid = getattr(frag, "id", None)
            if fid:
                slot["id"] = fid
            fn = getattr(frag, "function", None)
            if fn is not None:
                name = getattr(fn, "name", None)
                if name:
                    slot["name"] = name
                args = getattr(fn, "arguments", None)
                if args:
                    slot["arguments"] += args  # ← 关键：跨 chunk 拼接

        return new_text

    def to_result(self, latency_ms: int) -> TurnResult:
        calls = []
        for idx in sorted(self.calls):
            slot = self.calls[idx]
            calls.append(
                ToolCall(
                    id=slot["id"] or f"call_{idx}",
                    name=slot["name"],
                    arguments_raw=slot["arguments"],
                )
            )
        return TurnResult(
            text="".join(self.text_parts),
            tool_calls=calls,
            finish_reason=self.finish_reason,
            usage=self.usage,
            deltas_seen=self.deltas_seen,
            latency_ms=latency_ms,
            token_source="provider" if self.usage else "estimate",
        )


def _to_dict(obj: Any) -> dict[str, Any]:
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "dict"):
        return obj.dict()
    return dict(obj) if isinstance(obj, dict) else {}


# ---------------------------------------------------------------------------
# 一次 LLM turn
# ---------------------------------------------------------------------------
def _llm_headers() -> dict[str, str] | None:
    """LLM 请求附带的额外头（当前只有 User-Agent）。

    默认带自定义标识而不是 SDK 默认的 "OpenAI/Python x.x"：有些套 Cloudflare 的
    第三方网关会按 UA 把 SDK 形状的请求直接 403（实测 2026-09-13），请求到不了模型。
    设 PAPERLENS_HTTP_USER_AGENT="" 可回退 SDK 默认。
    """
    if not settings.http_user_agent:
        return None
    return {"User-Agent": settings.http_user_agent}


# ---------------------------------------------------------------------------
# 端点限额（RPM / TPM / 额度用尽）：权威信号优先 + 兜底节流 + 分类报错
# ---------------------------------------------------------------------------
# 为什么必须做：一次定位/侦察要几十次 LLM 调用（一次 turn 一次请求）。实测某中转站
# 限制「1 分钟最多 10 次，含失败次数」，用户那次 20 轮只花 61.6s ≈ 19.5 次/分钟 → 必然撞；
# 而旧代码撞到 429 直接让整个 run failed，前面跑出来的东西全丢。
#
# 判据按"越靠前越权威"排序（尽量不猜，也尽量不要求用户知道自己的限额）：
#   1. 配置：PAPERLENS_LLM_MAX_REQUESTS_PER_MINUTE（用户知道限额时最省事）
#   2. **响应头**（不需要用户知道任何东西）：
#      成功响应 → x-ratelimit-limit-requests: `10, 10;w=60`（上限, 突发; 窗口秒）、
#                 x-ratelimit-limit-tokens（token/分钟）
#      失败响应 → litellm_response_headers 里的 retry-after
#      注意：流式响应要把头从 `response.completion_stream.response.headers` 里取，
#      异常要从 `exc.litellm_response_headers` 取——实测 `exc.response.headers` 是**空的**
#      （2026-09-15 踩过：写了读头的分支却读不到，只有中文话术那层侥幸生效）
#   3. 网关话术：`1分钟内最多请求10次`（中英文都认）
#   4. 兜底：窗口感知阶梯（10s 起、翻倍到 90s），按"总等待预算"而非"5 次"计数
#
# 三类要分开对待（不同用户会撞不同的墙）：
#   - transient 限流（RPM）→ 退避重试 + 学会节奏，之后自动降速
#   - TPM（token/分钟）→ 按请求降速**没用**，要按 token 节流：用声明的/学到的
#     limit-tokens + 最近 60s 已发送 token 决定是否要等
#   - 额度用尽/欠费（402 或 429 + quota/余额话术）→ 重试永远不会成功，**不重试**，直接说清
TOKEN_WINDOW_SECONDS = 60.0
FALLBACK_FIRST_DELAY = 10.0
FALLBACK_MAX_DELAY = 90.0
RETRY_TOTAL_WAIT_SECONDS = 180.0     # 重试总等待预算（比"重试几次"更贴近真实窗口）
PACE_FILE_NAME = "llm-pace.json"     # 学到的节奏落盘：重启不用重学（不含任何密钥）
NOTICE_WAIT_SECONDS = 10.0           # 只有等得久才打扰用户（否则每次调用都刷屏）

_LLM_PACE_LOCK = asyncio.Lock()
_LLM_LAST_CALL_AT = 0.0
_LLM_LEARNED_INTERVAL_SECONDS = 0.0      # 两次请求最小间隔（≥ 配置值）
_LLM_LEARNED_TOKEN_LIMIT = 0.0           # token/分钟
_LLM_PACE_SOURCE = "none"                # config / header / prose / learned / none
_LLM_TOKEN_SOURCE = "none"
_LLM_TOKEN_WINDOW: deque[tuple[float, int]] = deque()   # (monotonic, tokens)
_RATE_LIMIT_HINTS: list[float] = []      # 最近几次被限流建议的等待（展示/断言用）
_PACE_CACHE: dict[str, dict[str, float]] = {}
_PACE_LOADED = False

QUOTA_MARKERS = (
    "insufficient", "quota", "balance", "credit", "billing", "exceeded your current",
    "余额", "配额", "欠费", "额度不足", "已用完", "免费额度",
)
TOKEN_LIMIT_MARKERS = (
    "tokens per minute", "token per min", "tokens_per_minute", "tpm",
    "token rate limit", "令牌", "token 速率",
)
# 网络/网关侧的瞬时故障：**重试往往就好了**（实测用户跑到第 10 轮时遇到
# `InternalServerError: Connection error.`，旧代码归到"其他" → 直接失败、整轮作废）。
# 刻意**不包含超时**：超时意味着"端点太慢"，重试只会在同一个时限上再等一遍，
# 而 per_turn_timeout_seconds 是用户设的护栏（自检也靠它判定慢端点）。
TRANSIENT_MARKERS = (
    "connection error", "connection reset", "connection aborted", "connection refused",
    "server disconnected", "remote protocol", "eof occurred", "broken pipe",
    "temporarily unavailable", "bad gateway", "service unavailable", "gateway time-out",
    "gateway timeout", "internal server error", "connection closed",
    "连接中断", "连接被重置", "服务不可用", "midstream",
)
TRANSIENT_STATUSES = (500, 502, 503, 504, 520, 521, 522, 523, 524, 529)


def reset_llm_pacing() -> None:
    """清掉学到的节奏与 token 窗口（验收脚本用；正常运行时不需要）。"""
    global _LLM_LEARNED_INTERVAL_SECONDS, _LLM_LEARNED_TOKEN_LIMIT, _LLM_LAST_CALL_AT
    global _LLM_PACE_SOURCE, _LLM_TOKEN_SOURCE, _PACE_LOADED
    _LLM_LEARNED_INTERVAL_SECONDS = 0.0
    _LLM_LEARNED_TOKEN_LIMIT = 0.0
    _LLM_LAST_CALL_AT = 0.0
    _LLM_PACE_SOURCE = "none"
    _LLM_TOKEN_SOURCE = "none"
    _LLM_TOKEN_WINDOW.clear()
    _RATE_LIMIT_HINTS.clear()
    _PACE_CACHE.clear()
    _PACE_LOADED = True        # 验收里不要再从磁盘加载，避免互相污染


def classify_llm_error(exc: Exception) -> str:
    """把端点错误分成四类，**重试策略按类决定**。

    quota_exhausted 是最要紧的一类：它长得像限流（很多网关就用 429），但重试永远不会
    成功——用户该做的是充值/换 key，而不是等我们白等一分钟再失败。
    """
    name = type(exc).__name__.lower()
    text = str(exc)
    lowered = text.lower()
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status == 402 or any(marker in lowered for marker in QUOTA_MARKERS):
        return "quota_exhausted"
    if any(marker in lowered for marker in TOKEN_LIMIT_MARKERS):
        return "token_limit"
    if status == 429 or "ratelimit" in name or "rate limit" in lowered or "429" in lowered or "请求数限制" in text:
        return "rate_limit"
    if status in TRANSIENT_STATUSES or any(marker in lowered for marker in TRANSIENT_MARKERS):
        return "transient"
    return "other"


def _is_rate_limit_error(exc: Exception) -> bool:
    return classify_llm_error(exc) in {"rate_limit", "token_limit"}


# ---- 响应头：权威信号 ------------------------------------------------------
def _headers_from_response(response: Any) -> Any | None:
    """成功响应上的响应头（流式对象把 httpx.Response 藏在 completion_stream 里）。"""
    for candidate in (
        getattr(getattr(response, "completion_stream", None), "response", None),
        getattr(response, "response", None),
        getattr(response, "_response", None),
        response,
    ):
        headers = getattr(candidate, "headers", None)
        if headers:
            return headers
    return None


def _headers_from_exception(exc: Exception) -> Any | None:
    """异常上的响应头：litellm 放在 `litellm_response_headers`（实测）。"""
    for attr in ("litellm_response_headers", "headers"):
        headers = getattr(exc, attr, None)
        if headers:
            return headers
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    return headers or None


def _interval_from_limit_header(raw: str) -> float | None:
    """`10, 10;w=60` → 窗口 60s / 上限 10 次 = 6s。"""
    head = str(raw).split(",")[0].strip()
    try:
        limit = float(head)
    except ValueError:
        return None
    window = TOKEN_WINDOW_SECONDS
    match = re.search(r"w\s*=\s*(\d+)", str(raw))
    if match:
        window = float(match.group(1))
    if limit <= 0 or window <= 0:
        return None
    return window / limit


def _tokens_per_minute_from_header(raw: str) -> float | None:
    head = str(raw).split(",")[0].strip()
    try:
        limit = float(head)
    except ValueError:
        return None
    window = TOKEN_WINDOW_SECONDS
    match = re.search(r"w\s*=\s*(\d+)", str(raw))
    if match:
        window = float(match.group(1))
    if limit <= 0 or window <= 0:
        return None
    return limit * (60.0 / window)


def _parse_limit_headers(headers: Any) -> dict[str, float]:
    def _get(name: str) -> str | None:
        try:
            value = headers.get(name) if headers is not None else None
        except Exception:  # noqa: BLE001
            return None
        return str(value) if value else None

    found: dict[str, float] = {}
    retry_after = _get("retry-after")
    if retry_after:
        try:
            found["retry_after"] = max(0.2, min(float(retry_after.rstrip("s")), 120.0))
        except ValueError:
            pass
    limit_requests = _get("x-ratelimit-limit-requests")
    if limit_requests:
        interval = _interval_from_limit_header(limit_requests)
        if interval:
            found["request_interval"] = interval
    limit_tokens = _get("x-ratelimit-limit-tokens")
    if limit_tokens:
        tpm = _tokens_per_minute_from_header(limit_tokens)
        if tpm:
            found["tokens_per_minute"] = tpm
    remaining = _get("x-ratelimit-remaining-requests")
    if remaining:
        try:
            found["remaining_requests"] = float(str(remaining).split(",")[0])
        except ValueError:
            pass
    return found


def _pace_path() -> Path:
    return settings.data_dir / PACE_FILE_NAME


def _load_pace_cache() -> None:
    global _PACE_LOADED, _PACE_CACHE
    if _PACE_LOADED:
        return
    _PACE_LOADED = True
    try:
        raw = json.loads(_pace_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(raw, dict):
        _PACE_CACHE = {str(k): v for k, v in raw.items() if isinstance(v, dict)}


def _persist_pace() -> None:
    """把学到的节奏写盘（**只存节奏，不存 key/token**）。失败不影响运行。"""
    try:
        path = _pace_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        trimmed = dict(list(_PACE_CACHE.items())[-50:])
        path.write_text(json.dumps(trimmed, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def apply_pace_for_key(key: str) -> None:
    """用过的端点：把上次学到的节奏先装上（重启后不用重新撞一次）。"""
    global _LLM_PACE_SOURCE, _LLM_TOKEN_SOURCE
    _load_pace_cache()
    saved = _PACE_CACHE.get(key)
    if not saved:
        return
    interval = float(saved.get("request_interval") or 0)
    tpm = float(saved.get("tokens_per_minute") or 0)
    if interval > 0:
        _learn_interval(interval, source="learned")
    if tpm > 0:
        _learn_token_limit(tpm, source="learned")


def _learn_interval(interval: float, *, source: str, key: str | None = None) -> None:
    global _LLM_LEARNED_INTERVAL_SECONDS, _LLM_PACE_SOURCE
    if interval <= 0:
        return
    _LLM_LEARNED_INTERVAL_SECONDS = max(_LLM_LEARNED_INTERVAL_SECONDS, interval)
    if _LLM_PACE_SOURCE != "config":
        _LLM_PACE_SOURCE = source
    if key:
        _load_pace_cache()
        entry = _PACE_CACHE.setdefault(key, {})
        entry["request_interval"] = max(float(entry.get("request_interval") or 0), interval)
        _persist_pace()


def _learn_token_limit(tpm: float, *, source: str, key: str | None = None) -> None:
    global _LLM_LEARNED_TOKEN_LIMIT, _LLM_TOKEN_SOURCE
    if tpm <= 0:
        return
    _LLM_LEARNED_TOKEN_LIMIT = tpm
    _LLM_TOKEN_SOURCE = source
    if key:
        _load_pace_cache()
        entry = _PACE_CACHE.setdefault(key, {})
        entry["tokens_per_minute"] = tpm
        _persist_pace()


def _learn_from_headers(headers: Any, *, key: str | None = None) -> dict[str, float]:
    found = _parse_limit_headers(headers)
    if "request_interval" in found:
        _learn_interval(found["request_interval"], source="header", key=key)
    if "tokens_per_minute" in found:
        _learn_token_limit(found["tokens_per_minute"], source="header", key=key)
    return found


def _rate_limit_interval_from_message(text: str) -> float | None:
    """从网关话术里读出限额（中英文都认），换算成"两次请求最小间隔"。

    实测话术：「您已达到总请求数限制：1分钟内最多请求10次，包括失败次数，请检查您的请求是否正确」
    → 60s / 10 次 = 6s。读不出来就返回 None（那就用兜底阶梯）。
    """
    window: float | None = None
    match = re.search(r"(\d+)\s*(?:分钟|minutes?|min\b)", text, re.IGNORECASE)
    if match:
        window = float(match.group(1)) * 60
    else:
        match = re.search(r"(\d+)\s*(?:秒|seconds?|sec\b)", text, re.IGNORECASE)
        if match:
            window = float(match.group(1))
        elif re.search(r"(?:分钟|minutes?|per\s+min)", text, re.IGNORECASE):
            window = 60.0        # 只说了"每分钟"，没说窗口长度 → 按一分钟算
        elif re.search(r"(?:per\s+sec|每秒|/\s*s\b)", text, re.IGNORECASE):
            window = 1.0
    limit_match = re.search(r"(\d+)\s*(?:次|requests?|reqs?\b)", text, re.IGNORECASE)
    if not window or not limit_match:
        return None
    limit = int(limit_match.group(1))
    if limit <= 0:
        return None
    return window / limit


def _token_limit_from_message(text: str) -> float | None:
    """从话术里读 token/分钟 限额。

    实测话术：`Rate limit reached for 200000 tokens per minute (TPM)` → 200000。
    也认「每分钟 200000 tokens」「token 上限 200000/分钟」这类语序。
    """
    patterns = (
        r"(\d[\d,]*)\s*tokens?\s*(?:per|/)\s*(?:min|minute)",
        r"(?:per|/)\s*(?:min|minute)[^\d]{0,12}(\d[\d,]*)\s*tokens?",
        r"每分钟[^\d]{0,8}(\d[\d,]*)\s*(?:个)?\s*tokens?",
        r"(\d[\d,]*)\s*(?:个)?\s*tokens?\s*(?:每|/)\s*分钟",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            try:
                value = float(match.group(1).replace(",", ""))
            except ValueError:
                continue
            if value > 0:
                return value
    return None


def _retry_after_seconds(exc: Exception) -> float | None:
    """优先问响应头（retry-after 最准），拿不到再返回 None。"""
    found = _parse_limit_headers(_headers_from_exception(exc))
    return found.get("retry_after")


def _clamp_delay(delay: float) -> float:
    return max(0.2, min(float(delay), FALLBACK_MAX_DELAY))


def _retry_delay(exc: Exception, rounds: int, kind: str, *, key: str | None = None) -> float:
    """这次该等多久：retry-after > 响应头声明的限额 > 网关话术 > 窗口感知兜底阶梯。"""
    header_delay = _retry_after_seconds(exc)
    parsed = _rate_limit_interval_from_message(str(exc))
    if parsed:
        _learn_interval(parsed, source="prose", key=key)
    if header_delay:
        delay = _clamp_delay(header_delay)
    elif parsed:
        delay = _clamp_delay(parsed)
    else:
        delay = _clamp_delay(FALLBACK_FIRST_DELAY * (2 ** rounds))   # 10/20/40/80…
    if kind == "token_limit":
        tpm = _token_limit_from_message(str(exc))
        if tpm:
            _learn_token_limit(tpm, source="prose", key=key)
        delay = max(delay, 30.0)          # token 窗口通常要等一个窗口才恢复
    if kind == "transient" and not header_delay:
        # 网络抖动/网关重启：从 5s 起指数退避（比限流的 10s 起更快，因为通常很快恢复）
        delay = _clamp_delay(5.0 * (2 ** rounds))
    _RATE_LIMIT_HINTS.append(delay)
    return delay


def _effective_llm_interval() -> float:
    """生效的"两次请求最小间隔"：配置与学到值取大。"""
    configured = 0.0
    if settings.llm_max_requests_per_minute > 0:
        configured = 60.0 / settings.llm_max_requests_per_minute
        global _LLM_PACE_SOURCE
        if _LLM_PACE_SOURCE == "none":
            _LLM_PACE_SOURCE = "config"
    return max(configured, _LLM_LEARNED_INTERVAL_SECONDS)


def _effective_token_limit() -> float:
    return _LLM_LEARNED_TOKEN_LIMIT


def llm_pacing_status() -> dict[str, Any]:
    """给 /api/health 与自检看的"当前节奏 + 来源"。"""
    interval = _effective_llm_interval()
    return {
        "min_interval_seconds": round(interval, 2),
        "requests_per_minute": round(60.0 / interval, 1) if interval > 0 else 0,
        "token_limit_per_minute": int(_effective_token_limit()),
        "source": _LLM_PACE_SOURCE,
        "token_source": _LLM_TOKEN_SOURCE,
        "configured_requests_per_minute": settings.llm_max_requests_per_minute,
        "max_retries": _llm_retry_limit(),
        "total_wait_budget_seconds": RETRY_TOTAL_WAIT_SECONDS,
    }


def _tokens_in_window(now: float) -> int:
    while _LLM_TOKEN_WINDOW and now - _LLM_TOKEN_WINDOW[0][0] > TOKEN_WINDOW_SECONDS:
        _LLM_TOKEN_WINDOW.popleft()
    return sum(tokens for _, tokens in _LLM_TOKEN_WINDOW)


def _token_wait_seconds(tokens: int, tpm: float, now: float) -> float:
    """按 token 限额还要等多久（TPM 限流靠这个，而不是靠拉长请求间隔）。"""
    if tokens <= 0 or tpm <= 0:
        return 0.0
    used = _tokens_in_window(now)
    if used + tokens <= tpm:
        return 0.0
    if not _LLM_TOKEN_WINDOW:
        return 0.0
    oldest = _LLM_TOKEN_WINDOW[0][0]
    return max(0.0, TOKEN_WINDOW_SECONDS - (now - oldest)) + 0.1


async def _await_llm_slot(tokens: int = 0, on_wait: Any = None) -> None:
    """客户端节流：请求间隔 + token 限额。等得久（≥10s）就告诉用户，避免像卡死。"""
    global _LLM_LAST_CALL_AT
    interval = _effective_llm_interval()
    tpm = _effective_token_limit()
    async with _LLM_PACE_LOCK:
        now = time.monotonic()
        if interval > 0:
            wait = interval - (now - _LLM_LAST_CALL_AT)
            if wait > 0:
                await _sleep_and_notice(wait, "requests", on_wait)
        wait = _token_wait_seconds(tokens, tpm, time.monotonic())
        if wait > 0:
            await _sleep_and_notice(wait, "tokens", on_wait)
        _LLM_LAST_CALL_AT = time.monotonic()
        if tokens > 0 and tpm > 0:
            _LLM_TOKEN_WINDOW.append((time.monotonic(), tokens))


async def _sleep_and_notice(seconds: float, reason: str, on_wait: Any) -> None:
    if on_wait is not None and seconds >= NOTICE_WAIT_SECONDS:
        try:
            await on_wait(reason, seconds)
        except Exception:  # noqa: BLE001 —— 报告进度不许弄死请求
            pass
    await asyncio.sleep(seconds)


def _llm_retry_limit() -> int:
    return max(0, int(settings.llm_max_retries))


async def _completion_with_retries(
    kwargs: dict[str, Any],
    on_retry: Any,
    *,
    pace_key: str | None = None,
    tokens: int = 0,
    retries: int | None = None,
) -> Any:
    """发一次 LLM 请求：节流 + 按类重试（额度用尽不重试）+ stream_options 兼容回退。"""
    max_rounds = _llm_retry_limit() if retries is None else max(0, int(retries))

    async def _notify_wait(reason: str, seconds: float) -> None:
        if on_retry is not None:
            await on_retry(0, seconds, f"按端点限额等待（{reason}）", reason)

    waited = 0.0
    rounds = 0
    while True:
        await _await_llm_slot(tokens, _notify_wait)
        try:
            response = await litellm.acompletion(**kwargs)
        except Exception as exc:  # noqa: BLE001
            kind = classify_llm_error(exc)
            if kind == "quota_exhausted":
                raise                     # 重试永远不会成功：直接让上层说清
            if kind in {"rate_limit", "token_limit", "transient"}:
                # 失败响应上也可能带限流头（最权威），先学再决定等多久
                _learn_from_headers(_headers_from_exception(exc), key=pace_key)
                # TPM 只重试一次：token 窗口不是"等几秒"能恢复的，反复空等只会让用户干瞪眼
                # （正确的出路是减少上下文或换 key，文案里已经写了）
                allowed = 1 if kind == "token_limit" else max_rounds
                if rounds >= allowed or waited >= RETRY_TOTAL_WAIT_SECONDS:
                    raise
                delay = _retry_delay(exc, rounds, kind, key=pace_key)
                rounds += 1
                waited += delay
                if on_retry is not None:
                    try:
                        await on_retry(rounds, delay, str(exc)[:300], kind)
                    except Exception:  # noqa: BLE001 —— 报告进度不许弄死请求
                        pass
                await asyncio.sleep(delay)
                continue
            lowered = str(exc).lower()
            if "stream_options" in lowered or "include_usage" in lowered or "unrecognized" in lowered:
                kwargs.pop("stream_options", None)
                continue
            raise
        _learn_from_headers(_headers_from_response(response), key=pace_key)
        return response


async def stream_turn(
    cfg: ProviderConfig,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    *,
    on_text: Callable[[str], Awaitable[None]] | None = None,
    on_retry: Callable[[int, float, str, str], Awaitable[None]] | None = None,
    retries: int | None = None,
    timeout: int | None = None,
    temperature: float = 0.2,
) -> TurnResult:
    """跑一个 LLM turn，把流式增量通过 on_text 回调出去（用于 SSE）。

    timeout 缺省取 PAPERLENS_PER_TURN_TIMEOUT_SECONDS（§8：单次调用不许卡死整个 run）；
    自检场景会显式传更短的值。

    限流（429）在这里统一处理：先按客户端节奏等一个空位，撞到限流就退避重试，
    并通过 on_retry 让上层把它显示给用户（见 _await_llm_slot / _retry_delay）。
    """
    if timeout is None:
        timeout = settings.per_turn_timeout_seconds
    headers = _llm_headers()
    kwargs: dict[str, Any] = {
        "model": cfg.litellm_model(),
        "messages": messages,
        "temperature": temperature,
        "stream": True,
        "timeout": timeout,
    }
    if headers:
        kwargs["extra_headers"] = headers
    if cfg.api_key:
        kwargs["api_key"] = cfg.api_key
    if cfg.base_url:
        kwargs["api_base"] = cfg.base_url
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"

    # 尽量让端点把真实用量报回来（§8 的"诚实记账"）。
    # 有些端点不认识这个字段，所以失败时退一步重试一次，而不是让它把整个请求搞挂。
    kwargs["stream_options"] = {"include_usage": True}

    # 关掉 SDK 自己的隐式重试：实测 openai SDK 默认会对 429 静默重试（max_retries=2），
    # 于是"撞了限流"这件事在时间线上完全看不见（用户只看到卡住），
    # 而且它不知道我们后来学到的端点节奏。重试统一由 _completion_with_retries 负责：
    # 会读 retry-after、会限速、会发 llm_retry 事件、也会在放弃时如实报错。
    kwargs["num_retries"] = 0

    acc = _Accumulator()
    started = time.monotonic()
    # 端点身份（base_url + model）：学到的节奏按它持久化（**不含 api_key**）
    pace_key = f"{cfg.base_url or 'default'}|{cfg.model}"
    apply_pace_for_key(pace_key)
    tokens = estimate_tokens(messages, tools)

    # 流到一半断开也要能重来（用户实测：第 10 轮时 Connection error. 直接作废整轮）。
    # 但只在「还没收到任何内容」时重试：已经吐给前端的文字收不回来，重来会让时间线上
    # 出现两份重复的中间过程；那种情况如实失败，靠「失败也交付」保住已确认的结论。
    mid_rounds = 0
    while True:
        try:
            response = await _completion_with_retries(
                kwargs, on_retry, pace_key=pace_key, tokens=tokens, retries=retries
            )
            async for chunk in response:
                new_text = acc.absorb(chunk)
                if new_text and on_text is not None:
                    await on_text(new_text)
        except Exception as exc:  # noqa: BLE001
            limit = _llm_retry_limit() if retries is None else max(0, int(retries))
            if mid_rounds >= limit or classify_llm_error(exc) != "transient" or acc.deltas_seen > 0:
                raise
            delay = _retry_delay(exc, mid_rounds, "transient", key=pace_key)
            mid_rounds += 1
            if on_retry is not None:
                try:
                    await on_retry(
                        mid_rounds,
                        delay,
                        f"{str(exc)[:200]}（连接中断，本轮还没收到内容，重试中）",
                        "transient",
                    )
                except Exception:  # noqa: BLE001
                    pass
            await asyncio.sleep(delay)
            continue
        return acc.to_result(int((time.monotonic() - started) * 1000))


# ---------------------------------------------------------------------------
# §4 启动自检
# ---------------------------------------------------------------------------
PING_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "ping",
        "description": "测试工具。返回 pong 和传入的 page 值。",
        "parameters": {
            "type": "object",
            "properties": {"page": {"type": "integer", "description": "页码"}},
            "required": ["page"],
            "additionalProperties": False,
        },
    },
}


class SmokeStep(BaseModel):
    turn: int
    text: str = ""
    tool_calls: list[dict[str, Any]] = []
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    latency_ms: int = 0


class SmokeResult(BaseModel):
    ok: bool
    diagnosis: str
    capabilities: dict[str, Any] = {}
    steps: list[SmokeStep] = []
    provider: str = ""
    # 失败时顺手探测一下端点，把"网关坏了"和"地址写错了"分开
    diagnostics: dict[str, Any] = {}


async def smoke_test(cfg: ProviderConfig, timeout: int = 90) -> SmokeResult:
    """两轮工具调用冒烟测试（§4）。

    第 1 轮：要求模型调用 ping —— 测的是"支不支持 function calling"和"参数能不能解析"。
    第 2 轮：把工具结果回给模型，看它能不能基于结果说话。
    """
    steps: list[SmokeStep] = []
    capabilities: dict[str, Any] = {}

    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": "你必须使用提供的工具来回答问题，不要凭空回答。",
        },
        {"role": "user", "content": "请调用 ping 工具，page 参数填 1。"},
    ]

    # ---- 第 1 轮 ----
    try:
        first = await stream_turn(cfg, messages, [PING_TOOL], timeout=timeout, retries=1)
    except Exception as exc:  # noqa: BLE001 —— 这里就是要兜住所有异常并翻译成人话
        return SmokeResult(
            ok=False,
            diagnosis=_diagnose_exception(exc),
            provider=cfg.safe_label(),
            diagnostics=await probe_endpoint(cfg),
        )

    capabilities["streaming"] = first.deltas_seen > 0
    capabilities["finish_reason"] = first.finish_reason is not None
    capabilities["usage_in_stream"] = first.usage is not None

    steps.append(
        SmokeStep(
            turn=1,
            text=first.text,
            tool_calls=[
                {"name": tc.name, "arguments": tc.arguments(), "arguments_raw": tc.arguments_raw}
                for tc in first.tool_calls
            ],
            finish_reason=first.finish_reason,
            usage=first.usage,
            latency_ms=first.latency_ms,
        )
    )

    if not first.tool_calls:
        capabilities["tool_calling"] = False
        return SmokeResult(
            ok=False,
            diagnosis=(
                "该端点没有返回工具调用（tool_calls）。本项目完全依赖 function calling，"
                "无法在这样的模型上工作 —— 请更换支持工具调用的模型。"
            ),
            capabilities=capabilities,
            steps=steps,
            provider=cfg.safe_label(),
        )

    call = first.tool_calls[0]
    args = call.arguments()
    if call.name != "ping" or not isinstance(args.get("page"), int):
        capabilities["tool_calling"] = True
        return SmokeResult(
            ok=False,
            diagnosis=(
                f"工具调用参数不可靠：期望调用 ping(page=int)，实际得到 "
                f"{call.name}({call.arguments_raw!r})。该模型的工具参数生成不可靠，建议更换模型。"
            ),
            capabilities=capabilities,
            steps=steps,
            provider=cfg.safe_label(),
        )
    capabilities["tool_calling"] = True

    # ---- 第 2 轮 ----
    messages.append(first.assistant_message())
    messages.append(
        {
            "role": "tool",
            "tool_call_id": call.id,
            "name": "ping",
            "content": json.dumps({"pong": True, "page": args["page"]}, ensure_ascii=False),
        }
    )
    try:
        second = await stream_turn(cfg, messages, [PING_TOOL], timeout=timeout, retries=1)
    except Exception as exc:  # noqa: BLE001
        return SmokeResult(
            ok=False,
            diagnosis=f"第 1 轮通过，但第 2 轮失败：{_diagnose_exception(exc)}",
            capabilities=capabilities,
            steps=steps,
            provider=cfg.safe_label(),
        )

    steps.append(
        SmokeStep(
            turn=2,
            text=second.text,
            tool_calls=[{"name": tc.name, "arguments": tc.arguments()} for tc in second.tool_calls],
            finish_reason=second.finish_reason,
            usage=second.usage,
            latency_ms=second.latency_ms,
        )
    )

    if not second.text.strip():
        return SmokeResult(
            ok=False,
            diagnosis="第 2 轮没有返回任何文本（可能无限循环调用工具，或流式响应不完整）。",
            capabilities=capabilities,
            steps=steps,
            provider=cfg.safe_label(),
        )

    # 自检通过时顺手把"这个端点的限额/节奏"报出来：用户**在跑之前**就知道会不会撞上限流
    pacing = llm_pacing_status()
    capabilities["llm_pacing"] = pacing
    diagnosis = "通过：支持流式输出 + function calling + 工具结果回传。"
    if pacing["min_interval_seconds"] > 0:
        source_cn = {
            "config": "来自你的配置",
            "header": "来自端点响应头",
            "prose": "来自端点的错误话术",
            "learned": "来自上次学到的节奏",
        }.get(str(pacing["source"]), str(pacing["source"]))
        diagnosis += (
            f"另外：端点限额已识别（{source_cn}）——每 {pacing['min_interval_seconds']}s 一次调用"
            f"（约 {pacing['requests_per_minute']} 次/分钟），本轮会按这个节奏发请求。"
        )
        if pacing["token_limit_per_minute"]:
            diagnosis += f"token 限额约 {pacing['token_limit_per_minute']}/分钟。"
    return SmokeResult(
        ok=True,
        diagnosis=diagnosis,
        capabilities=capabilities,
        steps=steps,
        provider=cfg.safe_label(),
    )


_HTML_MARK = re.compile(r"<\s*(!doctype|html|head|body)", re.IGNORECASE)


def _clean(text: str, limit: int = 200) -> str:
    """把原始异常信息压成一行短文本。"""
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _html_diagnosis(text: str, status: int | None) -> str:
    """对方返回的是 HTML 错误页——那说明请求根本没到模型那儿。

    这种情况非常常见（第三方网关、Cloudflare、nginx 报 5xx/403 时会甩一个 HTML 页面回来），
    而把整页 HTML 原样丢给用户既看不懂也没用，所以这里要把线索捞出来说人话。
    """
    lowered = text.lower()
    clues: list[str] = []
    if status:
        clues.append(f"HTTP {status}")
    if "cloudflare" in lowered:
        clues.append("中间挡着 Cloudflare")
    if "nginx" in lowered:
        clues.append("中间挡着 nginx")
    if "bad gateway" in lowered:
        clues.append("网关报 Bad Gateway")
    if "gateway time-out" in lowered or "gateway timeout" in lowered:
        clues.append("网关超时")
    if "access denied" in lowered or "forbidden" in lowered:
        clues.append("访问被拒绝")
    if "not found" in lowered:
        clues.append("路径不存在")
    detail = f"（{'，'.join(clues)}）" if clues else ""
    return (
        f"对方返回的是 HTML 错误页{detail}，不是模型的 JSON 响应 —— "
        "也就是说请求没到模型那里。常见原因：① base_url 写错了路径（多数服务要带 /v1）；"
        "② 网关/代理在报错（5xx、被 Cloudflare 挡、额度或鉴权在网关这一层就没过）；"
        "③ 这个地址其实不是 OpenAI 兼容接口。"
        "补充：个别套 Cloudflare 的网关会把 Python SDK 的 User-Agent 当 bot 拦掉——"
        "试试设置 PAPERLENS_HTTP_USER_AGENT=浏览器UA 或其他标识，再跑一次自检。"
        "下面有探测结果，可以看清它到底回了什么。"
    )


def _diagnose_exception(exc: Exception) -> str:
    """把一堆 SDK 异常翻译成用户能看懂的一句话。"""
    name = type(exc).__name__
    text = str(exc)
    lowered = text.lower()
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)

    # HTML 错误页优先判断：它最容易被当成"未知错误"而把整页 HTML 甩给用户
    if _HTML_MARK.search(text[:2000]):
        return _html_diagnosis(text, status if isinstance(status, int) else None)

    if "authentication" in name.lower() or "api key" in lowered or "401" in lowered:
        return "认证失败：api_key 不对，或者它不属于这个 base_url 对应的服务商。"
    if "notfound" in name.lower() or "404" in lowered:
        return "端点或模型名不存在：检查 base_url 是否要带 /v1，以及模型名是否拼写正确。"
    # 超时判断要在 connection 之前：很多 SDK 把超时包装成连接类异常，
    # 而"连了但对方不说话"和"根本没连上"对用户的含义不一样。
    if "timed out" in lowered or "timeout" in lowered or isinstance(exc, asyncio.TimeoutError):
        return "超时：端点在该时间内没有任何响应。"
    kind = classify_llm_error(exc)

    # 顺序很重要：transient 必须排在 connection 之前。
    # 实测踩过：MidStreamFallbackError / InternalServerError 的消息里也带 "connection"，
    # 结果被下面那条"连不上 base_url"抢走，用户看到的解释与处置建议都是错的。
    if kind == "transient":
        if any(
            marker in lowered
            for marker in (
                "connection refused", "name or service not known", "nodename nor servname",
                "getaddrinfo", "拒绝连接", "名称解析",
            )
        ):
            # 这一类更像"地址/端口不对，或服务没在跑"，而不是网络抖动——建议完全不同
            return (
                "连不上 base_url：地址或端口不对，或者那个服务根本没在跑。"
                "检查 base_url 是否要带 /v1、端口是否正确、服务是否已启动。"
                f"原始信息：{_clean(text)[:160]}"
            )
        if "midstream" in name.lower() or "stream" in name.lower():
            return (
                "流到一半连接断了（不是限流/额度问题）：已经收到的内容**不能当作结论**，"
                "所以这一轮如实失败——但已定位并核验过的结论照常交付，重跑一次通常会跑完。"
                f"原始信息：{_clean(text)[:160]}"
            )
        return (
            "连接中断或网关 5xx（不是限流，也不是额度问题）：常见原因是网络抖动、代理不稳定，"
            "或中转站自己重启/过载。**已经自动重试过几次**；仍然失败就重跑一次，"
            "反复出现就检查本机代理或换端点。已确认的结论不会丢（失败也会交付）。"
            f"原始信息：{_clean(text)[:160]}"
        )
    if "connection" in name.lower() or "connect" in lowered:
        return "连不上 base_url：检查地址、网络，以及该服务是否允许从这里访问。"
    if kind == "quota_exhausted":
        return (
            "账号额度/余额用尽（不是限流，**重试无用**）：去充值、换 key 或换端点。"
            f"已跑出来的部分不会丢（失败也会交付已核验的结论）。原始信息：{_clean(text)[:160]}"
        )
    if kind == "token_limit":
        return (
            "被 token 速率限制（TPM）：这种限制卡的是 token 而不是请求次数，所以「拉长请求间隔」"
            "帮助有限。可行的办法：① 减少每次调用的上下文（少勾几条目标、少让模型一次读很多页）；"
            "② 换一个 TPM 更高的 key/端点。已跑出来的部分不会丢。"
            f"原始信息：{_clean(text)[:160]}"
        )
    if kind == "rate_limit":
        interval = _effective_llm_interval()
        clause = (
            f"当前节奏：每 {interval:.1f}s 一次调用（来源 {_LLM_PACE_SOURCE}）。"
            if interval > 0
            else "端点没有声明限额，也没给出可解析的话术，只能靠退避。"
        )
        return (
            f"被限流（429）：一次定位/侦察要几十次调用，很容易撞上限。{clause}"
            "办法：① 设 PAPERLENS_LLM_MAX_REQUESTS_PER_MINUTE=<你的限额>（例如 10）；"
            "② 换限额更宽的端点。已跑出来的部分不会丢（失败也会交付已核验的结论）。"
            f"原始信息：{_clean(text)[:160]}"
        )
    if "badrequest" in name.lower() or "400" in lowered:
        return f"请求被拒绝（400）：通常是该端点不支持本次请求里的某个字段。原始信息：{_clean(text)}"
    return f"{name}：{_clean(text)}"


def _probe_urls(base: str) -> list[str]:
    """探测地址列表（纯函数，m0_check 直接对它断言）。

    {base}/models 一定测；base 没带 /v1 时补一个 {base}/v1/models
    ——"漏了 /v1"是最常见的 base_url 手误。用 dict.fromkeys 去重：
    base 已经以 /v1 结尾时两个候选其实是同一个 URL，之前会把重复的探测结果甩给用户。
    """
    base = base.rstrip("/")
    candidates = [f"{base}/models"]
    if not base.endswith("/v1"):
        candidates.append(f"{base}/v1/models")
    return list(dict.fromkeys(candidates))


async def probe_endpoint(cfg: ProviderConfig, timeout: int = 12) -> dict[str, Any]:
    """直接去敲对方的 /models，看它到底回什么。

    这一步是为了把"网关坏了"和"base_url 写错了"区分开——光看报错分不出来，
    但直接发一个最简单的 GET 就能看出来：404 → 路径不对；401 → key 不对；
    5xx/HTML → 网关或代理在报错；200 → 端点活着，问题在别处。
    """
    import httpx

    base = (cfg.base_url or "").rstrip("/")
    if not base:
        return {"probes": [], "note": "没填 base_url（官方端点的话这里探测不了）"}

    probes: list[dict[str, Any]] = []
    headers = {"Authorization": f"Bearer {cfg.api_key}"}
    if settings.http_user_agent:
        headers["User-Agent"] = settings.http_user_agent  # 与 LLM 请求同一个客户端标识
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for url in _probe_urls(base):
            entry: dict[str, Any] = {"url": url}
            try:
                response = await client.get(url, headers=headers)
                _learn_from_headers(getattr(response, "headers", None))
                body = response.text or ""
                entry["status"] = response.status_code
                entry["content_type"] = response.headers.get("content-type", "")
                entry["snippet"] = _clean(body, 160)
                entry["is_html"] = bool(_HTML_MARK.search(body[:2000]))
            except Exception as exc:  # noqa: BLE001
                entry["status"] = None
                entry["error"] = f"{type(exc).__name__}: {_clean(str(exc), 120)}"
            probes.append(entry)
    return {"probes": probes}


__all__ = [
    "ProviderConfig",
    "ToolCall",
    "TurnResult",
    "SmokeResult",
    "SmokeStep",
    "PING_TOOL",
    "stream_turn",
    "smoke_test",
    "estimate_tokens",
    "probe_endpoint",
    "_probe_urls",
    "litellm",
]
