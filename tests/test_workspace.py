
import ast
import base64
from contextlib import closing
from datetime import datetime, timezone, timedelta
from io import BytesIO
import html
import json
import math
import re
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fal_backend import ProviderError
import sqlite3
import tempfile
import unittest
import uuid
import zipfile

APP = Path(__file__).resolve().parents[1] / "app.py"

def storage_functions(directory):
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    functions = ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef) or (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in ("BRIEF_RULES", "ISSUES", "BASES", "PRESENTATION_STYLES", "OUTCOME_RATINGS") for t in n.targets))], type_ignores=[])
    env = dict(Path=Path, sqlite3=sqlite3, closing=closing, datetime=datetime, timezone=timezone,
               uuid=uuid, BytesIO=BytesIO, zipfile=zipfile, json=json, html=html, base64=base64,
               re=re, math=math, timedelta=timedelta, ProviderError=ProviderError, NOTICE="AI-GENERATED ILLUSTRATION - NOT VERIFIED EVIDENCE",
               DATA_DIR=Path(directory), DB_PATH=Path(directory) / "workspace.sqlite3")
    exec(compile(functions, str(APP), "exec"), env)
    return env

class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=APP.parent, ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.env = storage_functions(self.temp.name)
        self.call("init_db")
        self.case = self.call("create_case", "Market 'quoted' case")
        self.other = self.call("create_case", "Other")

    def call(self, function, *args):
        return self.env[function](*args)

    def revision(self, parent=None):
        return self.call("save_revision", self.case, "Face sketch", "<script>not markup</script>",
                         "Exact prompt", 42, b"test image", parent)

    def test_persistence_and_case_isolation(self):
        first = self.revision()
        second = self.revision(first)
        self.call("init_db")
        self.assertEqual(self.call("get_revision", self.case, second)["parent_id"], first)
        self.assertIsNone(self.call("get_revision", self.other, first))
        self.assertEqual(self.call("list_revisions", self.other), [])
        with self.assertRaises(ValueError):
            self.call("save_revision", self.other, "Face sketch", "x", "x", 1, b"x", first)

    def test_drafts_notes_and_status(self):
        self.call("save_draft", self.case, "Face sketch", "Curly hair", {"Hair": "Curly"})
        self.assertEqual(self.call("get_draft", self.case, "Face sketch")["fields"]["Hair"], "Curly")
        self.assertEqual(self.call("get_draft", self.case, "Scene illustration")["description"], "")
        revision = self.revision()
        self.call("save_notes", self.case, revision, "Glasses missing")
        self.call("set_review_status", revision, "Needs revision")
        self.assertEqual(self.call("get_review_status", revision), "Needs revision")
        with self.assertRaises(ValueError):
            self.call("set_review_status", revision, "Verified identity")
        self.assertEqual(self.call("get_revision", self.case, revision)["notes"], "Glasses missing")

    def test_archive_rename_and_restore(self):
        self.call("rename_case", self.case, "New name")
        self.call("set_case_archived", self.case, True)
        self.call("init_db")
        self.assertTrue(self.call("case_is_archived", self.case))
        self.assertFalse(self.call("case_is_archived", self.other))
        self.call("set_case_archived", self.case, False)
        self.assertFalse(self.call("case_is_archived", self.case))
        self.assertIn("New name", [c["name"] for c in self.call("list_cases")])
        with self.assertRaises(ValueError):
            self.call("rename_case", self.case, "  ")

    def test_exports_escape_input_and_include_metadata(self):
        revision = self.revision()
        self.call("set_review_status", revision, "Reviewed")
        self.call("save_notes", self.case, revision, "Checked features")
        report = self.call("export_report", self.case, "<script>name</script>").decode()
        self.assertNotIn("<script>", report)
        self.assertIn("&lt;script&gt;", report)
        self.assertIn("Reviewed", report)
        self.assertIn("Exact prompt", report)
        with zipfile.ZipFile(BytesIO(self.call("export_case", self.case, "Example"))) as archive:
            metadata = json.loads(archive.read("case.json"))
            self.assertEqual(metadata["revisions"][0]["review_status"], "Reviewed")
            self.assertEqual(archive.read("images/" + revision + ".png"), b"test image")
            self.assertIn("case_report.html", archive.namelist())

    def test_database_backup(self):
        revision = self.revision()
        path = Path(self.temp.name) / "snapshot.sqlite3"
        path.write_bytes(self.call("database_backup"))
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("SELECT image FROM revisions WHERE id=?", (revision,)).fetchone()[0], b"test image")

    def test_upgrade_keeps_old_rows_and_creates_backup(self):
        self.revision()
        with closing(sqlite3.connect(self.env["DB_PATH"])) as db, db:
            db.execute("DROP TABLE drafts")
        self.call("init_db")
        self.assertTrue(list(Path(self.temp.name).glob("pre_upgrade_*.sqlite3")))
        self.assertEqual(len(self.call("list_revisions", self.case)), 1)


    def test_preferred_versions_are_case_and_mode_scoped(self):
        revision = self.revision()
        self.call("set_preferred", self.case, "Face sketch", revision)
        self.assertEqual(self.call("get_preferred", self.case, "Face sketch"), revision)
        with self.assertRaises(ValueError):
            self.call("set_preferred", self.other, "Face sketch", revision)
        with self.assertRaises(ValueError):
            self.call("set_preferred", self.case, "Scene illustration", revision)
        self.assertIsNone(self.call("get_preferred", self.case, "Scene illustration"))
        self.call("set_preferred", self.case, "Face sketch", None)
        self.assertIsNone(self.call("get_preferred", self.case, "Face sketch"))

    def test_review_is_atomic_and_scoped(self):
        revision = self.revision()
        self.call("persist_review", self.case, revision, "Missing moustache", "Needs revision")
        self.assertEqual(self.call("get_review_status", revision), "Needs revision")
        with self.assertRaises(ValueError):
            self.call("persist_review", self.other, revision, "Wrong case", "Reviewed")
        self.assertEqual(self.call("get_revision", self.case, revision)["notes"], "Missing moustache")

    def test_error_messages_do_not_echo_remote_tokens(self):
        message = self.call("generation_error_message", RuntimeError("401 token hf_private_example"))
        self.assertNotIn("hf_private_example", message)
        self.assertIn("LOGIN.cmd", message)
        zero_retry = self.call("generation_error_message", RuntimeError("ZeroGPU quota. Try again in 0:00:00."))
        self.assertNotIn("0:00:00", zero_retry)
        long_prompt = self.call("generation_error_message", RuntimeError("max_sequence length exceeded"))
        self.assertIn("Shorten", long_prompt)
        self.assertNotIn("usage limit", long_prompt)



    def test_brief_preserves_negation_and_uncertainty(self):
        text = "No people, cars or motorcycles were visible. The weather is unknown."
        brief = self.call("make_brief", text, "Scene illustration")
        self.assertIn("No people, cars or motorcycles were visible.", brief)
        self.assertIn("The weather is unknown.", brief)
        with self.assertRaises(ValueError):
            self.call("make_brief", "", "Face sketch")

    def test_refinement_exact_replacement_and_user_review(self):
        candidate = self.call("suggest_description", "Short hair and no beard.", "a thick beard", "no beard")
        self.assertEqual(candidate, "Short hair and a thick beard.")
        with self.assertRaises(ValueError):
            self.call("suggest_description", "Short hair.", "Bald head", "not in text")
        with self.assertRaises(ValueError):
            self.call("suggest_description", "Short hair.", "")

    def test_feedback_and_account_records_are_separate(self):
        revision = self.revision()
        account = self.call("save_account", self.case, "Face sketch", "No glasses, hair unknown.")
        self.call("add_provenance", self.case, account, "Hair colour", "Not supplied in the account")
        self.call("save_feedback", self.case, revision, account, "Wrong feature or object",
                  "Image has glasses", "No glasses", "Explicit in the account", "No glasses.")
        self.assertEqual(self.call("list_accounts", self.case)[0]["text"], "No glasses, hair unknown.")
        self.assertEqual(self.call("refinement_records", self.case)[0]["observed"], "Image has glasses")
        self.assertEqual(self.call("list_provenance", self.case)[0]["basis"], "Not supplied in the account")
        with self.assertRaises(ValueError):
            self.call("add_provenance", self.other, account, "Glasses", "Explicit in the account")
        with zipfile.ZipFile(BytesIO(self.call("export_case", self.case, "Refinement case"))) as archive:
            metadata = json.loads(archive.read("case.json"))
            self.assertEqual(metadata["account_records"][0]["text"], "No glasses, hair unknown.")
            self.assertEqual(len(metadata["refinement_feedback"]), 1)
            self.assertIn(b"No glasses, hair unknown.", archive.read("case_report.html"))



    def test_memory_is_opt_in_and_rejects_fact_preferences(self):
        preferences = self.call("load_preferences")
        self.assertFalse(preferences["enabled"])
        with self.assertRaises(ValueError):
            self.call("save_preferences", dict(preferences, facial_features="curly hair"))
        preferences.update(enabled=True, style="Clean ink outlines", white_margin=True)
        self.call("save_preferences", preferences)
        description = "A person has a tattoo reading Black-and-white pencil."
        prompt, snapshot = self.call("make_generation_prompt", description, "Face sketch")
        self.assertIn("Black-and-white ink", prompt)
        self.assertIn(description, prompt)
        self.assertEqual(snapshot["applied_presentation"], ["Clean ink outlines", "White margin"])

    def test_learning_requires_opt_in_and_never_reuses_account_text(self):
        parent = self.revision()
        child = self.call("save_revision", self.case, "Face sketch", "private child", "prompt", 42, b"image", parent)
        feedback = self.call("save_feedback", self.case, parent, None, "Wrong position",
                             "private account detail", "private requested correction",
                             "Explicit in the account", "private candidate")
        self.call("save_outcome", self.case, feedback, child, "Improved requested detail", False)
        self.assertEqual(self.call("pattern_suggestions", "Face sketch", self.other), [])
        self.call("save_outcome", self.case, feedback, child, "Improved requested detail", True)
        suggestions = self.call("pattern_suggestions", "Face sketch", self.other)
        self.assertEqual(suggestions[0]["helpful"], 1)
        self.assertNotIn("private", json.dumps(suggestions))
        self.assertEqual(self.call("pattern_suggestions", "Face sketch", self.case), [])
        self.assertEqual(self.call("pattern_suggestions", "Scene illustration", self.other), [])
        self.call("save_outcome", self.case, feedback, child, "Improved requested detail", True)
        self.assertEqual(self.call("pattern_suggestions", "Face sketch", self.other)[0]["helpful"], 1)
        self.call("reset_cross_case_learning")
        self.assertEqual(self.call("pattern_suggestions", "Face sketch", self.other), [])
        self.assertEqual(len(self.call("list_outcomes", self.case)), 1)

    def test_outcome_requires_a_real_child_revision(self):
        parent = self.revision()
        unrelated = self.revision()
        feedback = self.call("save_feedback", self.case, parent, None, "Missing detail", "No hair", "Curly hair",
                             "Explicit in the account", "Curly hair")
        with self.assertRaises(ValueError):
            self.call("save_outcome", self.case, feedback, unrelated, "Improved requested detail", True)
        with self.assertRaises(ValueError):
            self.call("save_outcome", self.other, feedback, unrelated, "Improved requested detail", True)

    def test_memory_snapshot_is_saved_and_exported_per_revision(self):
        snapshot = {"enabled": True, "applied_presentation": ["White margin"]}
        revision = self.env["save_revision"](self.case, "Face sketch", "An adult", "Exact prompt", 42,
                                              b"image", memory_snapshot=snapshot)
        self.assertEqual(self.call("revision_memory_snapshot", revision), snapshot)
        with zipfile.ZipFile(BytesIO(self.call("export_case", self.case, "Example"))) as archive:
            metadata = json.loads(archive.read("case.json"))
            self.assertEqual(metadata["revisions"][0]["memory_influence"], snapshot)
            self.assertNotIn("workspace_preferences", metadata)



    def test_ui_appearance_persists_without_changing_model_preferences(self):
        self.assertEqual(self.call("load_ui_theme"),"dark")
        self.call("save_ui_theme","light")
        self.call("init_db")
        self.assertEqual(self.call("load_ui_theme"),"light")
        self.assertFalse(self.call("load_preferences")["enabled"])
        with self.assertRaises(ValueError):
            self.call("save_ui_theme","invalid")

if __name__ == "__main__":
    unittest.main()
