"""Unit and integration tests for agent_team.py (standard library only).

Run with:

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent_team as at  # noqa: E402


def make_config(root: Path) -> at.Config:
    cfg = at.Config.load(root)
    cfg.root = root
    cfg.tasks_dir = root / "tasks"
    cfg.ref_dir = root / "ref"
    cfg.logs_dir = root / "logs"
    cfg.work_dir = root / "work"
    cfg.manager_prompt = root / "manager.md"
    cfg.worker_prompt = root / "worker.md"
    cfg.ensure_dirs()
    return cfg


class TempProject(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="agent-team-test-")
        self.root = Path(self._tmp.name)
        self.cfg = make_config(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class TestTaskParsing(TempProject):
    def test_front_matter_and_expects(self) -> None:
        path = self.cfg.tasks_dir / "P07.md"
        path.write_text(
            "---\n"
            "id: p07\n"
            "title: Build the thing\n"
            "expects:\n"
            "  - work/a.md\n"
            "  - work/img/*.png\n"
            "---\n\n# Build the thing\n\nDo it.\n",
            encoding="utf-8",
        )
        task = at.Task.from_file(path)
        self.assertEqual(task.id, "p07")
        self.assertEqual(task.title, "Build the thing")
        self.assertEqual(task.expects, ["work/a.md", "work/img/*.png"])
        self.assertIn("Do it.", task.prompt_block())
        self.assertEqual(task.log_path_name, "p07.log")

    def test_id_defaults_to_filename_and_title_to_heading(self) -> None:
        path = self.cfg.tasks_dir / "P02.md"
        path.write_text("# Refactor\n\nBody\n", encoding="utf-8")
        task = at.Task.from_file(path)
        self.assertEqual(task.id, "p02")
        self.assertEqual(task.title, "Refactor")
        self.assertEqual(task.expects, [])

    def test_comma_separated_expects(self) -> None:
        path = self.cfg.tasks_dir / "p03.md"
        path.write_text(
            "---\nid: p03\nexpects: work/a.md, work/b.md\n---\n\n# T\n",
            encoding="utf-8",
        )
        self.assertEqual(at.Task.from_file(path).expects, ["work/a.md", "work/b.md"])

    def test_discovery_is_naturally_sorted(self) -> None:
        for name in ("p10.md", "p02.md", "p01.md"):
            (self.cfg.tasks_dir / name).write_text(f"# {name}\n", encoding="utf-8")
        tasks = at.discover_tasks(self.cfg)
        self.assertEqual([t.id for t in tasks], ["p01", "p02", "p10"])

    def test_duplicate_ids_rejected(self) -> None:
        (self.cfg.tasks_dir / "p01.md").write_text("---\nid: same\n---\n# a\n", encoding="utf-8")
        (self.cfg.tasks_dir / "p02.md").write_text("---\nid: same\n---\n# b\n", encoding="utf-8")
        with self.assertRaises(SystemExit):
            at.discover_tasks(self.cfg)


class TestSandbox(TempProject):
    def setUp(self) -> None:
        super().setUp()
        self.sandbox = at.Sandbox(self.cfg.root, [self.cfg.work_dir, self.cfg.logs_dir])

    def test_relative_paths_resolve_inside_root(self) -> None:
        self.assertEqual(self.sandbox.resolve("work/x.png"), self.cfg.work_dir / "x.png")

    def test_escape_rejected(self) -> None:
        with self.assertRaises(at.ToolError):
            self.sandbox.resolve("../outside.txt")

    def test_write_outside_work_rejected(self) -> None:
        with self.assertRaises(at.ToolError):
            self.sandbox.resolve("ref/evil.txt", write=True)
        with self.assertRaises(at.ToolError):
            self.sandbox.resolve("agent_team.py", write=True)

    def test_write_inside_work_allowed(self) -> None:
        path = self.sandbox.resolve("work/sub/file.md", write=True)
        self.assertTrue(str(path).startswith(str(self.cfg.work_dir)))


class TestVerification(TempProject):
    def _task(self, expects: list[str] | None = None) -> at.Task:
        path = self.cfg.tasks_dir / "p01.md"
        path.write_text("# Task\n\nDo something.\n", encoding="utf-8")
        task = at.Task.from_file(path)
        task.expects = expects or []
        return task

    def test_missing_log_fails(self) -> None:
        task = self._task()
        (self.cfg.work_dir / "out.md").write_text("x", encoding="utf-8")
        gate = at.verify_task(self.cfg, task, {})
        self.assertFalse(gate.ok)
        self.assertTrue(any("missing" in r for r in gate.reasons))

    def test_short_log_fails(self) -> None:
        task = self._task()
        (self.cfg.logs_dir / "p01.log").write_text("done", encoding="utf-8")
        (self.cfg.work_dir / "out.md").write_text("x", encoding="utf-8")
        gate = at.verify_task(self.cfg, task, {})
        self.assertFalse(gate.ok)
        self.assertTrue(any("too small" in r for r in gate.reasons))

    def test_log_without_work_output_fails(self) -> None:
        task = self._task()
        (self.cfg.logs_dir / "p01.log").write_text("x" * 500, encoding="utf-8")
        gate = at.verify_task(self.cfg, task, {})
        self.assertFalse(gate.ok)
        self.assertTrue(any("no new or changed files" in r for r in gate.reasons))

    def test_unchanged_work_fails(self) -> None:
        task = self._task()
        (self.cfg.logs_dir / "p01.log").write_text("x" * 500, encoding="utf-8")
        existing = self.cfg.work_dir / "old.md"
        existing.write_text("old", encoding="utf-8")
        before = at.snapshot_dir(self.cfg.work_dir)
        gate = at.verify_task(self.cfg, task, before)
        self.assertFalse(gate.ok)

    def test_pass_with_log_and_new_work(self) -> None:
        task = self._task()
        (self.cfg.logs_dir / "p01.log").write_text("x" * 500, encoding="utf-8")
        (self.cfg.work_dir / "out.md").write_text("fresh", encoding="utf-8")
        gate = at.verify_task(self.cfg, task, {})
        self.assertTrue(gate.ok, gate.reasons)
        self.assertIn("out.md", gate.changed)

    def test_expected_deliverable_required(self) -> None:
        task = self._task(["work/report.md"])
        (self.cfg.logs_dir / "p01.log").write_text("x" * 500, encoding="utf-8")
        (self.cfg.work_dir / "other.md").write_text("x", encoding="utf-8")
        gate = at.verify_task(self.cfg, task, {})
        self.assertFalse(gate.ok)
        self.assertTrue(any("report.md" in r for r in gate.reasons))

    def test_expected_glob_satisfied(self) -> None:
        task = self._task(["work/images/*.png"])
        (self.cfg.logs_dir / "p01.log").write_text("x" * 500, encoding="utf-8")
        (self.cfg.work_dir / "images").mkdir(parents=True)
        (self.cfg.work_dir / "images" / "variant_01.png").write_bytes(b"\x89PNG\r\n")
        gate = at.verify_task(self.cfg, task, {})
        self.assertTrue(gate.ok, gate.reasons)


class TestHelpers(unittest.TestCase):
    def test_parse_arguments_handles_fences_and_prose(self) -> None:
        self.assertEqual(at._parse_arguments('{"a": 1}'), {"a": 1})
        self.assertEqual(at._parse_arguments('```json\n{"a": 2}\n```'), {"a": 2})
        self.assertEqual(at._parse_arguments('sure: {"a": 3} done'), {"a": 3})
        self.assertEqual(at._parse_arguments(None), {})

    def test_multipart_body_shape(self) -> None:
        body, content_type = at._multipart(
            {"model": "img-1", "prompt": "a cat", "n": 2},
            [("image", "m.png", b"\x89PNG", "image/png")],
        )
        self.assertIn("multipart/form-data; boundary=", content_type)
        text = body.decode("utf-8", "replace")
        self.assertIn('name="model"', text)
        self.assertIn('filename="m.png"', text)
        self.assertIn("a cat", text)
        self.assertTrue(body.endswith(b"--\r\n"))

    def test_extract_images_from_various_shapes(self) -> None:
        b64 = base64.b64encode(b"fake").decode()
        cases = [
            {"data": [{"b64_json": b64}]},
            {"choices": [{"message": {"images": [{"image_url": {"url": f"data:image/png;base64,{b64}"}}]}}]},
            {"choices": [{"message": {"content": f"![img](https://example.com/a.png)"}}]},
            {"choices": [{"message": {"content": [{"type": "image_url", "image_url": {"url": "https://example.com/b.png"}}]}}]},
        ]
        for payload in cases:
            found = at._extract_images(payload)
            self.assertTrue(found, payload)

    def test_text_of_flattens_parts(self) -> None:
        self.assertEqual(at._text_of("hi"), "hi")
        self.assertEqual(at._text_of([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]), "a\nb")

    def test_natural_key(self) -> None:
        names = ["p10.md", "p2.md", "p1.md"]
        self.assertEqual(sorted(names, key=lambda n: at.natural_key(Path(n))), ["p1.md", "p2.md", "p10.md"])

    def test_load_dotenv_does_not_override_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text('AT_TEST_A="quoted value"\nAT_TEST_B=bare # comment\n', encoding="utf-8")
            os.environ.pop("AT_TEST_A", None)
            os.environ.pop("AT_TEST_B", None)
            os.environ["AT_TEST_B"] = "from-env"
            try:
                at.load_dotenv(path)
                self.assertEqual(os.environ["AT_TEST_A"], "quoted value")
                self.assertEqual(os.environ["AT_TEST_B"], "from-env")
            finally:
                os.environ.pop("AT_TEST_A", None)
                os.environ.pop("AT_TEST_B", None)


class TestSimulatedWorkflow(TempProject):
    def _project(self) -> None:
        (self.root / "manager.md").write_text("# manager\n", encoding="utf-8")
        (self.root / "worker.md").write_text("# worker\n", encoding="utf-8")
        for name, title, expects in (
            ("p01", "First", "work/one.md"),
            ("p02", "Second", "work/images/*.png"),
        ):
            (self.cfg.tasks_dir / f"{name}.md").write_text(
                f"---\nid: {name}\ntitle: {title}\nexpects:\n  - {expects}\n---\n\n# {title}\n\nDo it.\n",
                encoding="utf-8",
            )

    def test_full_queue_runs_and_verifies(self) -> None:
        self._project()
        self.cfg.simulate = True
        reporter = at.Reporter(self.cfg.logs_dir, quiet=True)
        orchestrator = at.Orchestrator(self.cfg, reporter, at.ClientFactory(self.cfg, reporter))
        outcomes = orchestrator.run(at.discover_tasks(self.cfg))
        self.assertEqual([o.status for o in outcomes], ["done", "done"])
        self.assertTrue((self.cfg.logs_dir / "p01.log").is_file())
        self.assertTrue((self.cfg.logs_dir / "p02.log").is_file())
        self.assertTrue((self.cfg.work_dir / "one.md").is_file())
        self.assertTrue(list((self.cfg.work_dir / "images").glob("*.png")))

    def test_manager_pass_cannot_override_failed_gate(self) -> None:
        """An optimistic manager verdict must still be blocked by the gate."""

        class PassOnlyManager:
            def __init__(self) -> None:
                self.turns = 0

            def chat(self, messages, tools=None, **kwargs):
                self.turns += 1
                if self.turns % 2 == 1:
                    return at.tool_call_message("send_task_to_worker", {"instructions": "go"})
                return at.tool_call_message(
                    "finish_task", {"verdict": "pass", "notes": "looks fine to me"}
                )

        class LazyWorker:
            def __init__(self) -> None:
                self.runs = 0

            def chat(self, messages, tools=None, **kwargs):
                self.runs += 1
                return at.tool_call_message(
                    "finish_task", {"summary": "trust me, it is done", "status": "done"}
                )

        class Factory:
            def __init__(self, cfg: at.Config) -> None:
                self.cfg = cfg
                self.worker = LazyWorker()

            def __call__(self, role: str, task: at.Task):
                if role == "manager":
                    return PassOnlyManager()
                return self.worker

            def images(self):
                return at.ImageService(self.cfg.image, self.cfg.worker)

        (self.root / "manager.md").write_text("# manager\n", encoding="utf-8")
        (self.root / "worker.md").write_text("# worker\n", encoding="utf-8")
        (self.cfg.tasks_dir / "p01.md").write_text(
            "---\nid: p01\ntitle: Unverifiable\n---\n\n# Unverifiable\n", encoding="utf-8"
        )
        factory = Factory(self.cfg)
        reporter = at.Reporter(self.cfg.logs_dir, quiet=True)
        orchestrator = at.Orchestrator(self.cfg, reporter, factory)
        outcomes = orchestrator.run(at.discover_tasks(self.cfg))

        self.assertEqual(outcomes[0].status, "failed")
        self.assertEqual(outcomes[0].attempts, self.cfg.max_task_attempts)
        self.assertIn("log file", outcomes[0].notes)
        self.assertEqual(factory.worker.runs, self.cfg.max_task_attempts)


if __name__ == "__main__":
    unittest.main(verbosity=2)
