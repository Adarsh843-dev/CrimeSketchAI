"""Local prototype authentication. Credentials and sessions stay outside case databases."""
from contextlib import closing
from hashlib import pbkdf2_hmac, sha256
import hmac
from pathlib import Path
import re
import secrets
import sqlite3
import time
import uuid

ITERATIONS=600_000
SESSION_SECONDS=8*60*60

class AuthError(ValueError):
    pass

class AuthStore:
    def __init__(self, root):
        self.root=Path(root).resolve()
        self.root.mkdir(parents=True,exist_ok=True)
        self.path=self.root/"accounts.sqlite3"
        with closing(self.connect()) as db,db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS users(
                id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, display_name TEXT NOT NULL,
                salt BLOB NOT NULL, password_hash BLOB NOT NULL, iterations INTEGER NOT NULL,
                role TEXT NOT NULL, legacy_owner INTEGER NOT NULL, created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions(
                token_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),expires REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS login_attempts(
                subject TEXT PRIMARY KEY,failures INTEGER NOT NULL,blocked_until REAL NOT NULL
            );
            """)

    def connect(self):
        db=sqlite3.connect(self.path,timeout=20)
        db.row_factory=sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def has_users(self):
        with closing(self.connect()) as db:
            return db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None

    @staticmethod
    def normalize_username(username):
        username=username.strip().lower()
        if not re.fullmatch(r"[a-z0-9_.-]{3,40}",username):
            raise AuthError("Use 3–40 letters, numbers, dots, underscores or hyphens for the username.")
        return username

    @staticmethod
    def validate_password(password):
        if not 15<=len(password)<=256:
            raise AuthError("Use a password or passphrase between 15 and 256 characters.")

    @staticmethod
    def public_user(row):
        return {key:row[key] for key in ("id","username","display_name","role","legacy_owner")}

    def create_user(self,username,display_name,password,*,first=False,admin_token=None):
        username=self.normalize_username(username)
        self.validate_password(password)
        display_name=display_name.strip()
        if not 1<=len(display_name)<=80:
            raise AuthError("Enter a display name between 1 and 80 characters.")
        if not first:
            admin=self.session_user(admin_token)
            if not admin or admin["role"]!="admin":
                raise AuthError("Only a signed-in administrator can create additional accounts.")
        salt=secrets.token_bytes(32)
        digest=pbkdf2_hmac("sha256",password.encode(),salt,ITERATIONS)
        user_id=uuid.uuid4().hex
        with closing(self.connect()) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                if first and db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                    raise AuthError("First-run setup is already complete. Sign in instead.")
                db.execute("INSERT INTO users VALUES (?,?,?,?,?,?,?,?,?)",
                           (user_id,username,display_name,salt,digest,ITERATIONS,
                            "admin" if first else "user",int(first),time.time()))
                db.commit()
            except sqlite3.IntegrityError:
                db.rollback()
                raise AuthError("That username is already in use.") from None
            except Exception:
                db.rollback()
                raise
        return user_id

    def login(self,username,password,*,current_time=None):
        current_time=time.time() if current_time is None else float(current_time)
        normalized=username.strip().lower()
        subject=sha256(normalized.encode()).hexdigest()
        with closing(self.connect()) as db,db:
            attempt=db.execute("SELECT * FROM login_attempts WHERE subject=?",(subject,)).fetchone()
            if attempt and attempt["blocked_until"]>current_time:
                seconds=max(1,int(attempt["blocked_until"]-current_time))
                raise AuthError("Too many attempts. Try again in "+str(seconds)+" seconds.")
            user=db.execute("SELECT * FROM users WHERE username=?",(normalized,)).fetchone()
            salt=user["salt"] if user else b"unknown-account-dummy-salt-32bytes!"
            iterations=user["iterations"] if user else ITERATIONS
            digest=pbkdf2_hmac("sha256",password.encode(),salt,iterations)
            valid=bool(user) and hmac.compare_digest(digest,user["password_hash"])
            if not valid:
                failures=(attempt["failures"] if attempt and attempt["blocked_until"]==0 else 0)+1
                blocked_until=current_time+300 if failures>=5 else 0
                db.execute("INSERT INTO login_attempts VALUES (?,?,?) ON CONFLICT(subject) DO UPDATE SET "
                           "failures=excluded.failures,blocked_until=excluded.blocked_until",
                           (subject,failures,blocked_until))
            else:
                db.execute("DELETE FROM login_attempts WHERE subject=?",(subject,))
                db.execute("DELETE FROM sessions WHERE expires<=?",(current_time,))
                token=secrets.token_urlsafe(32)
                db.execute("INSERT INTO sessions VALUES (?,?,?)",
                           (sha256(token.encode()).hexdigest(),user["id"],current_time+SESSION_SECONDS))
        if not valid:
            raise AuthError("Username or password is incorrect.")
        return token

    def session_user(self,token,*,current_time=None):
        if not token or not isinstance(token,str):
            return None
        current_time=time.time() if current_time is None else float(current_time)
        with closing(self.connect()) as db:
            row=db.execute("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
                           "WHERE s.token_hash=? AND s.expires>?",
                           (sha256(token.encode()).hexdigest(),current_time)).fetchone()
        return self.public_user(row) if row else None

    def logout(self,token):
        if token:
            with closing(self.connect()) as db,db:
                db.execute("DELETE FROM sessions WHERE token_hash=?",(sha256(token.encode()).hexdigest(),))

    def workspace_for(self,user):
        # Never accept a path or user ID from a browser widget.
        with closing(self.connect()) as db:
            row=db.execute("SELECT * FROM users WHERE id=?",(user["id"],)).fetchone()
        if not row:
            raise AuthError("Account not found.")
        return self.root if row["legacy_owner"] else self.root/"users"/row["id"]

    def change_password(self,token,current_password,new_password):
        user=self.session_user(token)
        if not user:
            raise AuthError("Sign in again.")
        self.validate_password(new_password)
        with closing(self.connect()) as db,db:
            row=db.execute("SELECT * FROM users WHERE id=?",(user["id"],)).fetchone()
            digest=pbkdf2_hmac("sha256",current_password.encode(),row["salt"],row["iterations"])
            if not hmac.compare_digest(digest,row["password_hash"]):
                raise AuthError("Current password is incorrect.")
            salt=secrets.token_bytes(32)
            new_hash=pbkdf2_hmac("sha256",new_password.encode(),salt,ITERATIONS)
            db.execute("UPDATE users SET salt=?,password_hash=?,iterations=? WHERE id=?",
                       (salt,new_hash,ITERATIONS,user["id"]))
            db.execute("DELETE FROM sessions WHERE user_id=?",(user["id"],))

    def reset_password_locally(self,username,new_password):
        # CLI only. Possession of the local database/filesystem is the recovery authority.
        self.validate_password(new_password)
        with closing(self.connect()) as db,db:
            row=db.execute("SELECT id FROM users WHERE username=?",(username.strip().lower(),)).fetchone()
            if not row:
                raise AuthError("Account not found.")
            salt=secrets.token_bytes(32)
            digest=pbkdf2_hmac("sha256",new_password.encode(),salt,ITERATIONS)
            db.execute("UPDATE users SET salt=?,password_hash=?,iterations=? WHERE id=?",
                       (salt,digest,ITERATIONS,row["id"]))
            db.execute("DELETE FROM sessions WHERE user_id=?",(row["id"],))

    def list_users(self,admin_token):
        admin=self.session_user(admin_token)
        if not admin or admin["role"]!="admin":
            raise AuthError("Administrator access required.")
        with closing(self.connect()) as db:
            return [self.public_user(row) for row in db.execute("SELECT * FROM users ORDER BY created")]
