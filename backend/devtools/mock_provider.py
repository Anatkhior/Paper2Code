"""本地 mock provider —— 不花一分钱就能验证整条链路。

它模仿一个 OpenAI 兼容端点（POST /v1/chat/completions），并且**故意把工具调用参数
切成多个 chunk**，因为真实端点就是这么干的，而「跨 chunk 拼接参数」是后端最容易写错的地方。

用模型名来选择行为：

| 模型名 | 行为 |
|---|---|
| `mock-model`        | 侦察：list_pages → get_page_text(3) → record_plan；定位：repo_tree → search_code → read_file → 逐条 record_finding → finish |
| `bad-plan`          | 同上，但第一次 record_plan 故意写错页码 → 验证引文核验会把模型打回 |
| `bad-evidence`      | 定位时第一次引用一个不存在的文件 → 验证代码引用核验会把模型打回 |
| `thin-explanation`  | 定位时第一次只给一句没有信息量的解释 → 验证解释质量门槛会打回 |
| `stuck-model`       | 一直重复同一个失败调用 → 验证后端会提前停止而不是空转到上限 |
| `no-tools`          | 不返回工具调用，只返回文本 → 自检应判定「不支持 function calling」 |
| `bad-args`          | 返回拼错的参数（page 是字符串）→ 自检应判定「工具参数不可靠」 |
| `html-error`        | 返回一个 HTML 错误页（模拟网关/Cloudflare 报错）→ 自检必须说人话，不能把整页 HTML 甩给用户 |

注意：定位阶段**只提交用户在提示里勾选的那些创新点**。如果不管用户勾了什么、按固定剧本
提交全部三条，用户只勾一条时就会被后端一直拒绝，于是原地空转到轮数上限。

启动：
    cd backend && .venv/bin/python -m uvicorn devtools.mock_provider:app --port 8123
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from tests.paper_fixture import INIT_QUOTE, KEY_QUOTE
from tests.repo_fixture import layer_lines, quote_from_lines

app = FastAPI(title="mock openai-compatible provider")

MODEL_NAME = "mock-model"
CHUNK_DELAY = 0.03  # 故意放慢，这样「边跑边推」和「跑完一次性返回」能被测试区分开

_FORWARD = layer_lines("    def forward(self, x):")
_RESET = layer_lines("    def reset_parameters(self):")


# ---------------------------------------------------------------------------
# 阶段 A（侦察）的剧本
# ---------------------------------------------------------------------------
RECON_PLAN: dict[str, Any] = {
    "paper_summary": (
        "这篇合成论文提出用低秩分解表示权重更新，并配套一个让初始更新为零的初始化方案，"
        "从而在几乎不损失精度的前提下大幅减少可训练参数量。"
    ),
    "coverage_note": "读了第 1、3、4 页的方法与摘要部分，实验表格没有细读。",
    "innovations": [
        {
            "id": "inn-1",
            "name": "低秩重参数化",
            "one_liner": "把权重更新 dW 写成两个小矩阵的乘积 B·A，只训练 A 和 B。",
            "difficulty": "beginner",
            "paper_evidence": [{"page": 3, "quote": KEY_QUOTE, "kind": "text"}],
            "search_hints": ["LowRank", "lora_A", "lora_B", "rank", "reparameterization"],
        },
        {
            "id": "inn-2",
            "name": "零初始化与 alpha/r 缩放",
            "one_liner": "A 用高斯初始化、B 置零，使训练开始时更新为零；前向按 alpha/r 缩放。",
            "difficulty": "medium",
            "paper_evidence": [{"page": 4, "quote": INIT_QUOTE, "kind": "text"}],
            "search_hints": ["reset_parameters", "lora_alpha", "scaling", "zeros_"],
        },
        {
            "id": "inn-3",
            "name": "冻结主干只训练低秩分支",
            "one_liner": "预训练权重保持冻结，梯度只流向新增的低秩矩阵。",
            "difficulty": "beginner",
            "paper_evidence": [
                {
                    "page": 3,
                    "quote": "During training W0 is frozen and only A and B receive gradients.",
                    "kind": "text",
                }
            ],
            "search_hints": ["requires_grad", "freeze", "frozen"],
        },
    ],
}

# 故意把页码写错（引文在第 3 页，它说第 5 页）—— 用来验证后端会把它打回重做
BAD_PLAN: dict[str, Any] = json.loads(json.dumps(RECON_PLAN))
BAD_PLAN["innovations"][0]["paper_evidence"][0]["page"] = 5


# ---------------------------------------------------------------------------
# 阶段 B（定位）的剧本
#
# 解释写得像样一点，是为了让演示能看出界面的完整效果；
# 顺带也是「有信息量的解释」该有的样子。
# ---------------------------------------------------------------------------
FINDINGS: dict[str, dict[str, Any]] = {
    "inn-1": {
        "innovation_id": "inn-1",
        "status": "matched",
        "confidence": 0.9,
        "confidence_reason": (
            "公式 dW = B A 与代码里的 lora_A / lora_B 两个线性层一一对应，缩放系数也对得上；"
            "不确定的是论文没写初始化细节。"
        ),
        "code_evidence": [
            {
                "path": "loralib/layers.py",
                "line_start": _FORWARD[0],
                "line_end": _FORWARD[1],
                "symbol": "Linear.forward",
                "why": "前向里把低秩分支的输出乘上 scaling 后加回主分支，就是论文里的 h = W0x + (alpha/r)BAx",
                "quote": quote_from_lines(*_FORWARD, "result += after_B"),
            }
        ],
        "explanation": {
            "intuition": (
                "想像你要给一个已经训练好的大模型「补课」。最直接的做法是把几亿个参数全部重训一遍，"
                "但那要存下每个参数的梯度，显存和算力都吃不消。低秩分解的观察是：这次补课真正要学的"
                "新东西其实不多，可以只用两个很窄的矩阵 A、B 相乘，来代替那一大块「更新量」——"
                "就像不用整块黑板，只用两条窄纸带就能拼出同样的字。"
            ),
            "math": (
                "W = W_0 + \\Delta W = W_0 + \\frac{\\alpha}{r} B A,\\quad "
                "B\\in\\mathbb{R}^{d\\times r},\\ A\\in\\mathbb{R}^{r\\times k},\\ r \\ll \\min(d,k)"
            ),
            "code_walkthrough": [
                {
                    "line_ref": f"loralib/layers.py:{_FORWARD[0]}",
                    "text": "forward 入口：先老老实实算一遍主干输出 nn.Linear.forward(self, x)，原来的模型该怎么算还怎么算。",
                },
                {
                    "line_ref": f"loralib/layers.py:{_FORWARD[0] + 2}",
                    "text": "只有当 r > 0（说明确实要加低秩分支）且没被 merge 过时，才走下面三行。",
                },
                {
                    "line_ref": f"loralib/layers.py:{_FORWARD[0] + 3}-{_FORWARD[1] - 1}",
                    "text": "x 先过 dropout，再乘 A（把维度压到 r），再乘 B（升回输出维度），最后乘缩放系数 scaling 加回主干——这就是论文里 BAx 那一项。",
                },
            ],
            "pitfalls": [
                "别以为它在给模型「加层」：输入输出的维度完全没变，推理时可以把 BA 直接合并进 W0，速度不受影响。",
                "别以为 r 越大越好：r 增大后收益很快饱和，而参数量和显存是线性涨的。",
            ],
            "read_next": [
                f"loralib/layers.py:{_RESET[0]}-{_RESET[1]}（初始化为什么要置零）",
                "train.py（实际怎么训练）",
            ],
        },
    },
    "inn-2": {
        "innovation_id": "inn-2",
        "status": "matched",
        "confidence": 0.85,
        "confidence_reason": (
            "B 置零这一点在代码里很明确；A 的初始化方式论文只说了高斯，代码用的是 kaiming_uniform_。"
        ),
        "code_evidence": [
            {
                "path": "loralib/layers.py",
                "line_start": _RESET[0],
                "line_end": _RESET[1],
                "symbol": "LoRALayer.reset_parameters",
                "why": "这里把 B 初始化为零，保证训练开始时 B A = 0，等价于原始模型",
                "quote": quote_from_lines(*_RESET, "nn.init.zeros_"),
            }
        ],
        "explanation": {
            "intuition": (
                "新加的分支必须「一开始什么都不做」。如果 A、B 都随机初始化，模型一上来就被这个随机分支带偏，"
                "训练前几步全在纠正它。所以这里让 B 全为零：零乘任何东西都是零，于是训练刚开始时整个更新量"
                "恰好是零，模型行为和原来一模一样，再慢慢学出新东西。"
            ),
            "math": "B = 0 \\Rightarrow \\Delta W = BA = 0 \\Rightarrow h = W_0 x（与原始模型完全一致）",
            "code_walkthrough": [
                {
                    "line_ref": f"loralib/layers.py:{_RESET[0]}",
                    "text": "reset_parameters 是 PyTorch 里「初始化权重」的约定入口，nn.Linear 构造时会自动调用它。",
                },
                {
                    "line_ref": f"loralib/layers.py:{_RESET[0] + 2}",
                    "text": "A 用 kaiming_uniform_：它必须带随机性，否则梯度传不下去（全零的话梯度也是零，永远学不动）。",
                },
                {
                    "line_ref": f"loralib/layers.py:{_RESET[0] + 3}",
                    "text": "B 用 zeros_ 置零：这一步就是「初始更新为零」的全部实现。",
                },
            ],
            "pitfalls": [
                "容易记反：是 A 随机、B 置零，不是反过来——写反了模型一开始就不是原模型。",
                "别把 alpha/r 当成学习率：它是前向计算时的固定缩放，不是优化器的步长。",
            ],
            "read_next": [
                f"loralib/layers.py:{_FORWARD[0]}-{_FORWARD[1]}（scaling 在前向里怎么用）",
                "train.py",
            ],
        },
    },
    # 这个仓库里真的没有「冻结主干」：所以诚实的答案就是 not_found
    "inn-3": {
        "innovation_id": "inn-3",
        "status": "not_found",
        "confidence": 0.7,
        "confidence_reason": (
            "搜了 requires_grad / freeze / frozen 三个模式都没有命中；但仓库很小，也可能是我搜的词不对。"
        ),
        "code_evidence": [],
        "not_found_reason": (
            "仓库里没有把预训练权重设为 requires_grad=False 的逻辑，train.py 直接对全部参数做 AdamW。"
        ),
        "searched": ["requires_grad", "freeze", "frozen", "param.requires_grad", "named_parameters"],
        "explanation": {
            "intuition": (
                "论文反复强调「只训练低秩分支、主干冻结」，但我把这个仓库翻了一遍：train.py 构造优化器时"
                "直接把 model.parameters() 全丢给了 AdamW，没有任何地方把 requires_grad 设成 False。"
                "也就是说这份代码并没有实现论文描述的冻结策略——它更像个演示脚本，而不是论文的完整复现。"
            ),
            "math": "",
            "code_walkthrough": [],
            "pitfalls": [
                "看到论文说「冻结」就以为代码一定实现了——很多复现仓库只实现核心公式，训练策略被简化了。",
                "「论文有、代码没有」恰恰是解读系统最该说清楚的地方，不要糊过去。",
            ],
            "read_next": ["train.py:6-14（build_model 与优化器构造）"],
        },
    },
}

# 故意指向一个不存在的文件 —— 用来验证后端会把编造的引用打回
BAD_FINDING: dict[str, Any] = json.loads(json.dumps(FINDINGS["inn-1"]))
BAD_FINDING["code_evidence"][0]["path"] = "loralib/nonexistent_layer.py"

# 故意写一句没有信息量的解释 —— 用来验证解释质量门槛会打回
THIN_FINDING: dict[str, Any] = json.loads(json.dumps(FINDINGS["inn-1"]))
THIN_FINDING["explanation"] = {"intuition": "它就是做低秩分解的。", "code_walkthrough": [], "pitfalls": []}


def _target_ids(messages: list[dict[str, Any]]) -> list[str]:
    """从用户提示里读出「这次要定位哪几条创新点」。

    这是必须的：假端点如果不管用户勾了什么、按固定剧本提交三条，
    用户只勾一条时就会被后端一直拒绝，于是原地空转到轮数上限。
    """
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        found = re.findall(r'"id"\s*:\s*"([^"]+)"', message.get("content") or "")
        if found:
            # 不按剧本过滤：用户自定义的 user-N 也在清单里，它们同样是"本次要定位的目标"
            return found
    return list(FINDINGS)


def _tool_messages(messages: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [m for m in messages if m.get("role") == "tool" and m.get("name") == name]


def _accepted_findings(messages: list[dict[str, Any]]) -> int:
    count = 0
    for message in _tool_messages(messages, "record_finding"):
        try:
            if json.loads(message.get("content") or "{}").get("accepted"):
                count += 1
        except json.JSONDecodeError:
            continue
    return count


def recon_action(messages: list[dict[str, Any]], model: str) -> tuple[str, dict[str, Any], str]:
    """阶段 A：list_pages → get_page_text(3) → record_plan"""
    last = messages[-1]
    submitted = _tool_messages(messages, "record_plan")
    if not any(m.get("role") == "tool" for m in messages):
        return "list_pages", {}, "call_recon_1"
    if last.get("name") == "list_pages":
        return "get_page_text", {"page": 3}, "call_recon_2"
    if last.get("name") == "get_page_text":
        if "bad-plan" in model and not submitted:
            return "record_plan", BAD_PLAN, "call_recon_3"
        return "record_plan", RECON_PLAN, "call_recon_3"
    if last.get("name") == "record_plan":
        # 能走到这里说明上一次 record_plan 被打回了（成功就会终止）
        return "record_plan", RECON_PLAN, "call_recon_4"
    return "list_pages", {}, "call_recon_1"



# ---------------------------------------------------------------------------
# 用户自定义目标（阶段 C）：假端点也要能处理"不在剧本里的 id"
# ---------------------------------------------------------------------------
def _target_meta(messages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """从用户提示里把整份目标清单解析出来（id → 条目），拿到 source / hints / quote。"""
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content") or ""
        start = content.find("[")
        while start >= 0:
            try:
                data, _ = json.JSONDecoder().raw_decode(content[start:])
            except json.JSONDecodeError:
                data = None
            if isinstance(data, list) and data and isinstance(data[0], dict) and "id" in data[0]:
                return {str(item["id"]): item for item in data}
            start = content.find("[", start + 1)
    return {}


def _is_user_item(item_id: str, meta: dict[str, dict[str, Any]]) -> bool:
    return str(meta.get(item_id, {}).get("source") or "") == "user" or item_id.startswith("user-")


def _search_pattern(item: dict[str, Any]) -> str:
    """用户条目搜什么：先用它自己的线索，没有就退到原句里最长的那个词。"""
    hints = [hint for hint in (item.get("search_hints") or []) if hint and hint.strip()]
    if hints:
        return hints[0]
    words = [word for word in re.findall(r"[A-Za-z][A-Za-z0-9_]{5,}", item.get("paper_evidence", [{}])[0].get("quote", "") if item.get("paper_evidence") else item.get("name") or "")]
    return max(words, key=len) if words else (item.get("name") or "lora")


def _looks_like_code(text: str) -> bool:
    """别把文档字符串里的词当成实现：只认看起来像代码的行。"""
    return any(marker in text for marker in ("def ", "class ", "=", "(", "self."))


def _parse_hits(content: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return []
    return [hit for hit in payload.get("hits", []) if isinstance(hit, dict) and hit.get("path")]


def _user_item_not_found(item_id: str, pattern: str, reason: str) -> dict[str, Any]:
    return {
        "innovation_id": item_id,
        "status": "not_found",
        "confidence": 0.4,
        "confidence_reason": "假端点只会按关键词搜，没有语义判断能力；找不到不代表一定不存在。",
        "code_evidence": [],
        "not_found_reason": reason,
        "searched": [pattern],
        "explanation": {
            "intuition": (
                f"我拿着「{pattern}」在仓库里搜了一遍，没有找到看着像实现的代码行。"
                "注意：我是脚本化的假端点，只会做关键词匹配，不会像真模型那样换同义词反复找，"
                "所以这个「未找到」的可信度有限，建议你换成真模型再试一次。"
            ),
            "math": "",
            "code_walkthrough": [],
            "pitfalls": [
                "关键词搜不到，经常只是词没选对（论文用词和代码命名常常不一致）。",
                "别把「未找到」直接当成「论文在吹牛」——先换个说法再搜一次。",
            ],
            "read_next": ["换一个更接近代码命名的词再试"],
        },
    }


def _user_item_partial(item_id: str, hit: dict[str, Any], start: int, end: int, pattern: str) -> dict[str, Any]:
    return {
        "innovation_id": item_id,
        "status": "partial",
        "confidence": 0.4,
        "confidence_reason": (
            "这是按关键词命中的位置，不是语义匹配的结果。假端点没有判断能力，请你自己核对这段代码是否真的是实现。"
        ),
        "code_evidence": [
            {
                "path": hit["path"],
                "line_start": start,
                "line_end": end,
                "why": f"搜「{pattern}」时命中了这里（第 {hit['line']} 行），所以把附近的代码读出来给你看。",
            }
        ],
        "explanation": {
            "intuition": (
                f"你说想看懂「{pattern}」相关的部分，我在仓库里搜到这个关键词出现在 {hit['path']} 第 {hit['line']} 行附近，"
                "就把那一段读出来放在这里。但要提醒你：我（假端点）只会做关键词匹配，"
                "命中的地方不一定真是你要找的实现，请照着代码自己判断一下。"
            ),
            "math": "",
            "code_walkthrough": [
                {"line_ref": f"{hit['path']}:{start}-{end}", "text": "这是关键词命中位置附近的代码，供你核对。"}
            ],
            "pitfalls": [
                "关键词命中 ≠ 实现所在：同一个词可能出现在注释、配置或无关函数里。",
                "用真模型跑时，它会读代码并给出语义判断，这个位置只是线索。",
            ],
            "read_next": [f"{hit['path']}:{start}-{end}"],
        },
    }


def locate_action(messages: list[dict[str, Any]], model: str) -> tuple[str, dict[str, Any], str]:
    """阶段 B：repo_tree → search_code → read_file → 逐条 record_finding → finish"""
    last = messages[-1]
    attempts = len(_tool_messages(messages, "record_finding"))
    accepted = _accepted_findings(messages)
    targets = _target_ids(messages)
    meta = _target_meta(messages)
    pending = targets[accepted] if accepted < len(targets) else None

    if "stuck" in model:
        # 一直卡在同一个错误上：验证后端会提前停止而不是空转到上限
        return "read_file", {"path": "ghost_file_that_does_not_exist.py"}, "call_stuck"

    if not any(m.get("role") == "tool" for m in messages):
        return "repo_tree", {"path": "", "depth": 2}, "call_locate_1"
    if last.get("name") == "repo_tree":
        return "search_code", {"pattern": "lora_"}, "call_locate_2"
    if last.get("name") == "search_code":
        return (
            "read_file",
            {"path": "loralib/layers.py", "start_line": _FORWARD[0], "end_line": _FORWARD[1]},
            "call_locate_3",
        )
    if pending is None:
        return (
            "finish",
            {"coverage_note": "读了 README、loralib/layers.py、train.py；utils/ 和 node_modules 没细看。"},
            "call_locate_done",
        )
    # 剧本里有的条目：直接给写好答案
    if pending in FINDINGS and not _is_user_item(pending, meta):
        if last.get("name") == "read_file":
            if "bad-evidence" in model and attempts == 0:
                return "record_finding", BAD_FINDING, "call_locate_4"
            if "thin-explanation" in model and attempts == 0:
                return "record_finding", THIN_FINDING, "call_locate_thin"
            return "record_finding", FINDINGS[pending], "call_locate_4"
        if last.get("name") == "record_finding":
            return "record_finding", FINDINGS[pending], "call_locate_5"
        return "record_finding", FINDINGS[targets[0]], "call_locate_4"

    # 用户自定义条目：搜 → 读 → 交（找不到就诚实报 not_found）
    item = meta.get(pending) or {"id": pending, "name": pending}
    pattern = _search_pattern(item)
    if last.get("name") == "search_code":
        hits = [hit for hit in _parse_hits(last.get("content") or "") if _looks_like_code(str(hit.get("text") or ""))]
        if not hits:
            return "record_finding", _user_item_not_found(pending, pattern, f"搜「{pattern}」没有命中像实现的代码行。"), "call_user_notfound"
        hit = hits[0]
        start = max(1, int(hit["line"]) - 8)
        return "read_file", {"path": hit["path"], "start_line": start, "end_line": start + 20}, "call_user_read"
    if last.get("name") == "read_file":
        previous = _tool_messages(messages, "search_code")
        hits = [hit for hit in _parse_hits(previous[-1].get("content") or "") if _looks_like_code(str(hit.get("text") or ""))] if previous else []
        if not hits:
            return "record_finding", _user_item_not_found(pending, pattern, f"搜「{pattern}」没有命中像实现的代码行。"), "call_user_notfound"
        hit = hits[0]
        start = max(1, int(hit["line"]) - 8)
        return "record_finding", _user_item_partial(pending, hit, start, start + 20, pattern), "call_user_found"
    return "search_code", {"pattern": pattern, "glob": "**/*.py"}, "call_user_search"


def m0_action(messages: list[dict[str, Any]]) -> tuple[str, dict[str, Any], str, bool]:
    """M0 的 ping → finish 脚本。返回 (工具名, 参数, call_id, 是否带文本)。"""
    last = messages[-1]
    if not any(m.get("role") == "tool" for m in messages):
        return "ping", {"page": 1}, "call_mock_1", False
    return "finish", {"coverage_note": "只验证了链路，没有真的读论文。"}, "call_mock_2", True


# ---------------------------------------------------------------------------
# 协议构造
# ---------------------------------------------------------------------------
def _chunk(delta: dict[str, Any], finish_reason: str | None = None) -> str:
    payload = {
        "id": "chatcmpl-mock",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": MODEL_NAME,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _usage_chunk() -> str:
    payload = {
        "id": "chatcmpl-mock",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": MODEL_NAME,
        "choices": [],
        "usage": {"prompt_tokens": 128, "completion_tokens": 16, "total_tokens": 144},
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _tool_call_chunks(call_id: str, name: str, args: dict[str, Any], parts: int = 3) -> list[str]:
    """把 arguments 的 JSON 串切成 parts 段发出 —— 真实端点会在任意位置切断。"""
    payload = json.dumps(args, ensure_ascii=False)
    size = max(1, -(-len(payload) // parts))
    pieces = [payload[index : index + size] for index in range(0, len(payload), size)]
    chunks = [
        _chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": pieces[0]},
                    }
                ]
            }
        )
    ]
    for piece in pieces[1:]:
        chunks.append(_chunk({"tool_calls": [{"index": 0, "function": {"arguments": piece}}]}))
    chunks.append(_chunk({}, finish_reason="tool_calls"))
    return chunks


def _text_chunks(pieces: tuple[str, ...]) -> list[str]:
    out = [_chunk({"content": piece}) for piece in pieces]
    out.append(_chunk({}, finish_reason="stop"))
    return out



# ---------------------------------------------------------------------------
# 阶段 D（追问对话）的剧本
#
# 对话的工具集同时含论文工具和仓库工具，所以能用"两套都在"来识别这是对话阶段。
# 变体：chat-direct 不查直接答；chatty 一直查不同的失败路径（测每条消息的工具上限）；
#      chat-badcite 故意给一个错的代码位置（测"回答里的位置会被机械核对"）。
# ---------------------------------------------------------------------------
def _chat_answer(with_citation: bool, bad_citation: bool = False, saw_history: bool = False) -> str:
    prefix = "（接着上面说）" if saw_history else ""
    if not with_citation:
        return prefix + (
            "这个问题不需要翻代码就能答：低秩分解的要点是「不改结构、只加两条窄矩阵」。\n\n"
            "原来的做法要更新整块大权重，现在把它拆成 B·A 两个小矩阵的乘积，"
            "参数量从 d×k 降到 r×(d+k)，r 取很小的时候能少好几个数量级。"
        )
    wrong = "loralib/does_not_exist.py:12-20" if bad_citation else "loralib/layers.py:19-22"
    return prefix + (
        "简单说：它没有动模型结构，只是在原来的线性层旁边挂了两条很窄的矩阵，训练时只更新这两条。\n\n"
        f"你问的缩放系数在前向里：loralib/layers.py:{_FORWARD[0] + 3}-{_FORWARD[1] - 1} —— "
        "低秩分支的输出乘上 scaling 之后才加回主干，这就是论文里 (alpha/r)·BAx 那一项。\n\n"
        f"另外初始化的处理在 {wrong}：B 被置零，所以训练刚开始时整个更新量恰好为零，"
        "模型行为和原来完全一致。\n\n"
        "常见误解：这个 scaling 是前向计算时的固定放缩，**不是学习率**；改它不等于改训练步长。"
    )


def chat_action(messages: list[dict[str, Any]], model: str, tool_names: list[str]) -> list[str]:
    last = messages[-1]
    tool_rounds = len([m for m in messages if m.get("role") == "tool"])
    # 用户提示里若带着"## 之前的对话"，说明历史确实被传进来了 —— 回一句标记，让测试能验证这件事
    saw_history = any(
        "## 之前的对话" in (m.get("content") or "") for m in messages if m.get("role") == "user"
    )

    if "read_file" not in tool_names:
        # 还没克隆仓库 / 没读过论文时，只能凭已有产物回答
        return _text_chunks((_chat_answer(with_citation=False, saw_history=saw_history),))

    if "chat-direct" in model:
        return _text_chunks((_chat_answer(with_citation=False, saw_history=saw_history),))

    if "chatty" in model:
        # 每次换一个不存在的文件：错误签名不同，所以"原地打转"守卫不会触发，
        # 只能靠"每条消息的工具调用上限"来停。
        return _tool_call_chunks("call_chat_loop", "read_file", {"path": f"ghost_{tool_rounds}.py"})

    if last.get("name") != "read_file":
        return _tool_call_chunks(
            "call_chat_read",
            "read_file",
            {"path": "loralib/layers.py", "start_line": _FORWARD[0], "end_line": _FORWARD[1]},
        )

    return _text_chunks(
        (
            _chat_answer(
                with_citation=True,
                bad_citation="chat-badcite" in model,
                saw_history=saw_history,
            ),
        )
    )


def _choose(messages: list[dict[str, Any]], model: str, tool_names: list[str]) -> list[str]:
    """返回本次要发出的 SSE chunk 列表。"""
    # 对话阶段：论文工具和仓库工具同时在（侦察只有论文工具，定位只有仓库工具）
    if "list_pages" in tool_names and "repo_tree" in tool_names:
        return chat_action(messages, model, tool_names)

    if "repo_tree" in tool_names:
        name, args, call_id = locate_action(messages, model)
        return _tool_call_chunks(call_id, name, args)

    if "list_pages" in tool_names:
        name, args, call_id = recon_action(messages, model)
        return _tool_call_chunks(call_id, name, args)

    if "ping" in tool_names:
        if "no-tools" in model:
            return _text_chunks(("我不使用工具，直接回答。",))
        if "bad-args" in model:
            if any(m.get("role") == "tool" for m in messages):
                return _text_chunks(("收到 ping 的结果。",))
            return _tool_call_chunks("call_mock_1", "ping", {"page": "1"})
        name, args, call_id, with_text = m0_action(messages)
        prefix = [_chunk({"content": ""})]
        if with_text:
            prefix += _text_chunks(("我已经看到 ping 的结果，", "现在调用 finish 结束本次分析。"))[:-1]
        return prefix + _tool_call_chunks(call_id, name, args)

    return _text_chunks(("没有可用工具，我只输出文本。",))


# ---------------------------------------------------------------------------
# HTTP 端点
# ---------------------------------------------------------------------------
@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True}


@app.get("/v1/models")
async def list_models(request: Request) -> dict[str, Any]:
    """给 probe_endpoint 用的最小 /models：顺便回显请求的 User-Agent（验收断言用）。"""
    return {
        "object": "list",
        "data": [{"id": "mock-model", "object": "model", "owned_by": "mock"}],
        "x_user_agent": request.headers.get("user-agent", ""),
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    auth = request.headers.get("authorization")
    if auth != "Bearer mock-key":
        return JSONResponse(
            status_code=401,
            content={"error": {"message": "Invalid API key", "type": "invalid_request_error"}},
        )

    body = await request.json()
    model = str(body.get("model", ""))

    if "html-error" in model:
        # 模拟第三方网关/Cloudflare 把 HTML 错误页当响应体丢回来
        return HTMLResponse(
            status_code=502,
            content=(
                "<!DOCTYPE html><html><head><title>502 Bad Gateway</title></head>"
                "<body><center><h1>502 Bad Gateway</h1></center><hr>"
                "<center>cloudflare</center></body></html>"
            ),
        )

    messages = body.get("messages", [])
    stream = bool(body.get("stream"))
    include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
    tool_names = [t.get("function", {}).get("name") for t in (body.get("tools") or [])]

    # ua-check：把请求的 User-Agent 原样回显成回复文本。
    # 验收脚本用它断言「自定义 UA 真的穿过 litellm/openai 客户端到达端点」。
    if "ua-check" in model:
        ua = request.headers.get("user-agent", "")
        chunks = _text_chunks((f"UA={ua}",))
        if include_usage:
            chunks = chunks[:-1] + [_usage_chunk(), chunks[-1]]
        if not stream:
            return JSONResponse(content=_non_streaming(chunks))
        return _sse(chunks)

    # slow-model：第一轮响应前先停 1.2 秒，其余行为与 mock-model 完全一致。
    # 两个用途：① m0 验证 per_turn_timeout_seconds 真的会在时限内打断慢端点；
    # ② m6 验证「阶段运行中改清单会被 409 拒绝」——没有这个延迟，
    #   mock 跑得太快，PATCH 根本追不上运行窗口，断言只能靠碰运气。
    if "slow-model" in model and not any(m.get("role") == "tool" for m in messages):
        await asyncio.sleep(1.2)

    chunks = _choose(messages, model, tool_names)
    if include_usage:
        chunks = chunks[:-1] + [_usage_chunk(), chunks[-1]]

    if not stream:
        return JSONResponse(content=_non_streaming(chunks))

    return _sse(chunks)


def _sse(chunks: list[str]) -> StreamingResponse:
    """把拼好的 chunk 序列包成 SSE 流式响应。"""
    async def gen() -> AsyncIterator[str]:
        for piece in [*chunks, "data: [DONE]\n\n"]:
            await asyncio.sleep(CHUNK_DELAY)
            yield piece

    return StreamingResponse(gen(), media_type="text/event-stream")


def _non_streaming(chunks: list[str]) -> dict[str, Any]:
    """把上面拼好的 chunk 序列折叠成一条非流式响应（供 stream=False 的场景）。"""
    text = ""
    tool_call: dict[str, Any] | None = None
    finish = "stop"
    for raw in chunks:
        payload = json.loads(raw[len("data: ") :])
        choice = payload["choices"][0]
        if choice.get("finish_reason"):
            finish = choice["finish_reason"]
        delta = choice.get("delta") or {}
        text += delta.get("content") or ""
        for frag in delta.get("tool_calls") or []:
            if tool_call is None:
                tool_call = {
                    "id": frag.get("id", "call_mock"),
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                }
            if frag.get("id"):
                tool_call["id"] = frag["id"]
            fn = frag.get("function") or {}
            if fn.get("name"):
                tool_call["function"]["name"] = fn["name"]
            if fn.get("arguments"):
                tool_call["function"]["arguments"] += fn["arguments"]

    message: dict[str, Any] = {"role": "assistant", "content": text}
    if tool_call is not None:
        message["tool_calls"] = [tool_call]
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": MODEL_NAME,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 128, "completion_tokens": 16, "total_tokens": 144},
    }
