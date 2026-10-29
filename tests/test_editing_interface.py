from io import BytesIO
import unittest
from unittest.mock import patch
from PIL import Image
import test_interface
from test_interface import find
from test_workspace import storage_functions
from image_editing import label_picture

class EditingInterfaceTests(unittest.TestCase):
    setUp=test_interface.InterfaceTests.setUp
    create=test_interface.InterfaceTests.create

    def generate_source(self):
        self.create("Source case")
        find(self.app.text_area,"Description to generate").input("A fictional adult with broad eyebrows.").run()
        find(self.app.button,"Get Image").click().run()
        self.assertFalse(self.app.exception)
        self.env=storage_functions(self.directory)
        self.case=self.env["list_cases"]()[0]["id"]
        self.source=self.env["list_revisions"](self.case)[0]

    def test_rename_is_visible_and_survives_active_search(self):
        self.generate_source()
        find(self.app.text_input,"Search cases").input("Source").run()
        find(self.app.button,"Rename case").click().run()
        find(self.app.text_input,"New case title").input("Updated case name")
        find(self.app.button,"Save new case name").click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.env["list_cases"]()[0]["name"],"Updated case name")
        self.assertEqual(len(self.env["list_revisions"](self.case)),1)
        self.assertEqual(find(self.app.text_input,"Search cases").value,"")
        self.assertTrue(any("Updated case name" in x.value for x in self.app.header))

    def test_local_edit_creates_child_without_any_ai_request(self):
        self.generate_source()
        requests=self.client.return_value.submit.call_count
        with patch("image_editing.run_fal") as paid:
            find(self.app.slider,"Contrast").set_value(1.2).run()
            find(self.app.button,"Save adjusted version").click().run()
            self.assertFalse(self.app.exception)
            paid.assert_not_called()
        rows=self.env["list_revisions"](self.case)
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]["parent_id"],self.source["id"])
        self.assertEqual(self.client.return_value.submit.call_count,requests)
        self.assertEqual(self.env["get_revision"](self.case,rows[0]["id"])["provider"],"Local image adjustments")

    def test_ai_edit_uses_generated_source_and_keeps_parent(self):
        self.generate_source()
        source=self.env["get_revision"](self.case,self.source["id"])
        self.assertTrue(find(self.app.button,"Apply AI edit · paid").disabled)
        buffer=BytesIO();Image.new("RGB",(768,768),"black").save(buffer,format="PNG")
        with patch("image_editing.run_fal",return_value=(buffer.getvalue(),42,"edit-1")) as paid:
            find(self.app.text_area,"What should change in this image?").input("Make the eyebrows thinner.").run()
            find(self.app.text_input,"fal key for image edits (private)").input("mock-private-key").run()
            self.assertTrue(find(self.app.button,"Apply AI edit · paid").disabled)
            find(self.app.checkbox,"I authorize a paid image edit and sending this generated image to fal").check().run()
            find(self.app.button,"Apply AI edit · paid").click().run()
            self.assertFalse(self.app.exception)
            self.assertEqual(paid.call_count,1)
            self.assertEqual(paid.call_args.kwargs["context"]["parent_id"],source["id"])
            self.assertIn("image_url",paid.call_args.kwargs["payload"])
        rows=self.env["list_revisions"](self.case)
        self.assertEqual(rows[0]["parent_id"],source["id"])
        self.assertEqual(self.env["get_revision"](self.case,source["id"])["image"],source["image"])
        self.assertEqual(self.env["get_revision"](self.case,rows[0]["id"])["provider"],"fal image edit · FLUX Kontext")

    def test_selecting_an_older_version_edits_that_exact_parent(self):
        self.generate_source()
        find(self.app.slider,"Contrast").set_value(1.2).run()
        find(self.app.button,"Save adjusted version").click().run()
        child=self.env["list_revisions"](self.case)[0]
        self.assertTrue(any(x.label=="View previous version" for x in self.app.button))
        find(self.app.button,"View previous version").click().run()
        self.assertEqual(find(self.app.selectbox,"Image to view or edit").value,self.source["id"])
        find(self.app.slider,"Brightness").set_value(1.1).run()
        find(self.app.button,"Save adjusted version").click().run()
        self.assertFalse(self.app.exception)
        rows=self.env["list_revisions"](self.case)
        self.assertEqual(len(rows),3)
        self.assertEqual(rows[0]["parent_id"],self.source["id"])

    def test_empty_name_is_rejected_and_cancel_is_safe(self):
        self.generate_source()
        find(self.app.button,"Rename case").click().run()
        find(self.app.text_input,"New case title").input("   ")
        find(self.app.button,"Save new case name").click().run()
        self.assertFalse(self.app.exception)
        self.assertTrue(any("case name" in x.value.lower() for x in self.app.warning))
        self.assertEqual(self.env["list_cases"]()[0]["name"],"Source case")
        find(self.app.button,"Cancel rename").click().run()
        self.assertFalse(any(x.label=="New case title" for x in self.app.text_input))
