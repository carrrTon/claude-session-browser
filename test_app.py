import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app


class TerminalResumeTests(unittest.TestCase):
    def test_open_session_in_terminal_preserves_unicode_paths_in_applescript(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            project_dir = root / "-demo"
            project_dir.mkdir(parents=True)
            cwd = Path(tmp) / "线下生鲜订货" / "供应商订货"
            cwd.mkdir(parents=True)
            session_file = project_dir / "session.jsonl"
            session_file.write_text(
                json.dumps({"type": "summary", "summary": "demo"}, ensure_ascii=False) + "\n"
                + json.dumps({"cwd": str(cwd), "sessionId": "abc-123"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            with mock.patch.object(app, "ROOT", root), mock.patch.object(app.subprocess, "run") as run:
                app.open_session_in_terminal(str(session_file))

            script = run.call_args.args[0][2]
            self.assertIn(str(cwd), script)
            self.assertNotIn("\\u", script)

    def test_read_session_uses_latest_cwd_update_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_file = Path(tmp) / "session.jsonl"
            old_cwd = Path(tmp) / "missing"
            new_cwd = Path(tmp) / "new"
            session_file.write_text(
                json.dumps({"cwd": str(old_cwd), "sessionId": "abc-123"}, ensure_ascii=False) + "\n"
                + json.dumps({"type": "cwd-update", "cwd": str(new_cwd)}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            session = app.read_session(session_file)

            self.assertEqual(session["cwd"], str(new_cwd))

    def test_open_session_in_terminal_reports_missing_cwd_without_launching_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            project_dir = root / "-demo"
            project_dir.mkdir(parents=True)
            missing_cwd = Path(tmp) / "missing"
            session_file = project_dir / "session.jsonl"
            session_file.write_text(
                json.dumps({"cwd": str(missing_cwd), "sessionId": "abc-123"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            with mock.patch.object(app, "ROOT", root), mock.patch.object(app.subprocess, "run") as run:
                result = app.open_session_in_terminal(str(session_file))

            self.assertEqual(result, {"ok": False, "missingCwd": str(missing_cwd), "canChooseDirectory": True})
            run.assert_not_called()

    def test_project_dirname_for_cwd_matches_claude_code_sanitized_project_key(self):
        cwd = "/Users/f/Documents/CODE/PRODUCE/线下生鲜订货/claude_gpt"

        self.assertEqual(app.project_dirname_for_cwd(cwd), "-Users-f-Documents-CODE-PRODUCE--------claude-gpt")

    def test_update_session_cwd_migrates_entire_project_and_updates_all_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            project_dir = root / "-old-project"
            nested_dir = project_dir / "nested"
            nested_dir.mkdir(parents=True)
            new_cwd = Path(tmp) / "new-cwd"
            new_cwd.mkdir()
            selected_file = project_dir / "selected.jsonl"
            sibling_file = nested_dir / "sibling.jsonl"
            note_file = project_dir / "memory.txt"
            selected_file.write_text(
                json.dumps({"cwd": str(Path(tmp) / "old"), "sessionId": "selected"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            sibling_file.write_text(
                json.dumps({"cwd": str(Path(tmp) / "old"), "sessionId": "sibling"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            note_file.write_text("project note\n", encoding="utf-8")

            with mock.patch.object(app, "ROOT", root):
                result = app.update_session_cwd(str(selected_file), str(new_cwd))

            expected_cwd = str(new_cwd.resolve())
            migrated_project = (root / app.project_dirname_for_cwd(expected_cwd)).resolve()
            migrated_selected = migrated_project / "selected.jsonl"
            migrated_sibling = migrated_project / "nested" / "sibling.jsonl"
            migrated_note = migrated_project / "memory.txt"
            selected_records = [json.loads(line) for line in migrated_selected.read_text(encoding="utf-8").splitlines()]
            sibling_records = [json.loads(line) for line in migrated_sibling.read_text(encoding="utf-8").splitlines()]

            self.assertEqual(result["ok"], True)
            self.assertEqual(result["cwd"], expected_cwd)
            self.assertEqual(result["file"], str(migrated_selected))
            self.assertEqual(result["projectDir"], str(migrated_project))
            self.assertEqual(result["migratedFiles"], 3)
            self.assertEqual(result["migratedSessions"], 2)
            self.assertFalse(project_dir.exists())
            self.assertTrue(Path(result["archivedProjectDir"]).exists())
            metadata = json.loads((migrated_project / ".claude-session-browser-migration.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["oldProjectDir"], str(project_dir.resolve()))
            self.assertEqual(metadata["newProjectDir"], str(migrated_project))
            self.assertEqual(metadata["newCwd"], expected_cwd)
            self.assertEqual(metadata["selectedSession"], "selected.jsonl")
            self.assertEqual(migrated_note.read_text(encoding="utf-8"), "project note\n")
            self.assertEqual(selected_records[-1]["type"], "cwd-update")
            self.assertEqual(selected_records[-1]["cwd"], expected_cwd)
            self.assertEqual(sibling_records[-1]["type"], "cwd-update")
            self.assertEqual(sibling_records[-1]["cwd"], expected_cwd)

    def test_update_session_cwd_uses_sanitized_project_key_for_new_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            project_dir = root / "-old-project"
            project_dir.mkdir(parents=True)
            new_cwd = Path(tmp) / "【线下生鲜智能订货】" / "claude_gpt" / "供应商订货"
            new_cwd.mkdir(parents=True)
            session_file = project_dir / "session.jsonl"
            session_file.write_text(
                json.dumps({"cwd": str(Path(tmp) / "old"), "sessionId": "abc-123"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            with mock.patch.object(app, "ROOT", root):
                result = app.update_session_cwd(str(session_file), str(new_cwd))

            expected_cwd = str(new_cwd.resolve())
            expected_file = (root / app.project_dirname_for_cwd(expected_cwd) / session_file.name).resolve()
            self.assertEqual(result["file"], str(expected_file))
            self.assertTrue(expected_file.exists())

    def test_update_session_cwd_rejects_different_destination_file_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            project_dir = root / "-old-project"
            project_dir.mkdir(parents=True)
            new_cwd = Path(tmp) / "new-cwd"
            new_cwd.mkdir()
            session_file = project_dir / "session.jsonl"
            session_file.write_text(
                json.dumps({"cwd": str(Path(tmp) / "old"), "sessionId": "abc-123"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            destination_project = root / app.project_dirname_for_cwd(str(new_cwd.resolve()))
            destination_project.mkdir(parents=True)
            colliding_file = destination_project / "session.jsonl"
            colliding_file.write_text("different content\n", encoding="utf-8")

            with mock.patch.object(app, "ROOT", root):
                with self.assertRaisesRegex(ValueError, "目标项目中已存在不同内容的文件"):
                    app.update_session_cwd(str(session_file), str(new_cwd))

            self.assertEqual(colliding_file.read_text(encoding="utf-8"), "different content\n")

    def test_update_session_cwd_updates_all_sessions_when_project_already_matches_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            cwd = Path(tmp) / "new-cwd"
            cwd.mkdir()
            project_dir = root / app.project_dirname_for_cwd(str(cwd.resolve()))
            project_dir.mkdir(parents=True)
            selected_file = project_dir / "selected.jsonl"
            sibling_file = project_dir / "sibling.jsonl"
            selected_file.write_text(
                json.dumps({"cwd": str(Path(tmp) / "old"), "sessionId": "selected"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            sibling_file.write_text(
                json.dumps({"cwd": str(Path(tmp) / "old"), "sessionId": "sibling"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            with mock.patch.object(app, "ROOT", root):
                result = app.update_session_cwd(str(selected_file), str(cwd))

            expected_cwd = str(cwd.resolve())
            selected_records = [json.loads(line) for line in selected_file.read_text(encoding="utf-8").splitlines()]
            sibling_records = [json.loads(line) for line in sibling_file.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(result["file"], str(selected_file.resolve()))
            self.assertEqual(result["migratedSessions"], 2)
            self.assertEqual(selected_records[-1]["cwd"], expected_cwd)
            self.assertEqual(sibling_records[-1]["cwd"], expected_cwd)

    def test_choose_folder_returns_selected_posix_path(self):
        completed = subprocess.CompletedProcess(["osascript"], 0, stdout="/tmp/demo folder\n", stderr="")
        with mock.patch.object(app.subprocess, "run", return_value=completed):
            result = app.choose_working_directory()

        self.assertEqual(result, "/tmp/demo folder")
class EmbeddedJavaScriptTests(unittest.TestCase):
    def test_open_in_terminal_handles_missing_cwd_with_directory_choice(self):
        self.assertIn("payload.canChooseDirectory&&payload.missingCwd", app.HTML)
        self.assertIn("confirm(`原工作目录不存在：\\n${payload.missingCwd}\\n\\n是否选择新的工作目录？`)", app.HTML)
        self.assertIn("fetch('/api/choose-working-directory'", app.HTML)
        self.assertIn("fetch('/api/update-session-cwd'", app.HTML)
        self.assertIn("if(updatePayload.file){selectedNode=findNode(updatePayload.file)", app.HTML)
        self.assertIn("await openInTerminal()", app.HTML)


if __name__ == "__main__":
    unittest.main()
