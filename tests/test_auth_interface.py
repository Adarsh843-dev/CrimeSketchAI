
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from streamlit.testing.v1 import AppTest
from test_workspace import APP,storage_functions
from auth_store import AuthStore

def find(elements,label):
    return next(item for item in elements if item.label==label)

class LoginInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=APP.parent,ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        env_patch=patch.dict(os.environ,{"CRIMESKETCH_DATA_DIR":str(self.root)})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.store=AuthStore(self.root)
        self.password="local-owner-passphrase-2026"
        self.env=storage_functions(self.root)
        self.env["init_db"]()
        self.env["create_case"]("Owner secret market")
        self.app=AppTest.from_file(str(APP),default_timeout=30)

    def setup_owner(self):
        self.store.create_user("owner","Owner",self.password,first=True)
        return self.store.login("owner",self.password)

    def test_guest_opens_directly_into_workspace(self):
        self.app.run()
        self.assertFalse(self.app.exception)
        self.assertTrue(any(x.label=="New case name" for x in self.app.text_input))
        self.assertFalse(any(x.label=="Username" for x in self.app.text_input))
        self.assertTrue(any(x.label=="Log in" for x in self.app.button))
        self.assertFalse(any("Owner secret market" in str(x.options) for x in self.app.selectbox))
        self.assertFalse(any("Bring descriptions into focus" in x.value for x in self.app.title))

    def test_optional_login_and_return_preserve_guest_workspace(self):
        self.setup_owner()
        self.app.run()
        guest=self.app.session_state["guest_workspace"]
        find(self.app.button,"Log in").click().run()
        self.assertTrue(any(x.label=="Username" for x in self.app.text_input))
        find(self.app.button,"Back to app").click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(guest,self.app.session_state["guest_workspace"])
        self.assertTrue(any(x.label=="New case name" for x in self.app.text_input))

    def test_login_and_logout_keep_owner_cases_private(self):
        self.setup_owner()
        self.app.run()
        find(self.app.button,"Log in").click().run()
        find(self.app.text_input,"Username").input("owner")
        find(self.app.text_input,"Password").input(self.password)
        find(self.app.button,"Sign in").click().run()
        self.assertFalse(self.app.exception)
        self.assertTrue(any("Owner secret market" in str(x.options) for x in self.app.selectbox))
        token=self.app.session_state["login_session"]
        find(self.app.button,"Log out").click().run()
        self.assertFalse(self.app.exception)
        self.assertIsNone(self.store.session_user(token))
        self.assertFalse(any("Owner secret market" in str(x.options) for x in self.app.selectbox))

    def test_two_guest_sessions_have_separate_workspaces(self):
        self.app.run()
        other=AppTest.from_file(str(APP),default_timeout=30).run()
        self.assertFalse(other.exception)
        self.assertNotEqual(self.app.session_state["guest_workspace"],other.session_state["guest_workspace"])

    def test_public_owner_setup_is_not_available(self):
        self.app.run()
        find(self.app.button,"Log in").click().run()
        self.assertFalse(any(x.label=="Create owner account" for x in self.app.button))
        self.assertFalse(self.store.has_users())
        self.assertTrue(any("not configured" in x.value for x in self.app.info))

    def test_guest_can_generate_image_and_brief_without_login(self):
        from PIL import Image
        image=self.root/"mock.png"
        Image.new("RGB",(768,768),"white").save(image)
        with patch("gradio_client.Client") as client:
            client.return_value.submit.return_value.result.return_value=(str(image),42)
            self.app.run()
            find(self.app.text_area,"Description to generate").input("A shop beside a road").run()
            find(self.app.button,"Get Brief").click().run()
            self.assertFalse(self.app.exception)
            client.assert_not_called()
            find(self.app.button,"Get Image").click().run()
            self.assertFalse(self.app.exception)
            self.assertTrue(client.return_value.submit.called)
            guest_env=storage_functions(self.root/"guest_sessions"/self.app.session_state["guest_workspace"])
            case=guest_env["list_cases"]()[0]["id"]
            self.assertEqual(len(guest_env["list_revisions"](case)),1)
            self.assertIsNotNone(guest_env["latest_brief"](case,"Scene illustration"))

    def test_optional_reference_panel_does_not_block_text_workflow(self):
        self.app.run()
        find(self.app.radio,"Output mode").set_value("Face sketch").run()
        self.assertFalse(self.app.exception)
        self.assertEqual(len(self.app.get("file_uploader")),6)
        self.assertFalse(find(self.app.button,"Get Image").disabled)
        self.assertTrue(any("Get Image uses text only" in x.value for x in self.app.info))
        find(self.app.text_area,"Description to generate").input("A fictional adult with curly hair.").run()
        find(self.app.button,"Get Brief").click().run()
        self.assertFalse(self.app.exception)
        find(self.app.button,"Clear all references").click().run()
        self.assertFalse(self.app.exception)

    def test_paid_reference_route_is_separate_and_requires_authorization(self):
        from io import BytesIO
        from PIL import Image
        image=BytesIO();Image.new("RGB",(64,64),"white").save(image,format="PNG")
        refs=[{"feature":"Eyes","instruction":"Narrower eyes, shape only","image_bytes":image.getvalue(),"crop_percent":{"horizontal":[0,100],"vertical":[0,100]}}]
        with patch("reference_board.render_reference_board",return_value=refs), patch("reference_backend.run_reference",return_value=(image.getvalue(),42,"reference-1")) as paid, patch("gradio_client.Client") as free:
            self.app.run()
            find(self.app.radio,"Output mode").set_value("Face sketch").run()
            self.assertTrue(find(self.app.button,"Generate with references · paid").disabled)
            find(self.app.text_input,"fal key for references (private)").input("test-key").run()
            self.assertTrue(find(self.app.button,"Generate with references · paid").disabled)
            find(self.app.checkbox,"I authorize paid reference requests and sending these crops and instructions to fal").check().run()
            find(self.app.text_area,"Description to generate").input("A fictional adult with curly hair.").run()
            find(self.app.button,"Generate with references · paid").click().run()
            self.assertFalse(self.app.exception)
            self.assertEqual(paid.call_count,1)
            free.assert_not_called()
            env=storage_functions(self.root/"guest_sessions"/self.app.session_state["guest_workspace"])
            case=env["list_cases"]()[0]["id"]
            revision=env["list_revisions"](case)[0]
            self.assertIn("Narrower eyes",revision["prompt"])
            self.assertEqual(env["get_revision"](case,revision["id"])["provider"],"fal reference · FLUX Kontext multi")
