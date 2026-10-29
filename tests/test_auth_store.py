
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from auth_store import AuthStore,AuthError,SESSION_SECONDS

class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1],ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.store=AuthStore(self.temp.name)
        self.password="a-long-owner-passphrase-2026"
        self.owner=self.store.create_user("owner","Owner",self.password,first=True)

    def test_passwords_and_session_tokens_are_not_stored_in_plaintext(self):
        token=self.store.login("OWNER",self.password)
        raw=self.store.path.read_bytes()
        self.assertNotIn(self.password.encode(),raw)
        self.assertNotIn(token.encode(),raw)
        self.assertEqual(self.store.session_user(token)["id"],self.owner)
        self.assertIsNone(self.store.session_user("tampered-token"))

    def test_legacy_owner_and_new_user_have_different_workspaces(self):
        token=self.store.login("owner",self.password)
        self.store.create_user("member","Member",self.password,admin_token=token)
        member_token=self.store.login("member",self.password)
        self.assertEqual(self.store.workspace_for(self.store.session_user(token)),Path(self.temp.name).resolve())
        member_path=self.store.workspace_for(self.store.session_user(member_token))
        self.assertNotEqual(member_path,Path(self.temp.name).resolve())
        self.assertTrue(str(member_path).startswith(str(Path(self.temp.name).resolve())))
        with sqlite3.connect(self.store.path) as db:
            rows=db.execute("SELECT salt,password_hash FROM users").fetchall()
        self.assertNotEqual(rows[0],rows[1])

    def test_wrong_login_is_generic_and_rate_limited(self):
        for attempt in range(5):
            with self.assertRaisesRegex(AuthError,"Username or password is incorrect"):
                self.store.login("owner","wrong",current_time=1000)
        with self.assertRaisesRegex(AuthError,"Too many attempts"):
            self.store.login("owner",self.password,current_time=1001)
        token=self.store.login("owner",self.password,current_time=1301)
        self.assertIsNotNone(self.store.session_user(token,current_time=1302))

    def test_logout_and_expiry_revoke_access(self):
        token=self.store.login("owner",self.password,current_time=1000)
        self.assertIsNotNone(self.store.session_user(token,current_time=1001))
        self.assertIsNone(self.store.session_user(token,current_time=1000+SESSION_SECONDS))
        token=self.store.login("owner",self.password)
        self.store.logout(token)
        self.assertIsNone(self.store.session_user(token))

    def test_change_password_revokes_all_existing_sessions(self):
        first=self.store.login("owner",self.password)
        second=self.store.login("owner",self.password)
        new="a-new-owner-passphrase-2026"
        self.store.change_password(first,self.password,new)
        self.assertIsNone(self.store.session_user(first))
        self.assertIsNone(self.store.session_user(second))
        with self.assertRaises(AuthError):
            self.store.login("owner",self.password)
        self.assertTrue(self.store.session_user(self.store.login("owner",new)))

    def test_only_admin_can_create_additional_accounts(self):
        owner_token=self.store.login("owner",self.password)
        self.store.create_user("member","Member",self.password,admin_token=owner_token)
        member_token=self.store.login("member",self.password)
        with self.assertRaises(AuthError):
            self.store.create_user("third","Third",self.password,admin_token=member_token)
        with self.assertRaises(AuthError):
            self.store.create_user("owner2","Owner2",self.password,first=True)

    def test_invalid_or_weak_credentials_are_rejected(self):
        token=self.store.login("owner",self.password)
        with self.assertRaises(AuthError):
            self.store.create_user("bad name","Bad","short",admin_token=token)
        with self.assertRaises(AuthError):
            self.store.create_user("member","Member","short",admin_token=token)

    def test_local_recovery_revokes_sessions(self):
        token=self.store.login("owner",self.password)
        new="a-recovered-passphrase-2026"
        self.store.reset_password_locally("owner",new)
        self.assertIsNone(self.store.session_user(token))
        self.assertIsNotNone(self.store.session_user(self.store.login("owner",new)))

if __name__=="__main__":
    unittest.main()
