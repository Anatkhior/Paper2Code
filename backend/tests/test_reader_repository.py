"""阅读接口在定位未结束时也必须使用 repo_ready 记录的 git 版本。"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from app import main, store
from app.config import settings
from tests.repo_fixture import build_repo


class ReaderRepositoryTest(unittest.IsolatedAsyncioTestCase):
    async def test_repository_lifecycle_and_replay(self):
        run_id = "reader-repository-test"
        source = build_repo(Path(__file__).resolve().parents[1])
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
        with tempfile.TemporaryDirectory() as folder, patch.object(settings, "data_dir", Path(folder)):
            store.write_meta(run_id, phase="locate")
            directory = store.run_dir(run_id)
            shutil.copytree(source, directory / "repo")
            run = main._registry(run_id)
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
                    endpoint = f"/api/runs/{run_id}/file"
                    params = {"path": "loralib/layers.py", "focus_start": 37, "focus_end": 43}
                    await run.bus.emit("repo_cloning", url=str(source))
                    self.assertEqual((await client.get(endpoint, params=params)).status_code, 409)
                    await run.bus.emit("repo_ready", repo={"commit_sha": commit, "url": "https://github.com/example/repo"})
                    self.assertFalse((directory / "artifact.json").exists())
                    response = await client.get(endpoint, params=params)
                    self.assertEqual(response.status_code, 200, response.text)
                    view = response.json()
                    self.assertEqual(view["commit_sha"], commit)
                    self.assertEqual(len(view["lines"]), view["total_lines"])
                    self.assertEqual(view["focus"], {"start": 37, "end": 43})
                    self.assertIn(commit, view["source_url"])
                    for focus_start, focus_end in [(1995, 2010), (1000, 2400), (2200, 2210), (501, 2500)]:
                        with self.subTest(focus_start=focus_start, focus_end=focus_end):
                            large = await client.get(endpoint, params={
                                "path": "utils/generated_tables.py", "focus_start": focus_start, "focus_end": focus_end,
                            })
                            self.assertEqual(large.status_code, 200, large.text)
                            window = large.json()
                            self.assertLessEqual(window["line_start"], focus_start)
                            self.assertGreaterEqual(window["line_end"], focus_end)
                            self.assertLessEqual(len(window["lines"]), main.MAX_VIEW_LINES)
                            self.assertEqual(window["focus"], {"start": focus_start, "end": focus_end})

                    # 最终产物可能尚未写入，也可能属于上次定位；当前 git 版本只读事件。
                    store.write_json(directory / "artifact.json", {"run": {"repo": {"commit_sha": "obsolete"}}})
                    main.RUNS.pop(run_id)
                    replayed = await client.get(endpoint, params=params)
                    self.assertEqual(replayed.status_code, 200, replayed.text)
                    self.assertEqual(replayed.json()["commit_sha"], commit)
                    tools, _, repo = main._chat_tools(run_id)
                    self.assertIsNotNone(repo)
                    self.assertEqual(repo.commit_sha, commit)
                    self.assertTrue(any(tool.name == "read_file" for tool in tools))

                    # 新一轮克隆覆盖目录时不能回读上轮的 repo_ready / artifact。
                    await main._registry(run_id).bus.emit("repo_cloning", url=str(source))
                    self.assertEqual((await client.get(endpoint, params=params)).status_code, 409)
            finally:
                main.RUNS.pop(run_id, None)


if __name__ == "__main__":
    unittest.main()
