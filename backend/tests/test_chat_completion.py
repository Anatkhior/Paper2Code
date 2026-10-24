"""追问收尾和改名兼容的离线回归。"""
import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agent.loop import run_agent
from app.agent.tools.base import Budget
from app.agent.tools.builtin import CHAT_FINISH, PING
from app.config import Settings
from app.events import RunBus
from app.providers import ProviderConfig, ToolCall, TurnResult
from scripts.harness import service_env


class ChatCompletionTest(unittest.IsolatedAsyncioTestCase):
    async def run_script(self, results, *, calls=8, timeout=10):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            bus = RunBus("test", path / "events.jsonl")
            budget = Budget(calls, 100_000, timeout, time.monotonic())
            requests = []
            finalized = []
            async def stream(cfg, messages, schemas, on_text, **kwargs):
                requests.append((list(messages), schemas))
                item = results[len(requests) - 1]
                if isinstance(item, Exception):
                    await on_text("已写出的一部分")
                    raise item
                if item == "cancel":
                    await on_text("已写出的一部分")
                    raise asyncio.CancelledError()
                await on_text(item.text)
                return item
            async def finalize(summary):
                finalized.append(summary.copy())
                await bus.emit("chat_reply", message=summary.get("final_text"), status=summary["status"])
                return {}
            with patch("app.agent.loop.stream_turn", new=stream):
                try:
                    report = await run_agent(
                        cfg=ProviderConfig(protocol="openai-compatible", api_key="mock-key", model="mock"),
                        tools=[PING, CHAT_FINISH], system_prompt="test", user_prompt="解释论文",
                        bus=bus, budget=budget, run_dir_path=path, finalize=finalize, require_final_answer=True,
                    )
                except asyncio.CancelledError:
                    report = finalized[-1]
            return report, requests, bus.history

    async def test_intro_plus_finish_must_generate_real_answer(self):
        intro = TurnResult(text="下面给你完整导读。", tool_calls=[ToolCall(id="end", name="finish", arguments_raw='{"coverage_note":"读了四页"}')])
        report, requests, events = await self.run_script([intro, TurnResult(text="论文研究低秩更新。具体方法是……", finish_reason="stop")], timeout=180)
        self.assertEqual(len(requests), 2)
        self.assertIsNotNone(requests[0][1])
        self.assertIsNone(requests[1][1])
        self.assertEqual(report["final_text"], "论文研究低秩更新。具体方法是……")
        self.assertFalse(report["partial"])
        self.assertEqual([e["type"] for e in events][-2:], ["chat_reply", "run_end"])
        self.assertEqual(sum(e["type"] == "chat_reply" for e in events), 1)

    async def test_direct_answer_does_not_generate_twice(self):
        report, requests, _ = await self.run_script([TurnResult(text="直接回答。")], timeout=180)
        self.assertEqual(len(requests), 1)
        self.assertEqual(report["status"], "ok")

    async def test_finish_narration_is_not_delivered_when_answer_fails(self):
        report, _, _ = await self.run_script([RuntimeError("endpoint failure")])
        self.assertEqual(report["status"], "failed")
        self.assertTrue(report["partial"])
        self.assertEqual(report["final_text"], "已写出的一部分")

    async def test_cancel_keeps_partial_reply_before_run_end(self):
        report, _, events = await self.run_script(["cancel"])
        self.assertEqual(report["status"], "cancelled")
        self.assertEqual(report["final_text"], "已写出的一部分")
        self.assertEqual([e["type"] for e in events][-2:], ["chat_reply", "run_end"])

    async def test_empty_or_truncated_response_is_not_complete(self):
        for result in (TurnResult(), TurnResult(text="断了一半", finish_reason="length")):
            report, _, _ = await self.run_script([result])
            self.assertTrue(report["partial"])
            self.assertEqual(report["status"], "failed")

    async def test_budget_reserves_answer_and_never_mentions_locate_tools(self):
        tool = TurnResult(tool_calls=[ToolCall(id="ping", name="ping", arguments_raw='{"page":1}')])
        report, requests, events = await self.run_script([tool, tool, TurnResult(text="根据已查材料回答。")], calls=4, timeout=180)
        self.assertIsNone(requests[-1][1])
        self.assertLessEqual(report["usage"]["tool_calls"], 4)
        warnings = " ".join(e["data"]["reason"] for e in events if e["type"] == "budget_warning")
        self.assertNotIn("record_finding", warnings)
        self.assertNotIn("not_found", warnings)


class ConfigRenameTest(unittest.TestCase):
    def test_sources_keep_precedence_and_legacy_env_works(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            dotenv = Path(tmp) / ".env"
            dotenv.write_text("PAPERLENS_LLM_MAX_REQUESTS_PER_MINUTE=8\nPAPER2CODE_LLM_MAX_REQUESTS_PER_MINUTE=9\n")
            self.assertEqual(Settings(_env_file=dotenv).llm_max_requests_per_minute, 9)
            with patch.dict(os.environ, {"PAPERLENS_LLM_MAX_REQUESTS_PER_MINUTE": "6"}):
                self.assertEqual(Settings(_env_file=dotenv).llm_max_requests_per_minute, 6)
                with patch.dict(os.environ, {"PAPER2CODE_LLM_MAX_REQUESTS_PER_MINUTE": "7"}):
                    self.assertEqual(Settings(_env_file=dotenv).llm_max_requests_per_minute, 7)
                    self.assertEqual(Settings(_env_file=dotenv, llm_max_requests_per_minute=4).llm_max_requests_per_minute, 4)
            dotenv.write_text("PAPERLENS_LLM_MAX_REQUESTS_PER_MINUTE=8\n")
            self.assertEqual(Settings(_env_file=dotenv).llm_max_requests_per_minute, 8)

    def test_harness_translates_old_overrides_before_pinning(self):
        with patch.dict(os.environ, {"PAPERLENS_LLM_MAX_REQUESTS_PER_MINUTE": "8"}, clear=True):
            self.assertEqual(service_env(pin_local_config=False)["PAPER2CODE_LLM_MAX_REQUESTS_PER_MINUTE"], "8")
            self.assertEqual(service_env()["PAPER2CODE_LLM_MAX_REQUESTS_PER_MINUTE"], "0")
            self.assertEqual(service_env({"PAPERLENS_LLM_MAX_REQUESTS_PER_MINUTE": "5"})["PAPER2CODE_LLM_MAX_REQUESTS_PER_MINUTE"], "5")


if __name__ == "__main__":
    unittest.main()
