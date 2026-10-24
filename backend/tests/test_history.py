"""Offline history/restart/provider-switch regressions. All data lives in temporary dirs."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from app import history, main, store
from app.agent.tools.base import ToolContext
from app.agent.tools.history_tools import read_chat_history
from app.chat import append_turn, build_chat_prompt
from app.config import settings
from app.providers import ProviderConfig, ToolCall, TurnResult
from devtools.mock_provider import recon_action, locate_action
from scripts.clean_cache import clean_cache
from tests.paper_fixture import build_pdf
from tests.repo_fixture import build_repo

PROVIDER_A = {"protocol": "openai-compatible", "api_key": "secret-a", "model": "model-a", "base_url": "http://old.invalid/v1"}
PROVIDER_B = {"protocol": "anthropic", "api_key": "secret-b", "model": "model-b", "base_url": "http://new.invalid"}


class HistoryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patch = patch.object(settings, "data_dir", self.root / "data")
        self.patch.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test")
        self.ids = []

    async def asyncTearDown(self):
        for run_id in self.ids:
            run = main.RUNS.pop(run_id, None)
            if run and run.task and not run.task.done():
                run.task.cancel()
                try:
                    await run.task
                except asyncio.CancelledError:
                    pass
        await self.client.aclose()
        self.patch.stop()
        self.tmp.cleanup()

    async def create(self, name="test.pdf"):
        pdf = build_pdf(self.root / name)
        response = await self.client.post("/api/runs", files={"file": (name, pdf.read_bytes(), "application/pdf")},
                                          data={"provider": json.dumps(PROVIDER_A)})
        self.assertEqual(response.status_code, 200, response.text)
        run_id = response.json()["run_id"]
        self.ids.append(run_id)
        return run_id

    async def phase(self, run_id, phase, provider=PROVIDER_A, **payload):
        response = await self.client.post(f"/api/runs/{run_id}/{phase}", json={"provider": provider, **payload})
        self.assertEqual(response.status_code, 200, response.text)
        await main.RUNS[run_id].task

    async def test_saved_history_opens_without_provider_and_switches_only_future_chat(self):
        requests = []
        async def stream(cfg, messages, schemas, on_text, **kwargs):
            requests.append((cfg.model, messages, schemas))
            names = [schema["function"]["name"] for schema in schemas or []]
            if "record_plan" in names:
                name, args, text = recon_action(messages, "mock-model")
            elif "record_finding" in names:
                name, args, text = locate_action(messages, "mock-model")
            else:
                self.assertEqual(cfg.model, "model-b")
                self.assertNotIn("record_plan", names)
                self.assertNotIn("record_finding", names)
                self.assertIn("read_chat_history", names)
                return TurnResult(text="新模型基于旧解读继续回答。")
            return TurnResult(text=text, tool_calls=[ToolCall(id=str(len(requests)), name=name, arguments_raw=json.dumps(args))])

        with patch("app.agent.loop.stream_turn", new=stream), patch.object(settings, "allow_local_repo_paths", True):
            run_id = await self.create()
            await self.phase(run_id, "recon")
            await self.phase(run_id, "locate", repo_url=str(build_repo(self.root)))
            directory = store.run_dir(run_id)
            before = {name: (directory / name).read_bytes() for name in ["plan.json", "artifact.json"]}
            count = len(requests)
            main.RUNS.pop(run_id)
            base = f"/api/runs/{run_id}"
            saved = (await self.client.get(base + "/snapshot")).json()
            self.assertEqual(saved["history"]["analysis_status"], "complete")
            self.assertFalse(saved["active"])
            self.assertTrue(saved["plan"]["innovations"])
            self.assertTrue(saved["findings"])
            self.assertEqual(len(requests), count)
            replay = await self.client.get(base + "/events")
            self.assertEqual(replay.text.count("event: run_end"), 2)
            self.assertIn("event: verification_done", replay.text)
            self.assertTrue((await self.client.get(base)).json()["finished"])
            await self.phase(run_id, "chat", PROVIDER_B, message="继续解释旧结论", output_language="zh-CN")
            prompt = requests[-1][1][1]["content"]
            self.assertIn(saved["findings"][0]["name"], prompt)
            self.assertIn(saved["findings"][0]["explanation"]["intuition"], prompt)
            for name, content in before.items():
                self.assertEqual((directory / name).read_bytes(), content)
            saved = (await self.client.get(base + "/snapshot")).json()
            self.assertEqual(saved["turns"][-1]["provider"]["model"], "model-b")
            self.assertEqual(saved["history"]["original_provider"]["model"], "model-a")
            self.assertEqual(saved["history"]["analysis_status"], "complete")
            for path in directory.glob("*.json*"):
                self.assertNotIn("secret-a", path.read_text())
                self.assertNotIn("secret-b", path.read_text())

    async def test_interrupted_partial_findings_are_available_to_new_model(self):
        run_id = await self.create()
        run = main.RUNS[run_id]
        main._begin_operation(run, "locate", ProviderConfig(**PROVIDER_A))
        await run.bus.emit("repo_cloning", url="local")
        await run.bus.emit("finding", finding={"id": "saved-part", "name": "部分定位", "explanation": {"intuition": "已经确认的特殊事实"}})
        main.RUNS.pop(run_id)
        saved = (await self.client.get(f"/api/runs/{run_id}/snapshot")).json()
        self.assertEqual(saved["history"]["analysis_status"], "interrupted")
        self.assertEqual(saved["findings"][0]["id"], "saved-part")
        async def stream(cfg, messages, schemas, on_text, **kwargs):
            self.assertIn("已经确认的特殊事实", messages[1]["content"])
            return TurnResult(text="继续回答部分定位。")
        with patch("app.agent.loop.stream_turn", new=stream):
            await self.phase(run_id, "chat", PROVIDER_B, message="上次确认了什么？")
        saved = (await self.client.get(f"/api/runs/{run_id}/snapshot")).json()
        self.assertEqual(saved["history"]["analysis_status"], "interrupted")

    async def test_list_paginates_excludes_m0_and_isolates_corruption(self):
        good = await self.create("history.pdf")
        for run_id, content in [("bad-plan", {"paper": {"filename": "bad.pdf"}}), ("m0-only", {"phase": "m0"})]:
            store.write_meta(run_id, **content)
            self.ids.append(run_id)
        store.write_json(store.run_dir("bad-plan") / "artifact.json", {"run": "broken"})
        store.write_json(store.run_dir(good) / "history.json", {"schema_version": 1})
        response = await self.client.get("/api/runs?limit=1")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["total"], 2)
        self.assertEqual(len(response.json()["items"]), 1)
        search = (await self.client.get("/api/runs?q=history.pdf")).json()
        self.assertEqual(search["items"][0]["run_id"], good)
        saved = (await self.client.get("/api/runs/bad-plan/snapshot")).json()
        self.assertFalse(saved["can_chat"])
        self.assertTrue(any("artifact.json" in item for item in saved["warnings"]))

    async def test_index_reconciles_durable_end_and_repaired_journal(self):
        run_id = await self.create()
        run = main.RUNS[run_id]
        main._begin_operation(run, "recon", ProviderConfig(**PROVIDER_A))
        await run.bus.emit("run_start", meta={"phase": "recon"})
        # Simulate power loss after journal fsync but before index replacement.
        run.bus.on_event = None
        await run.bus.emit("run_end", status="ok")
        main.RUNS.pop(run_id)
        saved = (await self.client.get(f"/api/runs/{run_id}/snapshot")).json()
        self.assertEqual(saved["history"]["analysis_status"], "recon_complete")
        log = store.events_path(run_id)
        original = log.read_bytes()
        with log.open("ab") as handle:
            handle.write(b'{"id":')
        broken = (await self.client.get(f"/api/runs/{run_id}/snapshot")).json()
        self.assertFalse(broken["can_chat"])
        log.write_bytes(original)
        repaired = (await self.client.get(f"/api/runs/{run_id}/snapshot")).json()
        self.assertTrue(repaired["can_chat"], repaired["warnings"])

    async def test_failed_chat_does_not_fail_completed_analysis_and_old_turns_are_readable(self):
        run_id = await self.create()
        run = main.RUNS[run_id]
        main._begin_operation(run, "locate", ProviderConfig(**PROVIDER_A))
        await run.bus.emit("run_end", status="ok")
        for number in range(15):
            append_turn(run_id, "user", f"过去的问题 {number}")
        async def failed(*args, **kwargs):
            raise RuntimeError("provider no longer available")
        with patch("app.agent.loop.stream_turn", new=failed):
            await self.phase(run_id, "chat", PROVIDER_B, message="新的问题")
        saved = (await self.client.get(f"/api/runs/{run_id}/snapshot")).json()
        self.assertEqual(saved["history"]["analysis_status"], "complete")
        self.assertEqual(saved["history"]["latest_operation"]["status"], "failed")
        result = await read_chat_history({"offset": 0, "limit": 2}, ToolContext(run_id, store.run_dir(run_id), run.bus))
        self.assertIn("过去的问题 0", result.content)
        prompt = build_chat_prompt(saved["turns"], "followup")
        self.assertIn("read_chat_history", prompt)
        self.assertIn("历史助手（model-b）", prompt)

    async def test_cleanup_and_invalid_id_never_delete_or_create_history(self):
        run_id = await self.create()
        directory = store.run_dir(run_id)
        cache = directory / "cache"
        cache.mkdir(exist_ok=True)
        (cache / "generated.txt").write_text("cache")
        (directory / "chat.jsonl").write_text("")
        self.assertEqual(clean_cache(), 1)
        self.assertTrue((directory / "paper.pdf").exists())
        self.assertTrue((directory / "meta.json").exists())
        self.assertTrue((directory / "chat.jsonl").exists())
        self.assertFalse(cache.exists())
        self.assertEqual((await self.client.get("/api/runs/nonexistent/snapshot")).status_code, 404)
        self.assertFalse((settings.data_dir / "nonexistent").exists())
        with self.assertRaises(ValueError):
            store.run_dir("../outside")

    def test_safe_provider_summary_excludes_url_credentials(self):
        cfg = ProviderConfig(**{**PROVIDER_A, "base_url": "https://username:password@host.invalid/v1?token=private"})
        self.assertEqual(cfg.safe_summary()["host"], "host.invalid")
        self.assertNotIn("password", cfg.safe_label())
        self.assertNotIn("private", cfg.safe_label())

    async def test_live_long_reply_keeps_its_boundary_beyond_trace_page(self):
        run_id = await self.create()
        run = main.RUNS[run_id]
        for _ in range(5):
            await run.bus.emit("run_end", status="ok")
        main._begin_operation(run, "chat", ProviderConfig(**PROVIDER_B))
        user = await run.bus.emit("chat_user", message="长回答")
        await run.bus.emit("step_start", mode="answer")
        for _ in range(history.TRACE_PAGE_SIZE + 1):
            await run.bus.emit("assistant_text", delta="内容")
        saved = history.snapshot(run_id, active=True, events=run.bus.history)
        self.assertEqual(saved["events"][0]["id"], user["id"])
        self.assertTrue(saved["has_older_events"])
        earlier = await self.client.get(f"/api/runs/{run_id}/trace", params={"before": user["id"], "limit": 2})
        self.assertEqual(len(earlier.json()["events"]), 2)
        self.assertTrue(earlier.json()["has_more"])

    async def test_user_cancel_and_server_shutdown_have_distinct_durable_states(self):
        run_id = await self.create()
        run = main.RUNS[run_id]
        main._begin_operation(run, "recon", ProviderConfig(**PROVIDER_A))
        main._launch(run, lambda: asyncio.sleep(30))
        cancelled = await self.client.post(f"/api/runs/{run_id}/cancel")
        self.assertTrue(cancelled.json()["cancelled"])
        self.assertEqual(history.index_entry(run_id)["latest_operation"]["status"], "cancelled")
        main._begin_operation(run, "chat", ProviderConfig(**PROVIDER_B))
        main._launch(run, lambda: asyncio.sleep(30))
        await asyncio.sleep(0)
        run.task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await run.task
        self.assertEqual(history.index_entry(run_id)["latest_operation"]["status"], "interrupted")

    async def test_sse_drains_all_queued_events_if_task_finishes_during_replay(self):
        run_id = await self.create()
        run = main.RUNS[run_id]
        main._begin_operation(run, "recon", ProviderConfig(**PROVIDER_A))
        async def complete():
            await run.bus.emit("run_start", meta={"phase": "recon"})
            await run.bus.emit("tool_call", tool="finish")
            await run.bus.emit("verification_done", summary={})
            await run.bus.emit("run_end", status="ok")
        main._launch(run, complete)
        class SlowRequest:
            headers = {}
            query_params = {}
            async def is_disconnected(self):
                await run.task
                return False
        response = await main.run_events(run_id, SlowRequest())
        delivered = [event async for event in response.body_iterator]
        self.assertEqual([event["event"] for event in delivered][-3:], ["verification_done", "run_end", "stream_idle"])
