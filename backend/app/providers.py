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


async def stream_turn(
    cfg: ProviderConfig,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    *,
    on_text: Callable[[str], Awaitable[None]] | None = None,
    timeout: int | None = None,
    temperature: float = 0.2,
) -> TurnResult:
    """跑一个 LLM turn，把流式增量通过 on_text 回调出去（用于 SSE）。

    timeout 缺省取 PAPERLENS_PER_TURN_TIMEOUT_SECONDS（§8：单次调用不许卡死整个 run）；
    自检场景会显式传更短的值。
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

    acc = _Accumulator()
    started = time.monotonic()
    try:
        response = await litellm.acompletion(**kwargs)
    except Exception as exc:  # noqa: BLE001
        lowered = str(exc).lower()
        if "stream_options" in lowered or "include_usage" in lowered or "unrecognized" in lowered:
            kwargs.pop("stream_options", None)
            response = await litellm.acompletion(**kwargs)
        else:
            raise
    async for chunk in response:
        new_text = acc.absorb(chunk)
        if new_text and on_text is not None:
            await on_text(new_text)
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
        first = await stream_turn(cfg, messages, [PING_TOOL], timeout=timeout)
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
        second = await stream_turn(cfg, messages, [PING_TOOL], timeout=timeout)
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

    return SmokeResult(
        ok=True,
        diagnosis="通过：支持流式输出 + function calling + 工具结果回传。",
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
    if "connection" in name.lower() or "connect" in lowered:
        return "连不上 base_url：检查地址、网络，以及该服务是否允许从这里访问。"
    if "ratelimit" in name.lower() or "429" in lowered:
        return "被限流（429）：稍后重试，或换一个 key。"
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
