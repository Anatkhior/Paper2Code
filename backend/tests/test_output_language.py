"""Exercise language selection at HTTP boundaries and at the actual agent/provider boundary."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from app import main, store
from app.config import settings
from app.providers import ToolCall, TurnResult
from devtools.mock_provider import recon_action, locate_action
from tests.paper_fixture import build_pdf
from tests.repo_fixture import build_repo


PROVIDER = {"protocol": "openai-compatible", "api_key": "mock-key", "model": "mock-model"}


class OutputLanguageTest(unittest.IsolatedAsyncioTestCase):
    async def test_language_flows_through_all_phases_without_rewriting_history(self):
        requests = []
        async def stream(cfg, messages, schemas, on_text, **kwargs):
            system = messages[0]["content"]
            requests.append(system)
            names = [schema["function"]["name"] for schema in schemas or []]
            if "record_plan" in names:
                name, args, text = recon_action(messages, "mock-model")
            elif "record_finding" in names:
                name, args, text = locate_action(messages, "mock-model")
            elif schemas:
                name, args, text = "finish", {"coverage_note": "已查阅"}, "下面开始解释。"
            else:
                text = "English answer." if "English（英语）" in system else "这是中文回答。"
                await on_text(text)
                return TurnResult(text=text, finish_reason="stop")
            await on_text(text)
            return TurnResult(text=text, tool_calls=[ToolCall(id=f"tool-{len(requests)}", name=name, arguments_raw=json.dumps(args))])

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pdf = build_pdf(root / "paper.pdf")
            repo = build_repo(root)
            with patch.object(settings, "data_dir", root / "data"), patch.object(settings, "allow_local_repo_paths", True), patch("app.agent.loop.stream_turn", new=stream):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
                    created = await client.post("/api/runs", files={"file": ("paper.pdf", pdf.read_bytes(), "application/pdf")}, data={"provider": json.dumps(PROVIDER)})
                    self.assertEqual(created.status_code, 200, created.text)
                    run_id = created.json()["run_id"]
                    base = f"/api/runs/{run_id}"
                    self.addCleanup(main.RUNS.pop, run_id, None)
                    self.assertEqual(store.read_meta(run_id)["output_language"], "zh-CN")

                    async def phase(name, payload, language):
                        start = len(requests)
                        response = await client.post(f"{base}/{name}", json={"provider": PROVIDER, **payload})
                        self.assertEqual(response.status_code, 200, response.text)
                        await main.RUNS[run_id].task
                        self.assertGreater(len(requests), start)
                        for prompt in requests[start:]:
                            self.assertIn(language, prompt)
                            self.assertIn("code_walkthrough 中每一条 text", prompt)
                            self.assertIn("paper_evidence.quote 必须逐字保留原文", prompt)

                    await phase("recon", {}, "简体中文（Simplified Chinese）")
                    plan_before = store.read_json(store.run_dir(run_id) / "plan.json")
                    await phase("locate", {"repo_url": str(repo), "output_language": "en"}, "English（英语）")
                    artifact_before = store.read_json(store.run_dir(run_id) / "artifact.json")
                    self.assertEqual(artifact_before["run"]["output_language"], "en")
                    await phase("chat", {"message": "Explain it"}, "English（英语）")
                    await phase("chat", {"message": "继续解释", "output_language": "zh-CN"}, "简体中文（Simplified Chinese）")
                    turns = (await client.get(f"{base}/chat")).json()["turns"]
                    self.assertEqual([turn["text"] for turn in turns if turn["role"] == "assistant"], ["English answer.", "这是中文回答。"])
                    self.assertEqual([turn["output_language"] for turn in turns if turn["role"] == "assistant"], ["en", "zh-CN"])
                    self.assertEqual(store.read_json(store.run_dir(run_id) / "plan.json"), plan_before)
                    self.assertEqual(store.read_json(store.run_dir(run_id) / "artifact.json"), artifact_before)

                    # Legacy runs without a language use the documented Chinese default.
                    meta = store.read_meta(run_id)
                    meta.pop("output_language")
                    store.write_json(store.meta_path(run_id), meta)
                    await phase("chat", {"message": "老记录再问"}, "简体中文（Simplified Chinese）")
                    for name, payload in [("recon", {}), ("locate", {"repo_url": str(repo)}), ("chat", {"message": "x"})]:
                        response = await client.post(f"{base}/{name}", json={"provider": PROVIDER, "output_language": "invalid", **payload})
                        self.assertEqual(response.status_code, 422)
                    invalid = await client.post("/api/runs", files={"file": ("paper.pdf", pdf.read_bytes(), "application/pdf")}, data={"provider": json.dumps(PROVIDER), "output_language": "invalid"})
                    self.assertEqual(invalid.status_code, 422)
