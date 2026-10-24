"""真实评估路径的离线回归：不调用付费模型，不依赖 GitHub。"""

from __future__ import annotations

import argparse
import asyncio
import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from app import repo_source
from app.config import Settings
from app.eval_metrics import evaluate_pair, markdown_table
from eval import run_eval
from scripts.harness import service_env
from tests.repo_fixture import build_mini_repo, commit_all

SHA = "a" * 40
PROVIDER = {"protocol": "openai-compatible", "base_url": "http://mock/v1", "model": "mock", "api_key": "secret-canary"}
GOLD = {"id": "paper", "paper": {"title": "Test", "pdf": "paper.pdf"},
        "repo": {"url": "https://github.com/example/repo", "commit_sha": SHA}, "innovations": []}
PLAN = {"innovations": [{"id": "i1", "name": "test"}]}
ARTIFACT = {"missing_ids": [], "innovations": [], "run": {"repo": {"commit_sha": SHA}}}


def end_event(*, status="ok", reason="Agent 主动结束", tokens=100, **extra):
    return {"id": 9, "type": "run_end", "data": {
        "status": status, "stopped_reason": reason,
        "usage": {"tool_calls": 3, "input_tokens": tokens, "output_tokens": 10, "seconds": 2}, **extra,
    }}


class PinnedRepoTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = build_mini_repo(self.root / "source", {"version.py": "old\n", "nested/a.py": "old\n"})
        self.old = repo_source.run_git(["rev-parse", "HEAD"], cwd=self.source).stdout.strip()
        (self.source / "version.py").write_text("new\n")
        commit_all(self.source)
        self.new = repo_source.run_git(["rev-parse", "HEAD"], cwd=self.source).stdout.strip()
        settings = Settings(_env_file=None, data_dir=self.root / "data", allow_local_repo_paths=True,
                            repo_isolate_git_config=True)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(repo_source, "settings", settings))

    def test_old_commit_cold_cache_and_head_are_separate(self):
        for sparse in (True, False):
            with self.subTest(sparse=sparse), patch.object(repo_source.settings, "repo_sparse", sparse):
                url = self.source.as_uri()  # file:// 真正启用浅克隆，不能靠本地全历史侥幸过关
                dest = self.root / f"old-{sparse}"
                info = repo_source.clone_repo(url, dest, commit=self.old)
                self.assertEqual(info.commit_sha, self.old)
                self.assertFalse(info.from_cache)
                self.assertEqual((dest / "version.py").read_text(), "old\n")
                self.assertEqual(repo_source.run_git(["rev-parse", "--is-shallow-repository"], cwd=dest).stdout.strip(), "true")
                cached = repo_source.clone_repo(url, self.root / f"cache-{sparse}", commit=self.old)
                self.assertTrue(cached.from_cache)
                self.assertEqual(cached.commit_sha, self.old)
                self.assertEqual((cached.root / "version.py").read_text(), "old\n")
                latest = repo_source.clone_repo(url, self.root / f"latest-{sparse}")
                self.assertEqual(latest.commit_sha, self.new)
                self.assertEqual((latest.root / "version.py").read_text(), "new\n")

    def test_bad_commit_never_falls_back_to_head(self):
        for commit in ("HEAD", "--upload-pack=bad", "a" * 39):
            with self.subTest(commit=commit), self.assertRaises(repo_source.RepoError):
                repo_source.clone_repo(str(self.source), self.root / "invalid", commit=commit)
        dest = self.root / "missing"
        with self.assertRaises(repo_source.RepoError):
            repo_source.clone_repo(self.source.as_uri(), dest, commit=SHA)
        self.assertFalse(dest.exists())
        self.assertFalse(repo_source._cache_entry(self.source.as_uri(), SHA).exists())

    def test_local_gold_can_pin_an_ancestor(self):
        entry = copy.deepcopy(GOLD)
        entry["repo"]["commit_sha"] = self.old
        self.assertTrue(run_eval.check_frozen_commit(entry, str(self.source))[0])
        entry["repo"]["commit_sha"] = SHA
        self.assertFalse(run_eval.check_frozen_commit(entry, str(self.source))[0])
        for commit in (None, "main", "abc", 123):
            entry["repo"]["commit_sha"] = commit
            self.assertFalse(run_eval.check_frozen_commit(entry, GOLD["repo"]["url"])[0])

    def test_checkout_timeout_cleans_destination(self):
        dest = self.root / "timeout"
        dest.mkdir()
        with patch.object(repo_source, "_run_git_streaming", side_effect=repo_source.RepoError("timeout")):
            with self.assertRaises(repo_source.RepoError):
                repo_source._checkout_commit(dest, SHA)
        self.assertFalse(dest.exists())

    def test_locate_request_rejects_non_sha(self):
        from app.main import LocateRequest
        from pydantic import ValidationError
        base = {"provider": PROVIDER, "repo_url": GOLD["repo"]["url"]}
        self.assertIsNone(LocateRequest(**base).commit)
        self.assertEqual(LocateRequest(**base, commit=SHA).commit, SHA)
        for commit in ("main", "abc", "--help", "a" * 40 + "\n"):
            with self.subTest(commit=commit), self.assertRaises(ValidationError):
                LocateRequest(**base, commit=commit)


class ServiceEnvTest(unittest.TestCase):
    def test_real_respects_environment_and_mock_is_deterministic(self):
        with patch.dict(os.environ, {"PAPER2CODE_LLM_MAX_REQUESTS_PER_MINUTE": "10", "PAPER2CODE_REPO_NETWORK_MODE": "hosted"}):
            real = service_env(pin_local_config=False)
            mock = service_env()
            self.assertEqual(real["PAPER2CODE_LLM_MAX_REQUESTS_PER_MINUTE"], "10")
            self.assertEqual(real["PAPER2CODE_REPO_NETWORK_MODE"], "hosted")
            self.assertEqual(mock["PAPER2CODE_LLM_MAX_REQUESTS_PER_MINUTE"], "0")
            self.assertEqual(mock["PAPER2CODE_REPO_NETWORK_MODE"], "local")
            self.assertEqual(service_env({"PAPER2CODE_LLM_MAX_REQUESTS_PER_MINUTE": "7"})["PAPER2CODE_LLM_MAX_REQUESTS_PER_MINUTE"], "7")

    def test_real_does_not_shadow_dotenv(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            dotenv = Path(temp) / ".env"
            dotenv.write_text("PAPER2CODE_LLM_MAX_REQUESTS_PER_MINUTE=9\nPAPER2CODE_REPO_NETWORK_MODE=hosted\n")
            env = service_env(pin_local_config=False)
            self.assertNotIn("PAPER2CODE_LLM_MAX_REQUESTS_PER_MINUTE", env)
            with patch.dict(os.environ, env, clear=True):
                config = Settings(_env_file=dotenv)
                self.assertEqual(config.llm_max_requests_per_minute, 9)
                self.assertEqual(config.repo_network_mode, "hosted")


class EvalRunTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pdf = self.root / "paper.pdf"
        self.pdf.write_bytes(b"mock PDF")
        self.requests = []
        self.artifact = copy.deepcopy(ARTIFACT)
        self.phase_ends = [end_event(tokens=100), end_event(tokens=200)]
        self.details = 0

    def handler(self, request):
        self.requests.append(request)
        if request.method == "POST" and request.url.path == "/api/runs":
            return httpx.Response(200, json={"run_id": "run1"})
        if request.method == "GET":
            self.details += 1
            artifact = {"plan": PLAN} if self.details == 1 else self.artifact
            return httpx.Response(200, json={"artifact": artifact})
        return httpx.Response(200, json={})

    async def run_pair(self, entry=None, *, force=False):
        with patch.object(run_eval, "resolve_pdf", return_value=self.pdf), patch.object(
            run_eval, "_run_phase", new=AsyncMock(side_effect=self.phase_ends)
        ) as phase:
            async with httpx.AsyncClient(transport=httpx.MockTransport(self.handler), base_url="http://test") as client:
                report = await run_eval.run_one(client, entry or GOLD, PROVIDER, timeout_s=1, verbose=False, force=force)
        return report, phase

    async def test_pins_payload_and_records_both_stage_usage(self):
        report, phase = await self.run_pair()
        self.assertEqual(phase.call_args_list[1].args[3]["commit"], SHA)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["usage"]["input_tokens"], 300)
        self.assertEqual(report["usage"]["output_tokens"], 20)
        self.assertTrue(report["usage_complete"])
        self.assertFalse(report["partial"])
        self.assertIn("wall_clock_seconds", report)

    async def test_wrong_artifact_commit_is_invalid(self):
        self.artifact["run"]["repo"]["commit_sha"] = "b" * 40
        report, _ = await self.run_pair()
        self.assertEqual(report["status"], "invalid")
        self.assertEqual(report["expected_commit_sha"], SHA)

    async def test_missing_frozen_commit_never_scores_even_with_force(self):
        entry = copy.deepcopy(GOLD)
        entry["repo"].pop("commit_sha")
        report, phase = await self.run_pair(entry)
        self.assertEqual(report["status"], "invalid")
        phase.assert_not_called()
        report, _ = await self.run_pair(entry, force=True)
        self.assertEqual(report["status"], "invalid")

    async def test_missing_pdf_is_an_isolated_failure(self):
        entry = copy.deepcopy(GOLD)
        entry["paper"]["pdf"] = str(self.root / "missing.pdf")
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.handler), base_url="http://test") as client:
            report = await run_eval.run_one(client, entry, PROVIDER, timeout_s=1, verbose=False)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["stage"], "准备")
        self.assertEqual(self.requests, [])
        self.assertIsNone(report["usage"]["input_tokens"])
        self.assertFalse(report["usage_complete"])

    async def test_no_artifact_preserves_failure_usage_and_reason(self):
        self.artifact = {"plan": PLAN}
        self.phase_ends[1] = end_event(status="failed", reason="clone failed", tokens=12)
        report, _ = await self.run_pair()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["run_id"], "run1")
        self.assertEqual(report["usage"]["input_tokens"], 112)
        self.assertEqual(report["error"], "clone failed")

    async def test_budget_and_provider_failure_deliveries_are_marked_partial(self):
        for event in (end_event(reason="工具调用次数达到上限 40"), end_event(status="failed", partial=True)):
            with self.subTest(event=event):
                self.details = 0
                self.phase_ends[1] = event
                report, _ = await self.run_pair()
                self.assertEqual(report["status"], "ok")
                self.assertTrue(report["partial"])

    async def test_http_failure_redacts_key_and_keeps_recon_usage(self):
        self.phase_ends[1] = run_eval.PairFailed("定位", "server echoed " + PROVIDER["api_key"])
        report, _ = await self.run_pair()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["usage"]["input_tokens"], 100)
        self.assertFalse(report["usage_complete"])
        self.assertNotIn(PROVIDER["api_key"], json.dumps(report))

    async def test_phase_timeout_disconnect_and_http_errors_cancel(self):
        for error in (TimeoutError(), httpx.ReadTimeout("read timeout"), None):
            with self.subTest(error=error):
                self.requests.clear()
                async def handler(request):
                    self.requests.append(request)
                    return httpx.Response(200, json={"events": []})
                result = ([], 0, 0)
                with patch.object(run_eval, "collect_sse", new=AsyncMock(side_effect=error, return_value=result)):
                    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test") as client:
                        with self.assertRaises(run_eval.PairFailed):
                            await run_eval._run_phase(client, "run1", "/api/runs/run1/locate", {}, stage="定位", timeout_s=1)
                self.assertIn("/api/runs/run1/cancel", [r.url.path for r in self.requests])

    async def test_phase_timeout_is_wall_clock_and_does_not_reuse_recon_end(self):
        async def slow_sse(*args, **kwargs):
            await asyncio.sleep(10)
        event = end_event()
        async def handler(request):
            self.requests.append(request)
            return httpx.Response(200, json={"events": [event]})
        with patch.object(run_eval, "collect_sse", new=slow_sse):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test") as client:
                with self.assertRaises(run_eval.PairFailed) as caught:
                    await asyncio.wait_for(run_eval._run_phase(
                        client, "run1", "/api/runs/run1/locate", {}, stage="定位", timeout_s=.01
                    ), timeout=1)
        self.assertIsNone(caught.exception.end_event)
        self.assertIn("已请求取消", str(caught.exception))

    async def test_cancel_failure_is_not_reported_as_success(self):
        def handler(request):
            return httpx.Response(503 if request.url.path.endswith("/cancel") else 200, json={"events": []})
        with patch.object(run_eval, "collect_sse", new=AsyncMock(side_effect=TimeoutError())):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test") as client:
                with self.assertRaises(run_eval.PairFailed) as caught:
                    await run_eval._run_phase(client, "run1", "/api/runs/run1/locate", {}, stage="定位", timeout_s=1)
        self.assertIn("取消请求失败", str(caught.exception))

    async def test_disconnect_recovers_current_stage_usage(self):
        previous = end_event(tokens=100)
        current = end_event(status="cancelled", reason="用户取消", tokens=7)
        current["id"] = previous["id"] + 1
        gets = 0
        def handler(request):
            nonlocal gets
            if request.method == "GET":
                gets += 1
            return httpx.Response(200, json={"events": [previous] if gets < 2 else [previous, current]})
        with patch.object(run_eval, "collect_sse", new=AsyncMock(side_effect=httpx.ReadError("disconnected"))):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test") as client:
                with self.assertRaises(run_eval.PairFailed) as caught:
                    await run_eval._run_phase(client, "run1", "/api/runs/run1/locate", {}, stage="定位", timeout_s=1)
        self.phase_ends[1] = caught.exception
        report, _ = await self.run_pair()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["usage"]["input_tokens"], 107)
        self.assertEqual(report["phases"]["locate"]["status"], "cancelled")

    async def test_startup_failure_cleans_up_already_started_services(self):
        args = argparse.Namespace(provider="mock", label=None, goldset="eval/goldset.yaml",
                                  only=None, force=False, phase_timeout=1)
        proc = object()
        with patch.object(run_eval, "load_goldset", return_value=[GOLD]), patch.object(
            run_eval, "start_service", side_effect=[proc, RuntimeError("port occupied")]
        ), patch.object(run_eval, "stop_services") as stop, redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "port occupied"):
                await run_eval.main_async(args)
        stop.assert_called_once_with(proc)

    async def test_main_continues_after_failure_and_checkpoints_real_reports(self):
        bad = copy.deepcopy(GOLD)
        bad.update(id="missing")
        bad["paper"]["pdf"] = str(self.root / "missing.pdf")
        good = copy.deepcopy(GOLD)
        good["paper"]["pdf"] = str(self.pdf)
        args = argparse.Namespace(provider="real", label="model/test", goldset=str(self.root / "gold.yaml"),
                                  only=None, force=False, phase_timeout=1)
        transport_client = httpx.AsyncClient(transport=httpx.MockTransport(self.handler), base_url="http://test")
        real_write = run_eval._write_report
        snapshots = []
        def checkpoint(*args, **kwargs):
            summary = real_write(*args, **kwargs)
            snapshots.append(json.loads(args[5][0].read_text()))
            return summary
        with ExitStack() as stack:
            for attr in ("REPORT_JSON", "REPORT_MD"):
                baseline = self.root / getattr(run_eval, attr).name
                baseline.write_text("baseline-canary")
                stack.enter_context(patch.object(run_eval, attr, baseline))
            stack.enter_context(patch.object(run_eval, "REPORTS_DIR", self.root / "reports"))
            stack.enter_context(patch.object(run_eval, "load_goldset", return_value=[bad, good]))
            start = stack.enter_context(patch.object(run_eval, "start_service"))
            stop = stack.enter_context(patch.object(run_eval, "stop_services"))
            stack.enter_context(patch.object(run_eval, "wait_http", new=AsyncMock()))
            stack.enter_context(patch.object(run_eval.httpx, "AsyncClient", return_value=transport_client))
            stack.enter_context(patch.object(run_eval, "_run_phase", new=AsyncMock(side_effect=self.phase_ends)))
            stack.enter_context(patch.object(run_eval, "_write_report", side_effect=checkpoint))
            stack.enter_context(patch.dict(os.environ, {
                "PAPER2CODE_EVAL_BASE_URL": PROVIDER["base_url"], "PAPER2CODE_EVAL_MODEL": PROVIDER["model"],
                "PAPER2CODE_EVAL_API_KEY": PROVIDER["api_key"],
            }))
            with redirect_stdout(io.StringIO()):
                code = await run_eval.main_async(args)
        self.assertEqual(code, 1)
        start.assert_called_once()
        self.assertFalse(start.call_args.kwargs["pin_local_config"])
        stop.assert_called_once()
        self.assertEqual([len(p["pairs"]) for p in snapshots], [1, 2])
        summary = snapshots[-1]["aggregate"]
        self.assertEqual(summary["pairs"], 1)
        self.assertEqual(summary["pairs_attempted"], 2)
        self.assertEqual(summary["pairs_requested"], 2)
        self.assertEqual(summary["pairs_unscored"][0]["id"], "missing")
        self.assertNotIn(PROVIDER["api_key"], json.dumps(snapshots))
        self.assertEqual(len(list((self.root / "reports").glob("*.json"))), 1)
        self.assertIn("1/2", next((self.root / "reports").glob("*.md")).read_text())
        for name in ("report.json", "report.md"):
            self.assertEqual((self.root / name).read_text(), "baseline-canary")


class ReportTest(unittest.TestCase):
    def test_real_paths_never_collide_with_mock_or_each_other(self):
        mock = argparse.Namespace(provider="mock")
        real = argparse.Namespace(provider="real")
        self.assertEqual(run_eval._report_paths(mock, "test"), (run_eval.REPORT_JSON, run_eval.REPORT_MD))
        with patch.object(run_eval.time, "strftime", return_value="same-second"):
            first = run_eval._report_paths(real, "../../model/test")
            second = run_eval._report_paths(real, "../../model/test")
        self.assertNotEqual(first, second)
        self.assertEqual(first[0].parent, run_eval.REPORTS_DIR)
        self.assertNotIn(first[0], run_eval._report_paths(mock, "test"))
        unicode_path = run_eval._report_paths(real, "模型" * 100)[0]
        self.assertLessEqual(len(unicode_path.name.encode("utf-8")), 255)

    def test_unscored_cost_is_included_but_not_its_quality_scores(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            good = evaluate_pair(GOLD, PLAN, ARTIFACT)
            good.update(status="ok", partial=True, usage={"input_tokens": 100}, wall_clock_seconds=2, usage_complete=True)
            bad = {"id": "bad", "status": "failed", "usage": {"input_tokens": 50}, "wall_clock_seconds": 1}
            args = argparse.Namespace(goldset="eval/goldset.yaml")
            summary = run_eval._write_report(args, "test", "", PROVIDER, [good, bad],
                                             (root / "report.json", root / "report.md"), pairs_requested=3)
            self.assertEqual(summary["pairs"], 1)
            self.assertEqual(summary["usage_total"]["input_tokens"], 150)
            self.assertEqual(summary["wall_clock_seconds_total"], 3)
            self.assertEqual(summary["pairs_partial"], 1)
            table = markdown_table(summary, provider_label="test")
            self.assertIn("1/2", table)
            self.assertIn("bad failed", table)
            self.assertIn("已处理 2/3", table)
            self.assertIn("提前交付", table)
            self.assertFalse((root / "report.json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
