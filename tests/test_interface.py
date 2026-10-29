
import os
import re
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image
from streamlit.testing.v1 import AppTest
from test_workspace import storage_functions
from auth_store import AuthStore

APP = Path(__file__).resolve().parents[1] / "app.py"

def find(elements, label):
    return next(element for element in elements if element.label == label)

class InterfaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=APP.parent, ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.env_patch = patch.dict(os.environ, {"CRIMESKETCH_DATA_DIR": str(self.directory)})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.image_path = self.directory / "mock.png"
        Image.new("RGB", (768, 768), "white").save(self.image_path)
        self.client_patch = patch("gradio_client.Client")
        client = self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        client.return_value.submit.return_value.result.return_value = (str(self.image_path), 42)
        self.client = client
        self.auth=AuthStore(self.directory)
        self.auth.create_user("test-owner","Test owner","test-owner-passphrase-2026",first=True)
        self.auth_token=self.auth.login("test-owner","test-owner-passphrase-2026")
        self.app = AppTest.from_file(str(APP), default_timeout=20)
        self.app.session_state["login_session"]=self.auth_token
        self.app.session_state["generation_provider"] = "Hugging Face demo"
        self.app.run()
        self.assertFalse(self.app.exception)

    def create(self, name):
        find(self.app.text_input, "New case name").input(name)
        find(self.app.button, "Create case").click().run()
        self.assertFalse(self.app.exception)

    def test_empty_and_long_inputs_do_not_call_provider(self):
        self.create("Input checks")
        find(self.app.button, "Get Image").click().run()
        self.assertFalse(self.app.exception)
        self.assertTrue(self.app.warning)
        find(self.app.text_area, "Description to generate").input("x" * 901).run()
        find(self.app.button, "Get Image").click().run()
        self.client.assert_not_called()
        self.assertFalse(self.app.exception)

    def test_generate_review_revise_compare_export(self):
        self.create("Full workflow")
        find(self.app.text_area, "Description to generate").input("A shop beside a road").run()
        find(self.app.button, "Get Image").click().run()
        self.assertFalse(self.app.exception)
        env = storage_functions(self.directory)
        case = env["list_cases"]()[0]["id"]
        self.assertEqual(len(env["list_revisions"](case)), 1)
        find(self.app.text_area, "Review notes").input("Shop placement needs adjustment")
        find(self.app.selectbox, "Review status").select("Needs revision")
        find(self.app.button, "Save review notes").click().run()
        self.assertFalse(self.app.exception)
        find(self.app.button, "Use this description and seed for a revision").click().run()
        self.assertFalse(self.app.exception)
        find(self.app.text_area, "Description to generate").input("A shop on the left beside a road").run()
        find(self.app.button, "Get Image").click().run()
        self.assertFalse(self.app.exception)
        rows = env["list_revisions"](case)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["parent_id"], rows[1]["id"])
        self.assertNotEqual(find(self.app.selectbox, "First version").value,
                            find(self.app.selectbox, "Second version").value)
        find(self.app.button, "Prepare case export").click().run()
        self.assertFalse(self.app.exception)
        self.assertTrue(self.app.session_state["export_" + case])

    def test_guided_fields_drafts_and_mode_switching(self):
        self.create("Guided input")
        find(self.app.radio, "Output mode").set_value("Face sketch").run()
        find(self.app.radio, "Description entry").set_value("Guided fields").run()
        find(self.app.text_input, "Hair").input("Short curly hair").run()
        find(self.app.button, "Use fields in description").click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(find(self.app.text_area, "Description to generate").value, "Hair: Short curly hair")
        find(self.app.radio, "Output mode").set_value("Scene illustration").run()
        self.assertEqual(find(self.app.text_area, "Description to generate").value, "")
        find(self.app.radio, "Output mode").set_value("Face sketch").run()
        self.assertEqual(find(self.app.text_area, "Description to generate").value, "Hair: Short curly hair")
        restarted = AppTest.from_file(str(APP), default_timeout=20)
        restarted.session_state["login_session"]=self.auth_token
        restarted.session_state["generation_provider"] = "Hugging Face demo"
        restarted.run()
        find(restarted.radio, "Output mode").set_value("Face sketch").run()
        self.assertEqual(find(restarted.text_area, "Description to generate").value, "Hair: Short curly hair")
        self.assertFalse(restarted.exception)

    def test_archive_restore_and_new_case_after_search(self):
        self.create("First case")
        find(self.app.text_input, "Case name").input("Renamed case")
        find(self.app.button, "Save case name").click().run()
        self.assertFalse(self.app.exception)
        find(self.app.button, "Archive case").click().run()
        self.assertFalse(self.app.exception)
        find(self.app.checkbox, "Show archived cases").check().run()
        self.assertTrue(find(self.app.button, "Get Image").disabled)
        find(self.app.button, "Restore case").click().run()
        self.assertFalse(find(self.app.button, "Get Image").disabled)
        find(self.app.text_input, "Search cases").input("unmatched").run()
        self.create("Second case")
        self.assertEqual(find(self.app.text_input, "Search cases").value, "")
        self.assertFalse(self.app.exception)

    def test_quota_failure_preserves_draft_and_history(self):
        self.create("Quota handling")
        find(self.app.text_area, "Description to generate").input("Curly hair").run()
        self.client.return_value.submit.return_value.result.side_effect = RuntimeError(
            "You have exceeded your ZeroGPU quota. Try again in 0:05:00.")
        find(self.app.button, "Get Image").click().run()
        self.assertFalse(self.app.exception)
        self.assertTrue(any("0:05:00" in error.value for error in self.app.error))
        self.assertEqual(find(self.app.text_area, "Description to generate").value, "Curly hair")


    def test_clear_example_and_undo_preserve_description(self):
        self.create("Draft tools")
        find(self.app.text_area, "Description to generate").input("My custom description").run()
        find(self.app.button, "Clear draft").click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(find(self.app.text_area, "Description to generate").value, "")
        find(self.app.button, "Undo draft replacement").click().run()
        self.assertEqual(find(self.app.text_area, "Description to generate").value, "My custom description")
        find(self.app.button, "Use this example").click().run()
        self.assertIn("grocery shop", find(self.app.text_area, "Description to generate").value)
        find(self.app.button, "Undo draft replacement").click().run()
        self.assertEqual(find(self.app.text_area, "Description to generate").value, "My custom description")

    def test_duplicate_request_reopens_saved_result(self):
        self.create("Avoid wasted quota")
        find(self.app.text_area, "Description to generate").input("Shop beside a road").run()
        find(self.app.button, "Get Image").click().run()
        find(self.app.button, "Get Image").click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.client.return_value.submit.call_count, 1)
        self.assertTrue(any("without making a new generation" in success.value for success in self.app.success))
        find(self.app.checkbox, "Regenerate even when these exact settings already have a saved result").check().run()
        find(self.app.button, "Get Image").click().run()
        self.assertEqual(self.client.return_value.submit.call_count, 2)
        self.assertFalse(self.app.exception)

    def test_preference_and_history_filters(self):
        self.create("History tools")
        find(self.app.text_area, "Description to generate").input("Shop beside a road").run()
        find(self.app.button, "Get Image").click().run()
        find(self.app.button, "Mark as preferred for this mode").click().run()
        self.assertFalse(self.app.exception)
        env = storage_functions(self.directory)
        case = env["list_cases"]()[0]["id"]
        preferred = env["get_preferred"](case, "Scene illustration")
        self.assertTrue(preferred)
        find(self.app.selectbox, "Review filter").select("Reviewed").run()
        self.assertFalse(self.app.exception)
        self.assertFalse(any(box.label == "Saved version" for box in self.app.selectbox))
        find(self.app.selectbox, "Review filter").select("All statuses").run()
        self.assertTrue(any(box.label == "Saved version" for box in self.app.selectbox))

    def test_reuse_turns_off_random_seed(self):
        self.create("Reproducible revision")
        find(self.app.text_area, "Description to generate").input("Shop beside a road").run()
        find(self.app.button, "Get Image").click().run()
        find(self.app.checkbox, "Use a random seed").check().run()
        find(self.app.button, "Use this description and seed for a revision").click().run()
        self.assertFalse(find(self.app.checkbox, "Use a random seed").value)
        self.assertEqual(find(self.app.number_input, "Seed").value, 42)

    def test_unsaved_image_can_be_saved_without_regeneration(self):
        # Inject a disk failure only at the first generated-image persistence call.
        text = APP.read_text(encoding="utf-8")
        text = re.sub(r"new_id = save_revision\([^\n]*\)",
                      'raise sqlite3.OperationalError("Injected disk failure")', text, count=1)
        injected_app = self.directory / "injected_app.py"
        injected_app.write_text(text, encoding="utf-8")
        self.app = AppTest.from_file(str(injected_app), default_timeout=20)
        self.app.session_state["login_session"]=self.auth_token
        self.app.session_state["generation_provider"] = "Hugging Face demo"
        self.app.run()
        self.create("Save recovery")
        find(self.app.text_area, "Description to generate").input("Shop beside a road").run()
        find(self.app.button, "Get Image").click().run()
        self.assertFalse(self.app.exception)
        self.assertTrue(find(self.app.button, "Get Image").disabled)
        find(self.app.button, "Retry saving image").click().run()
        self.assertFalse(self.app.exception)
        self.assertFalse(find(self.app.button, "Get Image").disabled)
        self.assertEqual(self.client.return_value.submit.call_count, 1)



    def test_get_brief_saves_text_without_calling_image_provider(self):
        self.create("Brief flow")
        description = "An adult with curly hair, no beard or glasses. The eye colour is unknown."
        find(self.app.radio, "Output mode").set_value("Face sketch").run()
        find(self.app.text_area, "Description to generate").input(description).run()
        find(self.app.button, "Get Brief").click().run()
        self.assertFalse(self.app.exception)
        self.client.assert_not_called()
        env = storage_functions(self.directory)
        case = env["list_cases"]()[0]["id"]
        brief = env["latest_brief"](case, "Face sketch")
        self.assertEqual(brief["description"], description)
        self.assertIn("no beard or glasses", brief["content"])
        self.assertIn("unknown", brief["content"])

    def test_feedback_builds_linked_revision_without_retyping(self):
        self.create("Interactive refinement")
        original = "A motorcycle on right beside a shop."
        find(self.app.text_area, "Description to generate").input(original).run()
        find(self.app.button, "Get Image").click().run()
        env = storage_functions(self.directory)
        case = env["list_cases"]()[0]["id"]
        initial = env["list_revisions"](case)[0]["id"]
        find(self.app.selectbox, "What went wrong?").select("Wrong position").run()
        find(self.app.text_area, "What does this image show incorrectly?").input("Motorcycle is on the right")
        find(self.app.text_area, "What should the next image show instead?").input("on left")
        find(self.app.text_input, "Exact wording to replace (optional)").input("on right")
        find(self.app.button, "Save feedback and build revision").click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.client.return_value.submit.call_count, 1)
        self.assertEqual(find(self.app.text_area, "Revised description to review").value,
                         "A motorcycle on left beside a shop.")
        find(self.app.button, "Use revised description in Create").click().run()
        self.assertEqual(find(self.app.text_area, "Description to generate").value,
                         "A motorcycle on left beside a shop.")
        find(self.app.button, "Get Image").click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(env["list_revisions"](case)[0]["parent_id"], initial)
        self.assertEqual(env["get_revision"](case, initial)["description"], original)

    def test_preserved_account_and_uncertainty_ledger(self):
        self.create("Provenance")
        find(self.app.text_area, "Account text to preserve").input("A person with curly hair. Glasses unknown.")
        find(self.app.button, "Save new account record").click().run()
        self.assertFalse(self.app.exception)
        find(self.app.text_input, "Detail to classify").input("Glasses")
        find(self.app.selectbox, "Detail source").select("Not supplied in the account")
        find(self.app.button, "Add detail to ledger").click().run()
        self.assertFalse(self.app.exception)
        env = storage_functions(self.directory)
        case = env["list_cases"]()[0]["id"]
        self.assertEqual(len(env["list_accounts"](case)), 1)
        self.assertEqual(env["list_provenance"](case)[0]["basis"], "Not supplied in the account")



    def test_paid_generation_requires_key_and_explicit_authorization(self):
        self.app = AppTest.from_file(str(APP), default_timeout=20)
        self.app.session_state["login_session"]=self.auth_token
        self.app.run()
        self.create("Paid provider guard")
        find(self.app.selectbox, "Image provider").select("fal.ai API").run()
        self.assertEqual(find(self.app.selectbox, "Image provider").value, "fal.ai API")
        self.assertTrue(find(self.app.button, "Get Image").disabled)
        find(self.app.text_input, "fal API key (private)").input("test-only-not-a-real-key").run()
        self.assertTrue(find(self.app.button, "Get Image").disabled)
        find(self.app.text_area, "Description to generate").input("A fictional road").run()
        find(self.app.button, "Get Brief").click().run()
        self.assertFalse(self.app.exception)
        self.client.assert_not_called()
        find(self.app.checkbox, "I authorize paid image requests using this fal account").check().run()
        self.assertFalse(find(self.app.button, "Get Image").disabled)



    def test_paid_provider_mock_success_saves_attribution_without_key(self):
        from io import BytesIO
        buffer = BytesIO()
        Image.new("RGB", (768, 768), "white").save(buffer, format="PNG")
        self.create("Paid attribution")
        find(self.app.selectbox, "Image provider").select("fal.ai API").run()
        find(self.app.text_input, "fal API key (private)").input("private-dummy-key").run()
        find(self.app.checkbox, "I authorize paid image requests using this fal account").check().run()
        find(self.app.text_area, "Description to generate").input("A fictional shop and road").run()
        with patch("fal_backend.run_fal", return_value=(buffer.getvalue(), 42, "mock-paid-request")) as api:
            find(self.app.button, "Get Image").click().run()
        self.assertFalse(self.app.exception)
        api.assert_called_once()
        self.client.assert_not_called()
        env = storage_functions(self.directory)
        case = env["list_cases"]()[0]["id"]
        item = env["get_revision"](case, env["list_revisions"](case)[0]["id"])
        self.assertEqual(item["provider"], "fal.ai API")
        self.assertEqual(item["request_id"], "mock-paid-request")
        self.assertNotIn(b"private-dummy-key", env["database_backup"]())



    def test_editable_preferences_influence_prompt_and_record_why(self):
        self.create("Adaptive preferences")
        find(self.app.checkbox, "Apply saved presentation preferences").check()
        find(self.app.selectbox, "Preferred drawing style").select("Clean ink outlines")
        find(self.app.checkbox, "Prefer a small white margin").check()
        find(self.app.button, "Save memory settings").click().run()
        self.assertFalse(self.app.exception)
        find(self.app.text_area, "Description to generate").input("A shop on the left.").run()
        find(self.app.button, "Get Image").click().run()
        self.assertFalse(self.app.exception)
        arguments = self.client.return_value.submit.call_args.kwargs
        self.assertIn("Black-and-white ink", arguments["prompt"])
        self.assertIn("A shop on the left.", arguments["prompt"])
        env = storage_functions(self.directory)
        case = env["list_cases"]()[0]["id"]
        revision = env["list_revisions"](case)[0]["id"]
        self.assertEqual(env["revision_memory_snapshot"](revision)["applied_presentation"],
                         ["Clean ink outlines", "White margin"])

    def test_explicit_revision_outcome_can_inform_shared_patterns(self):
        self.create("Rated iteration")
        find(self.app.text_area, "Description to generate").input("A shop on the right.").run()
        find(self.app.button, "Get Image").click().run()
        find(self.app.text_area, "What does this image show incorrectly?").input("Wrong shop position")
        find(self.app.text_area, "What should the next image show instead?").input("on the left")
        find(self.app.text_input, "Exact wording to replace (optional)").input("on the right")
        find(self.app.button, "Save feedback and build revision").click().run()
        find(self.app.button, "Use revised description in Create").click().run()
        find(self.app.button, "Get Image").click().run()
        find(self.app.selectbox, "Did this revision improve the requested detail?").select("Improved requested detail")
        find(self.app.checkbox, "Allow this issue-category rating to inform other cases").check()
        find(self.app.button, "Save revision outcome").click().run()
        self.assertFalse(self.app.exception)
        env = storage_functions(self.directory)
        case = env["list_cases"]()[0]["id"]
        self.assertEqual(len(env["list_outcomes"](case)), 1)
        self.assertEqual(env["pattern_suggestions"]("Scene illustration")[0]["helpful"], 1)
        self.assertTrue(any("Version 2" in expander.label for expander in self.app.expander))



    def test_quota_countdown_and_brief_do_not_retry_generation(self):
        self.create("Visible quota status")
        find(self.app.text_area, "Description to generate").input("A fictional street").run()
        self.client.return_value.submit.return_value.result.side_effect = RuntimeError(
            "ZeroGPU quota exceeded (90s requested vs. 0s left). Try again in 0:05:00.")
        find(self.app.button, "Get Image").click().run()
        self.app.run()
        self.assertFalse(self.app.exception)
        metric = next(metric for metric in self.app.metric if metric.label == "Provider-estimated wait")
        self.assertTrue(metric.value.startswith("00:"))
        self.assertEqual(self.client.return_value.submit.call_count, 1)
        find(self.app.button, "Get Brief").click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.client.return_value.submit.call_count, 1)

    def test_zero_retry_estimate_is_explained_in_panel(self):
        self.create("Unknown reset")
        find(self.app.text_area, "Description to generate").input("A fictional street").run()
        self.client.return_value.submit.return_value.result.side_effect = RuntimeError(
            "ZeroGPU quota exceeded. Try again in 0:00:00.")
        find(self.app.button, "Get Image").click().run()
        self.app.run()
        self.assertFalse(self.app.exception)
        self.assertTrue(any("Reset time unavailable" in warning.value for warning in self.app.warning))
        self.assertFalse(any(metric.label == "Provider-estimated wait" for metric in self.app.metric))



    def test_professional_navigation_and_persistent_theme_toggle(self):
        self.create("Professional workspace")
        self.assertEqual([tab.label for tab in self.app.tabs[:3]],
                         ["Workspace","Review & Compare","Correct an image"])
        self.assertTrue(any(tab.label=="Case Records" for tab in self.app.tabs))
        find(self.app.toggle,"Light theme").set_value(True).run()
        self.assertFalse(self.app.exception)
        env=storage_functions(self.directory)
        self.assertEqual(env["load_ui_theme"](),"light")
        restarted=AppTest.from_file(str(APP),default_timeout=20)
        restarted.session_state["login_session"]=self.auth_token
        restarted.session_state["generation_provider"]="Hugging Face demo"
        restarted.run()
        self.assertTrue(find(restarted.toggle,"Light theme").value)
        find(restarted.toggle,"Light theme").set_value(False).run()
        self.assertEqual(env["load_ui_theme"](),"dark")
        self.client.assert_not_called()

    def test_focus_viewer_and_continue_refining_keep_saved_work(self):
        self.create("Focused review")
        find(self.app.text_area,"Description to generate").input("A fictional street.").run()
        find(self.app.button,"Get Image").click().run()
        find(self.app.toggle,"Focus image viewer").set_value(True).run()
        self.assertFalse(self.app.exception)
        find(self.app.button,"Continue refining this image").click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(find(self.app.text_area,"Description to generate").value,"A fictional street.")
        self.assertEqual(self.client.return_value.submit.call_count,1)

if __name__ == "__main__":
    unittest.main()
