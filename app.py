from io import BytesIO
from pathlib import Path
from contextlib import closing
from datetime import datetime, timezone, timedelta
import json
import math
import sqlite3
import uuid
import zipfile
import html
import base64
import secrets
import difflib
import os
import re
from concurrent.futures import TimeoutError as JobTimeout
from huggingface_hub import get_token
import streamlit as st
from gradio_client import Client
from fal_backend import run_fal, list_pending, ProviderError, PendingJob
from auth_store import AuthStore, AuthError
from reference_board import render_reference_board
from reference_backend import run_reference, prepare_references, REFERENCE_PROVIDER, REFERENCE_MODEL
from PIL import Image, ImageDraw, ImageOps

st.set_page_config(page_title="CrimeSketchAI", page_icon="🔎", layout="wide")
# Hosting secrets never belong in GitHub. Root keys are also available as env vars.
try:
    hosting_secrets=dict(st.secrets)
except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
    hosting_secrets={}
for secret_key in ("HF_TOKEN","FAL_KEY"):
    if hosting_secrets.get(secret_key):
        os.environ[secret_key]=str(hosting_secrets[secret_key])
NOTICE = "AI-GENERATED ILLUSTRATION - NOT VERIFIED EVIDENCE"
EXAMPLES = {
    "Scene illustration": "A narrow Indian street in daylight. A grocery shop on the left, a motorcycle parked in front of it, and two-storey buildings on the right. No people.",
    "Face sketch": "A fictional adult with an oval face, short curly hair, thick eyebrows, a broad nose and a moustache. No beard or glasses.",
}

def build_prompt(description, mode):
    if mode == "Scene illustration":
        style = "Black-and-white pencil scene illustration, wide eye-level view. Follow the described objects and positions. "
    else:
        style = "Black-and-white pencil head-and-shoulders portrait of a fictional person, front view, plain background. Follow the described facial features. "
    return style + "Description: " + description + ". Clear outlines, no added labels."

def generate_image(prompt, seed):
    client = Client("black-forest-labs/FLUX.1-schnell", token=get_token(), verbose=False)
    job = client.submit(
        prompt=prompt, seed=seed, randomize_seed=False,
        width=768, height=768, num_inference_steps=4, api_name="/infer",
    )
    try:
        image, actual_seed = job.result(timeout=240)
    except JobTimeout:
        try:
            job.cancel()
        except Exception:
            pass
        raise TimeoutError("Generation timed out after four minutes.") from None
    source = image.get("path") if isinstance(image, dict) else image
    if not source or not Path(source).is_file():
        raise RuntimeError("No downloaded image returned.")
    with Image.open(source) as original:
        picture = ImageOps.grayscale(original).convert("RGB")
    labelled = Image.new("RGB", (picture.width, picture.height + 48), "white")
    labelled.paste(picture, (0, 0))
    ImageDraw.Draw(labelled).text((12, picture.height + 17), NOTICE, fill="black")
    buffer = BytesIO()
    labelled.save(buffer, format="PNG")
    return buffer.getvalue(), int(actual_seed)


APP_DIR = Path(__file__).resolve().parent
# A nested installation reuses the previous workspace without moving or deleting it.
existing = APP_DIR.parent / "crimesketch_data"
default_data = existing if (existing / "workspace.sqlite3").is_file() else APP_DIR / "crimesketch_data"
DATA_DIR = Path(os.environ.get("CRIMESKETCH_DATA_DIR", str(default_data))).resolve()
DB_PATH = DATA_DIR / "workspace.sqlite3"

def connect_db():
    connection = sqlite3.connect(DB_PATH, timeout=20)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection

def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if DB_PATH.is_file():
        with closing(connect_db()) as current:
            has_preferences = current.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='preferred_revisions'").fetchone()
            has_drafts = current.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='drafts'").fetchone()
            has_ui = current.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ui_preferences'").fetchone()
            has_limits = current.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='provider_limits'").fetchone()
            has_memory = current.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workspace_preferences'").fetchone()
            has_sources = current.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='generation_sources'").fetchone()
            has_accounts = current.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_records'").fetchone()
            if not has_preferences or not has_drafts or not has_accounts or not has_sources or not has_memory or not has_limits or not has_ui:
                backup_path = DATA_DIR / ("pre_upgrade_" + uuid.uuid4().hex[:8] + ".sqlite3")
                with closing(sqlite3.connect(backup_path)) as backup:
                    current.backup(backup)
    with closing(connect_db()) as db, db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS cases (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, created TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS archived_cases (
                case_id TEXT PRIMARY KEY REFERENCES cases(id)
            );
            CREATE TABLE IF NOT EXISTS ui_preferences (
                id INTEGER PRIMARY KEY CHECK(id=1), theme TEXT NOT NULL CHECK(theme IN ('light','dark'))
            );
            CREATE TABLE IF NOT EXISTS provider_limits (
                provider TEXT PRIMARY KEY, state TEXT NOT NULL,
                observed REAL NOT NULL, retry_at REAL,
                requested_seconds REAL, remaining_seconds REAL
            );
            CREATE TABLE IF NOT EXISTS workspace_preferences (
                id INTEGER PRIMARY KEY CHECK(id=1), settings TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS feedback_outcomes (
                case_id TEXT NOT NULL REFERENCES cases(id),
                feedback_id TEXT NOT NULL REFERENCES refinement_feedback(id),
                child_id TEXT NOT NULL REFERENCES revisions(id),
                rating TEXT NOT NULL, allow_cross_case INTEGER NOT NULL,
                created TEXT NOT NULL, PRIMARY KEY(feedback_id,child_id)
            );
            CREATE TABLE IF NOT EXISTS revision_memory (
                revision_id TEXT PRIMARY KEY REFERENCES revisions(id),
                snapshot TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS generation_sources (
                revision_id TEXT PRIMARY KEY REFERENCES revisions(id),
                provider TEXT NOT NULL, request_id TEXT
            );
            CREATE TABLE IF NOT EXISTS account_records (
                id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
                mode TEXT NOT NULL, text TEXT NOT NULL, created TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS detail_provenance (
                id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
                account_id TEXT NOT NULL REFERENCES account_records(id),
                detail TEXT NOT NULL, basis TEXT NOT NULL, created TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS refinement_feedback (
                id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
                revision_id TEXT NOT NULL REFERENCES revisions(id),
                account_id TEXT REFERENCES account_records(id),
                issue TEXT NOT NULL, observed TEXT NOT NULL, expected TEXT NOT NULL,
                basis TEXT NOT NULL, candidate TEXT NOT NULL, created TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS written_briefs (
                id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
                mode TEXT NOT NULL, description TEXT NOT NULL,
                content TEXT NOT NULL, created TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS preferred_revisions (
                case_id TEXT NOT NULL REFERENCES cases(id), mode TEXT NOT NULL,
                revision_id TEXT NOT NULL REFERENCES revisions(id),
                PRIMARY KEY(case_id, mode)
            );
            CREATE TABLE IF NOT EXISTS drafts (
                case_id TEXT NOT NULL REFERENCES cases(id), mode TEXT NOT NULL,
                description TEXT NOT NULL, fields TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY(case_id, mode)
            );
            CREATE TABLE IF NOT EXISTS review_status (
                revision_id TEXT PRIMARY KEY REFERENCES revisions(id),
                status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS revisions (
                id TEXT PRIMARY KEY,
                case_id TEXT NOT NULL REFERENCES cases(id),
                parent_id TEXT REFERENCES revisions(id),
                mode TEXT NOT NULL, description TEXT NOT NULL,
                prompt TEXT NOT NULL, seed INTEGER NOT NULL,
                image BLOB NOT NULL, created TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT ''
            );
        ''')

def case_is_archived(case_id):
    with closing(connect_db()) as db:
        return db.execute("SELECT 1 FROM archived_cases WHERE case_id=?", (case_id,)).fetchone() is not None

def set_case_archived(case_id, archived):
    with closing(connect_db()) as db, db:
        if archived:
            db.execute("INSERT OR IGNORE INTO archived_cases(case_id) VALUES (?)", (case_id,))
        else:
            db.execute("DELETE FROM archived_cases WHERE case_id=?", (case_id,))

def rename_case(case_id, name):
    name = name.strip()
    if not name or len(name) > 100:
        raise ValueError("Use a case name between 1 and 100 characters.")
    with closing(connect_db()) as db, db:
        db.execute("UPDATE cases SET name=? WHERE id=?", (name, case_id))

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def create_case(name):
    name = name.strip()
    if not name or len(name) > 100:
        raise ValueError("Use a case name between 1 and 100 characters.")
    case_id = uuid.uuid4().hex
    with closing(connect_db()) as db, db:
        db.execute("INSERT INTO cases VALUES (?, ?, ?)", (case_id, name, now()))
    return case_id

def list_cases():
    with closing(connect_db()) as db:
        return [dict(row) for row in db.execute("SELECT * FROM cases ORDER BY created DESC, rowid DESC")]

def list_revisions(case_id):
    with closing(connect_db()) as db:
        return [dict(row) for row in db.execute(
            "SELECT id, case_id, parent_id, mode, description, prompt, seed, created, notes "
            "FROM revisions WHERE case_id=? ORDER BY rowid DESC", (case_id,)
        )]

def get_revision(case_id, revision_id):
    with closing(connect_db()) as db:
        row = db.execute("SELECT r.*, COALESCE(g.provider,'Hugging Face demo') AS provider,g.request_id FROM revisions r LEFT JOIN generation_sources g ON g.revision_id=r.id WHERE r.case_id=? AND r.id=?", (case_id, revision_id)).fetchone()
    return dict(row) if row else None

def save_revision(case_id, mode, description, prompt, seed, image, parent_id=None,
                  provider="Hugging Face demo", request_id=None, memory_snapshot=None):
    revision_id = uuid.uuid4().hex
    with closing(connect_db()) as db, db:
        if parent_id:
            parent = db.execute("SELECT case_id FROM revisions WHERE id=?", (parent_id,)).fetchone()
            if not parent or parent["case_id"] != case_id:
                raise ValueError("The source revision does not belong to this case.")
        db.execute(
            "INSERT INTO revisions (id,case_id,parent_id,mode,description,prompt,seed,image,created) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (revision_id,case_id,parent_id,mode,description,prompt,int(seed),image,now()),
        )
        db.execute("INSERT INTO generation_sources VALUES (?,?,?)", (revision_id,provider,request_id))
        if memory_snapshot is not None:
            db.execute("INSERT INTO revision_memory VALUES (?,?)", (revision_id,json.dumps(memory_snapshot)))
    return revision_id

def save_notes(case_id, revision_id, notes):
    with closing(connect_db()) as db, db:
        db.execute("UPDATE revisions SET notes=? WHERE case_id=? AND id=?", (notes,case_id,revision_id))

def export_case(case_id, case_name):
    buffer = BytesIO()
    revisions = list_revisions(case_id)
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        manifest = {"case": case_name, "exported_utc": now(), "disclosure": NOTICE,
                    "model": "black-forest-labs/FLUX.1-schnell",
                    "width": 768, "height": 768, "steps": 4, "revisions": [],
                    "account_records": list_accounts(case_id),
                    "detail_provenance": list_provenance(case_id),
                    "refinement_feedback": refinement_records(case_id),
                    "written_briefs": brief_records(case_id),
                    "feedback_outcomes": list_outcomes(case_id)}
        for revision in reversed(revisions):
            item = get_revision(case_id, revision["id"])
            item["memory_influence"] = revision_memory_snapshot(item["id"])
            item["review_status"] = get_review_status(item["id"])
            item["preferred"] = get_preferred(case_id, item["mode"]) == item["id"]
            filename = "images/" + item["id"] + ".png"
            archive.writestr(filename, item.pop("image"))
            item["image_file"] = filename
            manifest["revisions"].append(item)
        archive.writestr("case.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.writestr("case_report.html", export_report(case_id, case_name))
        archive.writestr("README.txt",
            "CrimeSketchAI case export. All images are AI-generated illustrations. "
            "They do not verify identity or events. case.json contains descriptions, "
            "prompts, seeds, review notes and source revision links. Timestamps use UTC.")
    return buffer.getvalue()


def export_report(case_id, case_name):
    escape = lambda value: html.escape(str(value), quote=True)
    sections = []
    for number, revision in enumerate(reversed(list_revisions(case_id)), 1):
        item = get_revision(case_id, revision["id"])
        encoded = base64.b64encode(item["image"]).decode("ascii")
        notes = item["notes"].strip() or "No review notes recorded."
        status = get_review_status(item["id"])
        parent = item["parent_id"] or "Independent version"
        memory = revision_memory_snapshot(item["id"])
        memory_text = ", ".join(memory.get("applied_presentation", [])) or "None recorded"
        preference = "Preferred illustration for this mode" if get_preferred(case_id, item["mode"]) == item["id"] else "Not marked preferred"
        sections.append(
            '<section><h2>Version ' + str(number) + ' / ' + escape(item["mode"]) + '</h2>'
            '<p class="meta">Created ' + escape(item["created"]) + ' (UTC)<br>'
            'Seed: ' + escape(item["seed"]) + ' / Revision: ' + escape(item["id"]) + '<br>'
            'Source revision: ' + escape(parent) + '<br>' + escape(preference) + '</p>'
            '<img alt="AI-generated illustration" src="data:image/png;base64,' + encoded + '">'
            '<p>Presentation memory applied: ' + escape(memory_text) + '</p>'
            '<h3>Source description</h3><p class="preserve">' + escape(item["description"]) + '</p>'
            '<h3>Review notes</h3><p>Review status: ' + escape(status) + '</p><p class="preserve">' + escape(notes) + '</p>'
            '<h3>Generation prompt</h3><p class="preserve">'
            + escape(item["prompt"]) + '</p></section>'
        )
    provenance_sections = ["<h2>Recorded accounts and detail provenance</h2>"]
    for record in list_accounts(case_id):
        provenance_sections.append("<h3>" + escape(record["mode"]) + " / " + escape(record["created"]) + "</h3><p class='preserve'>" + escape(record["text"]) + "</p>")
        for detail in list_provenance(case_id, record["id"]):
            provenance_sections.append("<p><strong>" + escape(detail["basis"]) + ":</strong> " + escape(detail["detail"]) + "</p>")
    provenance_sections.append("<h2>User-reported image feedback</h2>")
    for feedback in refinement_records(case_id):
        provenance_sections.append("<p><strong>" + escape(feedback["issue"]) + "</strong> / Image " + escape(feedback["revision_id"][:8]) + "<br>Observed: " + escape(feedback["observed"]) + "<br>Requested: " + escape(feedback["expected"]) + "<br>Basis: " + escape(feedback["basis"]) + "</p>")
    document = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>CrimeSketchAI case report</title>'
        '<style>body{font:16px/1.6 system-ui,sans-serif;color:#152d33;max-width:920px;'
        'margin:40px auto;padding:0 24px}h1{font-size:36px}h2{color:#007f7a}'
        '.notice{border-left:4px solid #007f7a;padding:12px 18px;background:#edf7f4}'
        '.meta{color:#536970;font-size:14px;overflow-wrap:anywhere}'
        'section{border-top:1px solid #ccd7d6;padding-top:24px;margin-top:36px}'
        'img{display:block;max-width:100%;max-height:650px;margin:20px auto}'
        '.preserve{white-space:pre-wrap;overflow-wrap:anywhere}'
        '@media print{body{margin:0;max-width:none;font-size:11pt}'
        'section{break-before:page}img{max-height:145mm}details{display:block}'
        'h2,h3{break-after:avoid}.print-help{display:none}}</style></head><body>'
        '<h1>CrimeSketchAI</h1><h2>' + escape(case_name) + '</h2>'
        '<p class="meta">Report exported ' + escape(now()) + ' (UTC)</p>'
        '<div class="notice"><strong>' + escape(NOTICE) + '</strong><br>'
        'These images may invent or omit details. They do not verify identity or events. '
        'Review notes record user observations and are not independent verification.</div>'
        '<p>Model: FLUX.1-schnell. Hosted generation at 768 × 768, four inference steps.</p>'
        '<p class="print-help">To save a PDF, use your browser’s Print command and select Save as PDF. '
        'The report includes the exact generation prompts.</p>'
        + ''.join(provenance_sections) + ''.join(sections) + '</body></html>'
    )
    return document.encode("utf-8")



def save_draft(case_id, mode, description, fields=None):
    with closing(connect_db()) as db, db:
        db.execute("INSERT INTO drafts(case_id,mode,description,fields) VALUES (?,?,?,?) "
                    "ON CONFLICT(case_id,mode) DO UPDATE SET description=excluded.description,fields=excluded.fields "
                   "WHERE drafts.description != excluded.description OR drafts.fields != excluded.fields",
                   (case_id, mode, description, json.dumps(fields or {}, ensure_ascii=False)))

def get_draft(case_id, mode):
    with closing(connect_db()) as db:
        row = db.execute("SELECT description,fields FROM drafts WHERE case_id=? AND mode=?", (case_id,mode)).fetchone()
    return {"description": row["description"], "fields": json.loads(row["fields"])} if row else {"description": "", "fields": {}}

def get_review_status(revision_id):
    with closing(connect_db()) as db:
        row = db.execute("SELECT status FROM review_status WHERE revision_id=?", (revision_id,)).fetchone()
    return row["status"] if row else "Not reviewed"

def set_review_status(revision_id, status):
    if status not in ("Not reviewed", "Needs revision", "Reviewed"):
        raise ValueError("Invalid review status")
    with closing(connect_db()) as db, db:
        db.execute("INSERT INTO review_status VALUES (?,?) ON CONFLICT(revision_id) DO UPDATE SET status=excluded.status",
                   (revision_id,status))

def invalidate_exports(case_id):
    st.session_state.pop("export_" + case_id, None)
    st.session_state.pop("report_" + case_id, None)

def database_backup():
    # SQLite's backup API produces a consistent snapshot, including committed images.
    import tempfile
    with tempfile.TemporaryDirectory(dir=DATA_DIR) as directory:
        target = Path(directory) / "workspace.sqlite3"
        with closing(connect_db()) as origin, closing(sqlite3.connect(target)) as destination:
            origin.backup(destination)
        return target.read_bytes()



def get_preferred(case_id, mode):
    with closing(connect_db()) as db:
        row = db.execute("SELECT revision_id FROM preferred_revisions WHERE case_id=? AND mode=?",
                         (case_id, mode)).fetchone()
    return row["revision_id"] if row else None

def set_preferred(case_id, mode, revision_id):
    with closing(connect_db()) as db, db:
        if revision_id is None:
            db.execute("DELETE FROM preferred_revisions WHERE case_id=? AND mode=?", (case_id, mode))
            return
        row = db.execute("SELECT mode FROM revisions WHERE case_id=? AND id=?", (case_id, revision_id)).fetchone()
        if not row or row["mode"] != mode:
            raise ValueError("Choose a saved illustration from this case and mode.")
        db.execute("INSERT INTO preferred_revisions VALUES (?,?,?) "
                   "ON CONFLICT(case_id,mode) DO UPDATE SET revision_id=excluded.revision_id",
                   (case_id,mode,revision_id))

def persist_review(case_id, revision_id, notes, status):
    if status not in ("Not reviewed", "Needs revision", "Reviewed"):
        raise ValueError("Invalid review status")
    with closing(connect_db()) as db, db:
        found = db.execute("SELECT 1 FROM revisions WHERE case_id=? AND id=?", (case_id, revision_id)).fetchone()
        if not found:
            raise ValueError("This revision is not in the current case.")
        db.execute("UPDATE revisions SET notes=? WHERE case_id=? AND id=?", (notes,case_id,revision_id))
        db.execute("INSERT INTO review_status VALUES (?,?) ON CONFLICT(revision_id) DO UPDATE SET status=excluded.status",
                   (revision_id,status))

def find_matching_revision(case_id, mode, prompt, seed):
    with closing(connect_db()) as db:
        row = db.execute("SELECT id FROM revisions WHERE case_id=? AND mode=? AND prompt=? AND seed=? "
                         "ORDER BY rowid DESC LIMIT 1", (case_id,mode,prompt,int(seed))).fetchone()
    return row["id"] if row else None

def generation_error_message(error):
    if isinstance(error, ProviderError):
        return str(error)
    message = str(error).lower()
    if any(word in message for word in ("zerogpu", "quota", "429", "rate limit")):
        retry = re.search(r"try again in ([0-9:.]+)", message)
        estimate = retry.group(1).rstrip(".") if retry else ""
        suffix = (" Provider retry estimate: " + estimate) if estimate and any(c in "123456789" for c in estimate) else ""
        return "The hosted Space has reached a usage limit. Your draft and saved work are safe." + suffix
    if any(word in message for word in ("401", "403", "unauthorized", "authentication", "invalid token")):
        return "The Space denied access. Run LOGIN.cmd to check your local Hugging Face login, then restart the app."
    if isinstance(error, TimeoutError):
        return "Generation timed out. The service may still be processing the request. Wait before retrying."
    if any(word in message for word in ("token limit", "sequence length", "max_sequence", "prompt too long")):
        return "The model could not accept the full description. Shorten it while preserving the important recalled details."
    if "downloaded image" in message or "cannot identify image" in message:
        return "The service did not return a readable image. Your description is saved; try again later."
    return "Generation failed. Check your connection and the hosted Space's availability. Your draft is saved."



BRIEF_RULES = {
    "Face sketch": [
        ("Face and jaw", r"\b(face|jaw|chin|forehead|cheek)\w*\b"),
        ("Hair and facial hair", r"\b(hair|bald|beard|moustache|mustache|stubble)\w*\b"),
        ("Eyes and eyebrows", r"\b(eye|eyes|eyebrow|eyebrows|eyelid)\w*\b"),
        ("Nose and mouth", r"\b(nose|mouth|lip|lips)\w*\b"),
        ("Accessories and distinguishing details", r"\b(glass|glasses|scar|mark|tattoo|earring)\w*\b"),
        ("Expression and viewpoint", r"\b(expression|neutral|smil|front|profile|tilt|gaze)\w*\b"),
    ],
    "Scene illustration": [
        ("Setting and buildings", r"\b(street|road|room|shop|store|building|house|market|lane)\w*\b"),
        ("Vehicles and objects", r"\b(motorcycle|bike|bicycle|car|truck|vehicle|bench|bag|door|window)\w*\b"),
        ("People and actions", r"\b(person|people|man|woman|adult|child|walk|run|stand|sit|riding)\w*\b"),
        ("Positions", r"\b(left|right|behind|beside|between|near|opposite|front|across)\b"),
        ("Lighting and conditions", r"\b(daylight|night|dark|rain|sun|weather|overcast|light)\w*\b"),
    ],
}
ISSUES = [
    "Missing detail", "Wrong feature or object", "Wrong position",
    "Unrequested detail", "Style mismatch", "Too much changed between versions",
]
BASES = ["Explicit in the account", "Uncertain recollection", "Presentation preference"]


def make_brief(description, mode):
    text = description.strip()
    if not text:
        raise ValueError("Enter a description before requesting a brief.")
    # Preserve whole sentences so negations and uncertainty keep their scope.
    chunks = [part.strip() for part in re.split(r"(?<=[.!?])\s+|[;\n]+", text) if part.strip()]
    rules = BRIEF_RULES[mode]
    lines = ["CRIMESKETCHAI DESCRIPTION BRIEF", "Mode: " + mode,
             "Local extractive organization of user text. No image analysis or factual verification.",
             "", "STATED DETAILS"]
    for chunk in list(dict.fromkeys(chunks)):
        topics = [title for title, pattern in rules if re.search(pattern, chunk, flags=re.I)]
        lines.append("- " + chunk)
        if topics:
            lines.append("  Topics: " + ", ".join(topics))
    uncertain = [chunk for chunk in chunks if re.search(
        r"\b(maybe|possibly|perhaps|uncertain|unknown|unsure|approximately|might|cannot recall|not sure)\b",
        chunk, flags=re.I)]
    lines += ["", "UNCERTAINTIES EXPRESSLY MENTIONED"]
    lines.extend(["- " + item for item in uncertain] or ["- No uncertainty wording detected. This does not establish certainty."])
    missing = [title for title, pattern in rules if not re.search(pattern, text, flags=re.I)]
    lines += ["", "OPTIONAL CLARIFICATION"]
    lines.extend(["- If recalled, clarify " + title.lower() + "." for title in missing]
                 or ["- Check the stated details against the full account."])
    lines += ["- Leave unknown details unspecified. Topic detection is keyword-based and can miss details.",
              "", "FULL SOURCE DESCRIPTION", text]
    return "\n".join(lines)


def save_brief(case_id, mode, description, content):
    brief_id = uuid.uuid4().hex
    with closing(connect_db()) as db, db:
        db.execute("INSERT INTO written_briefs VALUES (?,?,?,?,?,?)",
                   (brief_id,case_id,mode,description,content,now()))
    return brief_id

def latest_brief(case_id, mode):
    with closing(connect_db()) as db:
        row = db.execute("SELECT * FROM written_briefs WHERE case_id=? AND mode=? ORDER BY rowid DESC LIMIT 1",
                         (case_id,mode)).fetchone()
    return dict(row) if row else None

def list_accounts(case_id, mode=None):
    with closing(connect_db()) as db:
        if mode:
            rows = db.execute("SELECT * FROM account_records WHERE case_id=? AND mode=? ORDER BY rowid DESC", (case_id,mode)).fetchall()
        else:
            rows = db.execute("SELECT * FROM account_records WHERE case_id=? ORDER BY rowid DESC", (case_id,)).fetchall()
    return [dict(row) for row in rows]

def save_account(case_id, mode, text):
    text = text.strip()
    if not text or len(text) > 5000:
        raise ValueError("Record an account between 1 and 5000 characters.")
    record_id = uuid.uuid4().hex
    with closing(connect_db()) as db, db:
        db.execute("INSERT INTO account_records VALUES (?,?,?,?,?)", (record_id,case_id,mode,text,now()))
    return record_id

def add_provenance(case_id, account_id, detail, basis):
    if basis not in ("Explicit in the account", "Uncertain recollection", "Not supplied in the account"):
        raise ValueError("Choose a valid detail source.")
    if not detail.strip():
        raise ValueError("Enter the detail you are classifying.")
    with closing(connect_db()) as db, db:
        if not db.execute("SELECT 1 FROM account_records WHERE case_id=? AND id=?", (case_id,account_id)).fetchone():
            raise ValueError("The account does not belong to this case.")
        db.execute("INSERT INTO detail_provenance VALUES (?,?,?,?,?,?)",
                   (uuid.uuid4().hex,case_id,account_id,detail.strip(),basis,now()))

def list_provenance(case_id, account_id=None):
    with closing(connect_db()) as db:
        if account_id:
            rows = db.execute("SELECT * FROM detail_provenance WHERE case_id=? AND account_id=? ORDER BY rowid",
                              (case_id,account_id)).fetchall()
        else:
            rows = db.execute("SELECT * FROM detail_provenance WHERE case_id=? ORDER BY rowid", (case_id,)).fetchall()
    return [dict(row) for row in rows]

def suggest_description(base, expected, replace_phrase="", keep=""):
    expected = expected.strip()
    if not expected:
        raise ValueError("Describe the desired correction first.")
    result = base.strip()
    target = replace_phrase.strip()
    if target:
        if not re.search(re.escape(target), result, flags=re.I):
            raise ValueError("The wording to replace was not found. Copy the exact phrase from the description.")
        result = re.sub(re.escape(target), lambda match: expected, result, count=1, flags=re.I)
    else:
        result += "\nReviewer correction request: " + expected
    if keep.strip():
        result += "\nDetails requested unchanged: " + keep.strip()
    return result

def refinement_guidance(issue):
    return {
        "Missing detail": "A model may omit a detail even when the prompt includes it. Make the requested detail concise and prominent.",
        "Wrong feature or object": "Check whether the description contains conflicting wording. Replace the full conflicting phrase instead of adding the opposite.",
        "Wrong position": "State the relation explicitly, such as motorcycle directly in front of the shop. Text generation still cannot guarantee exact placement.",
        "Unrequested detail": "Check whether the account actually excludes this detail or leaves it unknown. Do not treat an invented detail as recalled.",
        "Style mismatch": "Keep style instructions separate from facts about the account.",
        "Too much changed between versions": "Reuse the seed and change fewer words. This does not lock identity or preserve all other details.",
    }[issue]

def save_feedback(case_id, revision_id, account_id, issue, observed, expected, basis, candidate):
    if issue not in ISSUES or basis not in BASES:
        raise ValueError("Choose valid feedback categories.")
    if not observed.strip() or not expected.strip():
        raise ValueError("Describe what you observed and what you want corrected.")
    feedback_id = uuid.uuid4().hex
    with closing(connect_db()) as db, db:
        image_row = db.execute("SELECT mode FROM revisions WHERE case_id=? AND id=?", (case_id,revision_id)).fetchone()
        if not image_row:
            raise ValueError("The image does not belong to this case.")
        if account_id:
            account_row = db.execute("SELECT mode FROM account_records WHERE case_id=? AND id=?", (case_id,account_id)).fetchone()
            if not account_row or account_row["mode"] != image_row["mode"]:
                raise ValueError("Choose an account from this case and output mode.")
        db.execute("INSERT INTO refinement_feedback VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (feedback_id,case_id,revision_id,account_id,issue,observed.strip(),expected.strip(),basis,candidate,now()))
    return feedback_id

def refinement_records(case_id):
    with closing(connect_db()) as db:
        return [dict(row) for row in db.execute("SELECT * FROM refinement_feedback WHERE case_id=? ORDER BY rowid", (case_id,))]

def brief_records(case_id):
    with closing(connect_db()) as db:
        return [dict(row) for row in db.execute("SELECT * FROM written_briefs WHERE case_id=? ORDER BY rowid", (case_id,))]



def generate_with_provider(prompt, seed, provider, key, case_id, context=None, references=None):
    if provider == "Hugging Face demo":
        st.session_state["last_provider_request"] = None
        return generate_image(prompt, seed)
    if provider == REFERENCE_PROVIDER:
        raw, actual_seed, request_id = run_reference(prompt, seed, key, DATA_DIR, case_id,
            references=references, authorized=True, context=context)
    else:
        raw, actual_seed, request_id = run_fal(prompt, seed, key, DATA_DIR, case_id, context=context)
    st.session_state["last_provider_request"] = request_id
    with Image.open(BytesIO(raw)) as original:
        picture = ImageOps.grayscale(original).convert("RGB")
    labelled = Image.new("RGB", (picture.width, picture.height + 48), "white")
    labelled.paste(picture, (0, 0))
    ImageDraw.Draw(labelled).text((12, picture.height + 17), NOTICE, fill="black")
    buffer = BytesIO()
    labelled.save(buffer, format="PNG")
    return buffer.getvalue(), int(actual_seed)



PRESENTATION_STYLES = ["Default pencil", "Soft pencil shading", "Clean ink outlines"]
OUTCOME_RATINGS = ["Improved requested detail", "No improvement", "Worse"]

def load_preferences():
    defaults = {"enabled": False, "style": "Default pencil", "white_margin": False, "reuse_patterns": False}
    with closing(connect_db()) as db:
        row = db.execute("SELECT settings FROM workspace_preferences WHERE id=1").fetchone()
    if row:
        defaults.update(json.loads(row["settings"]))
    return defaults

def save_preferences(settings):
    allowed = {"enabled", "style", "white_margin", "reuse_patterns"}
    if set(settings) != allowed or settings["style"] not in PRESENTATION_STYLES:
        raise ValueError("Only approved presentation settings can be stored as cross-case preferences.")
    if any(type(settings[key]) is not bool for key in ("enabled", "white_margin", "reuse_patterns")):
        raise ValueError("Preference switches must be true or false.")
    with closing(connect_db()) as db, db:
        db.execute("INSERT INTO workspace_preferences VALUES (1,?) ON CONFLICT(id) DO UPDATE SET settings=excluded.settings",
                   (json.dumps(settings),))

def save_outcome(case_id, feedback_id, child_id, rating, allow_cross_case):
    if rating not in OUTCOME_RATINGS:
        raise ValueError("Choose a valid description-adherence rating.")
    with closing(connect_db()) as db, db:
        feedback = db.execute("SELECT revision_id FROM refinement_feedback WHERE case_id=? AND id=?",
                              (case_id,feedback_id)).fetchone()
        child = db.execute("SELECT parent_id FROM revisions WHERE case_id=? AND id=?", (case_id,child_id)).fetchone()
        if not feedback or not child or child["parent_id"] != feedback["revision_id"]:
            raise ValueError("Rate a child revision of the image reviewed in this feedback.")
        db.execute("INSERT INTO feedback_outcomes VALUES (?,?,?,?,?,?) "
                   "ON CONFLICT(feedback_id,child_id) DO UPDATE SET rating=excluded.rating,"
                   "allow_cross_case=excluded.allow_cross_case,created=excluded.created",
                   (case_id,feedback_id,child_id,rating,int(bool(allow_cross_case)),now()))

def list_outcomes(case_id):
    with closing(connect_db()) as db:
        return [dict(row) for row in db.execute("SELECT * FROM feedback_outcomes WHERE case_id=? ORDER BY created", (case_id,))]

def pattern_suggestions(mode, exclude_case_id=None):
    # Only issue categories and rating totals leave the source case, never its descriptive text.
    with closing(connect_db()) as db:
        rows = db.execute(
            "SELECT f.issue,o.rating FROM feedback_outcomes o JOIN refinement_feedback f ON f.id=o.feedback_id "
            "JOIN revisions r ON r.id=f.revision_id WHERE o.allow_cross_case=1 AND r.mode=? "
            "AND (? IS NULL OR o.case_id != ?)", (mode,exclude_case_id,exclude_case_id)).fetchall()
    totals = {}
    for row in rows:
        counts = totals.setdefault(row["issue"], {"helpful": 0, "not_helpful": 0})
        counts["helpful" if row["rating"] == "Improved requested detail" else "not_helpful"] += 1
    return [{"issue": issue, **counts, "guidance": refinement_guidance(issue)}
            for issue, counts in sorted(totals.items())
            if counts["helpful"] > counts["not_helpful"]]

def reset_cross_case_learning():
    with closing(connect_db()) as db, db:
        db.execute("UPDATE feedback_outcomes SET allow_cross_case=0")

def revision_memory_snapshot(revision_id):
    with closing(connect_db()) as db:
        row = db.execute("SELECT snapshot FROM revision_memory WHERE revision_id=?", (revision_id,)).fetchone()
    return json.loads(row["snapshot"]) if row else {}

def make_generation_prompt(description, mode, settings=None):
    preferences = settings or load_preferences()
    prompt = build_prompt(description, mode)
    applied = []
    if preferences["enabled"]:
        if preferences["style"] == "Soft pencil shading":
            prompt += " Use soft graphite shading for the drawing style."
            applied.append("Soft pencil shading")
        elif preferences["style"] == "Clean ink outlines":
            prompt = prompt.replace("Black-and-white pencil", "Black-and-white ink", 1)
            prompt += " Use clean ink outlines for the drawing style."
            applied.append("Clean ink outlines")
        if preferences["white_margin"]:
            prompt += " Leave a small white margin around the illustration."
            applied.append("White margin")
    return prompt, {"applied_presentation": applied, "enabled": preferences["enabled"]}



def parse_quota_report(message):
    text = str(message).lower()
    if not any(word in text for word in ("zerogpu", "quota", "rate limit", "429")):
        return None
    retry_seconds = None
    clock_match = re.search(r"(?:try again in|retry in|retry after)\s+(?:(\d+)\s+days?,?\s*)?(\d+):(\d{2}):(\d{2}(?:\.\d+)?)", text)
    if clock_match:
        days, hours, minutes, seconds = clock_match.groups()
        if int(minutes) < 60 and float(seconds) < 60:
            candidate = int(days or 0)*86400 + int(hours)*3600 + int(minutes)*60 + float(seconds)
            if candidate > 0:
                retry_seconds = candidate
    else:
        duration_match = re.search(r"(?:try again in|retry in|retry after)\s+([^.;\n]+)", text)
        if duration_match:
            units = re.findall(r"(\d+(?:\.\d+)?)\s*(days?|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b",
                               duration_match.group(1))
            seconds = 0
            for number, unit in units:
                scale = 86400 if unit.startswith("d") else 3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1
                seconds += float(number)*scale
            if seconds > 0:
                retry_seconds = seconds
    allowance = re.search(r"(\d+(?:\.\d+)?)s\s+requested\s+vs\.?\s*(\d+(?:\.\d+)?)s\s+left", text)
    return {"retry_seconds": retry_seconds,
            "requested_seconds": float(allowance.group(1)) if allowance else None,
            "remaining_seconds": float(allowance.group(2)) if allowance else None}

def record_quota_failure(provider, error, observed=None):
    report = parse_quota_report(error)
    if report is None:
        return False
    observed = datetime.now(timezone.utc).timestamp() if observed is None else float(observed)
    retry_at = observed + report["retry_seconds"] if report["retry_seconds"] is not None else None
    with closing(connect_db()) as db, db:
        db.execute("INSERT INTO provider_limits VALUES (?,?,?,?,?,?) "
                   "ON CONFLICT(provider) DO UPDATE SET state=excluded.state,observed=excluded.observed,"
                   "retry_at=excluded.retry_at,requested_seconds=excluded.requested_seconds,remaining_seconds=excluded.remaining_seconds",
                   (provider,"limited",observed,retry_at,report["requested_seconds"],report["remaining_seconds"]))
    return True

def record_generation_success(provider, observed=None):
    observed = datetime.now(timezone.utc).timestamp() if observed is None else float(observed)
    with closing(connect_db()) as db, db:
        db.execute("INSERT INTO provider_limits VALUES (?,?,?,?,?,?) "
                   "ON CONFLICT(provider) DO UPDATE SET state=excluded.state,observed=excluded.observed,"
                   "retry_at=NULL,requested_seconds=NULL,remaining_seconds=NULL",
                   (provider,"success",observed,None,None,None))

def clear_quota_notice(provider):
    with closing(connect_db()) as db, db:
        db.execute("DELETE FROM provider_limits WHERE provider=?", (provider,))

def get_quota_status(provider, current_time=None):
    with closing(connect_db()) as db:
        row = db.execute("SELECT * FROM provider_limits WHERE provider=?", (provider,)).fetchone()
    if not row:
        return {"status": "not_reported"}
    result = dict(row)
    current_time = datetime.now(timezone.utc).timestamp() if current_time is None else float(current_time)
    if result["state"] == "success":
        result["status"] = "last_succeeded"
    elif result["retry_at"] is None:
        result["status"] = "reset_unknown"
    else:
        result["wait_seconds"] = max(0, math.ceil(result["retry_at"]-current_time))
        result["status"] = "estimated_wait" if result["wait_seconds"] else "estimate_elapsed"
    return result

def format_wait(seconds):
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds,3600)
    minutes, seconds = divmod(rest,60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

def format_quota_time(timestamp, use_ist=True):
    zone = timezone(timedelta(hours=5,minutes=30)) if use_ist else timezone.utc
    suffix = "IST" if use_ist else "UTC"
    return datetime.fromtimestamp(timestamp, zone).strftime("%d %b %Y, %I:%M:%S %p") + " " + suffix

def render_generation_status(provider):
    st.subheader("Generation availability")
    try:
        status = get_quota_status(provider)
    except sqlite3.Error:
        st.caption("Could not read the last provider report. Your provider allowance has not been checked.")
        return
    state = status["status"]
    if state == "estimated_wait":
        st.warning("Image generation was paused by the provider.")
        st.metric("Provider-estimated wait", format_wait(status["wait_seconds"]))
        st.caption("Estimated retry time: " + format_quota_time(status["retry_at"]))
        st.caption("UTC: " + format_quota_time(status["retry_at"], False))
        st.caption("This countdown uses the last provider error. It is not a guarantee that quota will be available then.")
    elif state == "reset_unknown":
        st.warning("Provider quota reached. Reset time unavailable.")
        st.write("The provider did not give a usable retry estimate. A 0:00:00 response is not proof that quota has reset.")
    elif state == "estimate_elapsed":
        st.info("The provider's retry estimate has elapsed. You can try Get Image again; availability is not yet confirmed.")
    elif state == "last_succeeded":
        st.success("Last image request succeeded.")
        st.caption("Remaining allowance and the next reset time are not exposed by this status panel.")
    else:
        st.caption("No quota limit has been recorded in this workspace yet.")
    if status.get("observed") is not None:
        st.caption("Last provider response: " + format_quota_time(status["observed"]))
    if status.get("requested_seconds") is not None:
        st.caption("Provider GPU allowance at that response: " + str(status["remaining_seconds"]) +
                   "s remaining; request needed " + str(status["requested_seconds"]) + "s.")
    st.caption("Cases, Get Brief, memory, history, comparison and exports remain usable. The countdown makes no provider requests.")
    if state not in ("not_reported", "last_succeeded"):
        if st.button("Dismiss saved quota notice", key="dismiss_quota_" + provider,
                     help="Clears this local notice only. It does not reset provider quota."):
            clear_quota_notice(provider)
            st.rerun()



def load_ui_theme():
    with closing(connect_db()) as db:
        row=db.execute("SELECT theme FROM ui_preferences WHERE id=1").fetchone()
    return row["theme"] if row else "dark"

def save_ui_theme(theme):
    if theme not in ("light","dark"):
        raise ValueError("Choose light or dark.")
    with closing(connect_db()) as db, db:
        db.execute("INSERT INTO ui_preferences VALUES (1,?) ON CONFLICT(id) DO UPDATE SET theme=excluded.theme", (theme,))


def persist_appearance_choice():
    try:
        save_ui_theme("light" if st.session_state.get("light_theme") else "dark")
        st.session_state["appearance_save_failed"]=False
    except sqlite3.Error:
        st.session_state["appearance_save_failed"]=True


def require_account_login(store):
    token=st.session_state.get("login_session")
    user=store.session_user(token)
    if user:
        return user
    if token:
        st.session_state.clear()
    # The login gate renders before cases, provider credentials and workspace tools.
    light=st.toggle("Light theme",value=False,key="login_light")
    try:
        css=(APP_DIR/"theme.css").read_text(encoding="utf-8")
        if light:
            css+="\n"+(APP_DIR/"light.css").read_text(encoding="utf-8")
        st.markdown("<style>"+css+"</style>",unsafe_allow_html=True)
    except OSError:
        pass
    if st.button("Back to app"):
        st.session_state.pop("account_page",None)
        st.rerun()
    st.title("CrimeSketchAI")
    intro,form_column=st.columns([1.2,1],gap="large")
    with intro:
        st.header("Your cases, in one workspace")
        st.write("Sign in to continue descriptions, illustrations and review history.")
        st.caption("Local prototype account. This login is separate from your Hugging Face or fal API credentials.")
    first_run=not store.has_users()
    with form_column:
        if first_run:
            st.info("Account sign-in is not configured yet. You can continue using the app as a guest.")
            st.stop()
        else:
            st.subheader("Sign in")
            with st.form("account_login",clear_on_submit=False):
                username=st.text_input("Username",max_chars=40)
                password=st.text_input("Password",type="password",max_chars=256)
                sign_in=st.form_submit_button("Sign in",type="primary",width="stretch")
            if sign_in:
                try:
                    login_token=store.login(username,password)
                    st.session_state.clear()
                    st.session_state["login_session"]=login_token
                    st.rerun()
                except AuthError as error:
                    st.error(str(error))
                except sqlite3.Error:
                    st.error("Could not complete sign-in. Try again.")
            st.caption("Need an account? Ask the local workspace owner. Forgotten password? See LOGIN_HELP.md.")
    st.stop()

def log_out_of_workspace(store):
    store.logout(st.session_state.get("login_session"))
    st.session_state.clear()
    st.rerun()


def show_revision(item, key):
    st.image(item["image"], caption=NOTICE, width="stretch")
    st.caption(f"{item['mode']} · Seed {item['seed']} · {item['created']} (UTC)")
    st.caption("Generated via " + item.get("provider", "Hugging Face demo"))
    snapshot = revision_memory_snapshot(item["id"])
    if snapshot.get("applied_presentation"):
        st.caption("Saved preference influence: " + ", ".join(snapshot["applied_presentation"]))
    st.write(item["description"])
    st.download_button("Download PNG", item["image"],
                       file_name="crimesketch_" + item["id"][:8] + ".png",
                       mime="image/png", key="download_" + key)

try:
    init_db()
except (OSError, sqlite3.Error):
    st.error("Cannot open the workspace database. Check that this project folder is writable.")
    st.stop()

AUTH_ROOT=DATA_DIR
try:
    account_store=AuthStore(AUTH_ROOT)
    if not account_store.has_users():
        owner_username=hosting_secrets.get("OWNER_USERNAME", "")
        owner_password=hosting_secrets.get("OWNER_PASSWORD", "")
        if owner_username and owner_password:
            try:
                account_store.create_user(str(owner_username),"Workspace owner",str(owner_password),first=True)
            except AuthError:
                if not account_store.has_users():
                    st.error("Owner credentials in hosting secrets are invalid. Use a valid username and a passphrase of 15–256 characters.")
                    st.stop()
    signed_in_user=account_store.session_user(st.session_state.get("login_session"))
    if not signed_in_user and st.session_state.get("login_session"):
        st.session_state.clear()
    if not signed_in_user and st.session_state.get("account_page")=="login":
        signed_in_user=require_account_login(account_store)
    if signed_in_user:
        DATA_DIR=account_store.workspace_for(signed_in_user)
    else:
        if "guest_workspace" not in st.session_state:
            st.session_state["guest_workspace"]=uuid.uuid4().hex
        DATA_DIR=AUTH_ROOT/"guest_sessions"/st.session_state["guest_workspace"]
    DB_PATH=DATA_DIR/"workspace.sqlite3"
    init_db()
    if not signed_in_user and not list_cases():
        create_case("Untitled session")
except (OSError,sqlite3.Error):
    st.error("Could not open this account's workspace.")
    st.stop()

quota_status_fragment = st.fragment(run_every=1)(render_generation_status)

title_column, appearance_column=st.columns([5,1])
with appearance_column:
    if not signed_in_user and st.button("Log in",key="optional_login",width="stretch"):
        st.session_state["account_page"]="login"
        st.rerun()
    light_theme=st.toggle("Light theme", value=load_ui_theme()=="light", key="light_theme",
                          on_change=persist_appearance_choice)
    selected_theme="light" if light_theme else "dark"
    if st.session_state.get("appearance_save_failed"):
        st.caption("Appearance changed for this session, but could not be saved.")
try:
    workspace_css=(APP_DIR/"theme.css").read_text(encoding="utf-8")
    if selected_theme=="light":
        workspace_css+="\n"+(APP_DIR/"light.css").read_text(encoding="utf-8")
except OSError:
    workspace_css=""
if workspace_css:
    st.markdown("<style>"+workspace_css+"</style>",unsafe_allow_html=True)
with title_column:
    st.markdown('<div class="cs-spectrum"></div><div class="cs-eyebrow">VISUAL RECONSTRUCTION WORKSPACE</div>',
                unsafe_allow_html=True)
    st.title("CrimeSketchAI")
st.caption("A case workspace for visual reconstruction and review")
with st.expander("Illustration limitations and data handling"):
    st.write("AI images can invent or omit details and cannot verify identity or events. "
             "Use fictional accounts. Generation sends your description to the selected external provider.")

with st.sidebar:
    if signed_in_user:
        st.caption("Signed in as " + signed_in_user["display_name"] + " (" + signed_in_user["username"] + ")")
        if st.button("Log out",key="account_logout",width="stretch"):
            log_out_of_workspace(account_store)
    else:
        st.caption("Guest workspace")
        st.caption("No account needed. Download your work before leaving. Guest work is separate from account history; local files may remain on this computer.")
    st.subheader("Case navigator")
    case_navigation=st.container()
    quota_state=get_quota_status(st.session_state.get("generation_provider","Hugging Face demo"))["status"]
    status_panel=st.expander("Generation status",expanded=quota_state in ("estimated_wait","reset_unknown","estimate_elapsed"))
    settings_panel=st.expander("Settings",expanded=False)
    with settings_panel:
        st.subheader("Account")
        if signed_in_user:
            with st.form("change_account_password",clear_on_submit=True):
                current_password=st.text_input("Current password",type="password",max_chars=256)
                new_password=st.text_input("New password",type="password",max_chars=256)
                new_password_confirmation=st.text_input("Confirm new password",type="password",max_chars=256)
                change_password=st.form_submit_button("Change password")
            if change_password:
                if new_password!=new_password_confirmation:
                    st.error("The new passwords do not match.")
                else:
                    try:
                        account_store.change_password(st.session_state["login_session"],current_password,new_password)
                        log_out_of_workspace(account_store)
                    except AuthError as error:
                        st.error(str(error))
            if signed_in_user["role"]=="admin":
                with st.expander("Manage local accounts"):
                    with st.form("create_workspace_user",clear_on_submit=True):
                        member_username=st.text_input("New account username",max_chars=40)
                        member_name=st.text_input("New account display name",max_chars=80)
                        member_password=st.text_input("New account password",type="password",max_chars=256)
                        add_member=st.form_submit_button("Create account")
                    if add_member:
                        try:
                            account_store.create_user(member_username,member_name,member_password,
                                                      admin_token=st.session_state["login_session"])
                            st.success("Account created. Its cases are separate from yours.")
                        except AuthError as error:
                            st.error(str(error))
                    st.caption("Each additional account starts with an empty private case database.")
        st.header("Generation backend")
        generation_provider = st.selectbox("Image provider", ["Hugging Face demo", "fal.ai API"],
                                           key="generation_provider")
        fal_api_key = ""
        paid_authorized = False
        if generation_provider == "fal.ai API":
            st.caption("Paid FLUX.1-schnell API. Published rate checked 28 Oct 2026: $0.003 per rounded-up megapixel. "
                       "At 768×768, about $0.003 per image. A funded account is required; verify current billing.")
            fal_api_key = st.text_input("fal API key (private)", type="password", key="fal_api_key",
                                        help="Kept in this browser session only. Not saved in cases or exports.") or os.environ.get("FAL_KEY", "")
            paid_authorized = st.checkbox("I authorize paid image requests using this fal account",
                                          value=False, key="authorize_paid")
            st.caption("No automatic fallback or purchase. This avoids ZeroGPU limits, but provider billing and rate limits still apply.")
        else:
            st.warning("Public demo backend: ZeroGPU quotas apply. This route is unsuitable for uninterrupted iteration.")
        provider_ready = generation_provider == "Hugging Face demo" or (bool(fal_api_key.strip()) and paid_authorized)
        st.header("Workspace")
        st.caption("Hugging Face login detected" if get_token() else "No local Hugging Face login detected")
        with st.expander("Workspace backup"):
            st.caption("This backup includes this account's cases and images. Login credentials and other users' cases are excluded.")
            if st.button("Prepare full backup"):
                try:
                    st.session_state["workspace_backup"] = database_backup()
                except (OSError, sqlite3.Error):
                    st.error("Could not prepare the backup. Check free disk space and try again.")
            if st.session_state.get("workspace_backup"):
                st.download_button("Download database backup", st.session_state["workspace_backup"],
                                   file_name="crimesketch_workspace_backup.sqlite3", mime="application/octet-stream")
            st.caption("Prepare again to capture changes made after a previous backup.")
        st.caption("Cases and images save in this account's local workspace. The database is not encrypted.")
        st.caption("Generation requests in this browser session: " + str(st.session_state.get("request_count", 0)))
        st.caption("This counter is not your provider quota. Browsing saved work uses no image-generation quota.")
        with st.expander("Help and storage"):
            st.write("Active data folder:")
            st.code(str(DATA_DIR), language=None)
            st.write("Use History and review to revisit a version, or Compare to inspect two images.")
            st.write("When quota runs out, existing cases, history, comparison and exports remain available.")
            st.write("If login is missing, run LOGIN.cmd from the app folder. Never paste tokens into descriptions.")
        memory_tab=st.container()
    with status_panel:
        st.caption("Backend: " + generation_provider)
        quota_status_fragment(generation_provider)
    with case_navigation:
        with st.form("new_case", clear_on_submit=True):
            case_name = st.text_input("New case name", max_chars=100, placeholder="Fictional market incident")
            create = st.form_submit_button("Create case")
        if create:
            try:
                st.session_state["case_select"] = create_case(case_name)
                st.session_state["case_search"] = ""
                st.session_state["show_archived"] = False
                st.rerun()
            except ValueError as error:
                st.warning(str(error))
            except sqlite3.Error:
                st.error("Could not save the case. Check available disk space and try again.")
        all_cases = list_cases()
        show_archived = st.checkbox("Show archived cases", value=False, key="show_archived")
        search = st.text_input("Search cases", placeholder="Case name or ID", key="case_search").strip().casefold()
        cases = [case for case in all_cases
                 if (show_archived or not case_is_archived(case["id"]))
                 and (not search or search in case["name"].casefold() or search in case["id"].casefold())]
        if not cases:
            st.info("No matching cases. Clear the search, show archived cases, or create a case.")
            st.stop()
        names = {case["id"]: case["name"] for case in cases}
        if st.session_state.get("case_select") not in names:
            st.session_state["case_select"] = cases[0]["id"]
        case_id = st.selectbox("Open case", list(names),
                              format_func=lambda value: names[value] + " (" + value[:6] + ")",
                              key="case_select")

st.header(names[case_id])
archived = case_is_archived(case_id)
if archived:
    st.info("Archived case. Its saved work remains available. Restore it to add new illustrations.")
with settings_panel:
    with st.expander("Case settings"):
        with st.form("rename_" + case_id):
            revised_name = st.text_input("Case name", value=names[case_id], max_chars=100)
            rename = st.form_submit_button("Save case name")
        if rename:
            try:
                rename_case(case_id, revised_name)
                invalidate_exports(case_id)
                st.rerun()
            except ValueError as error:
                st.warning(str(error))
            except sqlite3.Error:
                st.error("Could not rename this case. Please try again.")
        st.caption("Archiving preserves saved images, descriptions and review notes.")
        if st.button("Restore case" if archived else "Archive case", key="archive_" + case_id):
            try:
                set_case_archived(case_id, not archived)
                st.rerun()
            except sqlite3.Error:
                st.error("Could not change the archive status. Please try again.")
pending_jobs = list_pending(DATA_DIR, case_id)
if pending_jobs:
    with st.expander("Recover provider requests", expanded=True):
        st.write("Accepted requests can be resumed without a new generation submission.")
        for index, job in enumerate(pending_jobs):
            if job.get("model") == REFERENCE_MODEL:
                st.caption("Reference request: " + job.get("request_id", "submission outcome unknown") + " / " + job["state"])
                resume_key = st.text_input("fal key to resume references",type="password",key="ref_resume_key_"+str(index))
                if not resume_key and signed_in_user and signed_in_user["role"]=="admin":
                    resume_key=os.environ.get("FAL_KEY", "")
                resume_permission=st.checkbox("I authorize retrieval of this existing reference request",key="ref_resume_permission_"+str(index))
                if job["state"] == "pending" and st.button("Resume reference request " + str(index+1),disabled=not resume_key or not resume_permission,key="ref_resume_"+str(index)):
                    st.session_state["reference_resume_"+case_id]=(job,resume_key)
                    st.rerun()
                elif job["state"] != "pending":
                    st.warning("Submission outcome is uncertain. Check fal request history before starting another request.")
                continue
            st.caption("Request: " + job.get("request_id", "submission outcome unknown") + " / " + job["state"])
            if job["state"] != "pending":
                st.warning("Check fal request history before submitting this input again. The submission outcome is uncertain.")
            elif st.button("Resume provider request " + str(index + 1), disabled=not provider_ready or generation_provider != "fal.ai API",
                           key="resume_provider_" + job["request_id"]):
                st.session_state["resume_job_" + case_id] = job
                st.session_state["mode_" + case_id] = "Face sketch" if "head-and-shoulders portrait" in job["prompt"] else "Scene illustration"
                st.rerun()
revisions = list_revisions(case_id)
pending_reviews=sum(get_review_status(row["id"])!="Reviewed" for row in revisions)
st.caption(f"{len(revisions)} saved versions / {pending_reviews} awaiting review")
workspace_area, review_area, records_area=st.tabs(["Workspace","Review & Compare","Case Records"])
with workspace_area:
    create_tab=st.container()
with review_area:
    refine_tab, compare_tab, timeline_tab, history_tab=st.tabs(
        ["Correct an image","Side-by-side","Revision timeline","Saved versions"])
with records_area:
    account_tab, export_tab=st.tabs(["Account & uncertainties","Reports & exports"])

pending = st.session_state.pop("reuse_revision", None)
mode_key = "mode_" + case_id
description_key = "description_" + case_id
seed_key = "seed_" + case_id
parent_key = "parent_" + case_id
if pending and pending["case_id"] == case_id:
    st.session_state[mode_key] = pending["mode"]
    st.session_state["description_" + case_id + "_" + pending["mode"]] = pending["description"]
    st.session_state[seed_key] = pending["seed"]
    st.session_state["random_" + case_id] = False
    st.session_state["force_" + case_id] = False
    st.session_state["parent_" + case_id + "_" + pending["mode"]] = pending["id"]

with create_tab:
    reference_recovery=st.session_state.pop("reference_resume_"+case_id,None)
    if reference_recovery:
        job,recovery_key=reference_recovery
        try:
            recovered_png,recovered_seed=generate_with_provider(job["prompt"],job["seed"],REFERENCE_PROVIDER,recovery_key,case_id)
            context=job.get("context") or {}
            st.session_state["unsaved_"+case_id]=dict(image=recovered_png,mode="Face sketch",
                description=context.get("description",job["prompt"]),prompt=job["prompt"],seed=recovered_seed,
                parent_id=context.get("parent_id"),created=now(),id=uuid.uuid4().hex,provider=REFERENCE_PROVIDER,
                request_id=st.session_state.get("last_provider_request"),memory_snapshot=context.get("memory_influence"))
            new_id=save_revision(case_id,"Face sketch",context.get("description",job["prompt"]),job["prompt"],recovered_seed,recovered_png,
                context.get("parent_id"),REFERENCE_PROVIDER,st.session_state.get("last_provider_request"),context.get("memory_influence"))
            st.session_state.pop("unsaved_"+case_id,None)
            st.session_state["latest_"+case_id]=new_id
            invalidate_exports(case_id)
            st.rerun()
        except Exception as error:
            st.error(generation_error_message(error))
    resume_job = st.session_state.pop("resume_job_" + case_id, None)
    if resume_job and provider_ready and generation_provider == "fal.ai API":
        try:
            with st.spinner("Resuming the accepted fal request; no new submission...", show_time=True):
                recovered_png, recovered_seed = generate_with_provider(resume_job["prompt"], resume_job["seed"],
                                                                        generation_provider, fal_api_key, case_id)
            record_generation_success(generation_provider)
            recovered_context = resume_job.get("context") or {}
            recovered_mode = recovered_context.get("mode", st.session_state.get("mode_" + case_id, "Scene illustration"))
            # This is the actual submitted prompt, retained verbatim when no separate description is available.
            recovered_id = save_revision(case_id, recovered_mode, recovered_context.get("description", resume_job["prompt"]), resume_job["prompt"],
                                         recovered_seed, recovered_png, parent_id=recovered_context.get("parent_id"), provider=generation_provider,
                                         request_id=st.session_state.get("last_provider_request"),
                                         memory_snapshot=recovered_context.get("memory_influence"))
            st.session_state["latest_" + case_id] = recovered_id
            invalidate_exports(case_id)
            st.rerun()
        except (ProviderError, OSError, sqlite3.Error) as error:
            try:
                record_quota_failure(generation_provider, error)
            except sqlite3.Error:
                pass
            st.error(generation_error_message(error) if isinstance(error, ProviderError) else
                     "Could not save the recovered request. Its provider result remains retrievable.")
    flash = st.session_state.pop("flash_" + case_id, None)
    if flash:
        st.success(flash)
    focus_viewer=st.toggle("Focus image viewer",value=False,key="focus_viewer_"+case_id)
    if focus_viewer:
        viewer_column=st.container()
        inspector_column=st.expander("Account inspector",expanded=False)
    else:
        viewer_column, inspector_column=st.columns([1.35,1],gap="large")
    left, right=inspector_column, viewer_column
    with left:
        st.subheader("Account inspector")
        mode = st.radio("Output mode", list(EXAMPLES), horizontal=True, key=mode_key)
        description_key = "description_" + case_id + "_" + mode
        parent_key = "parent_" + case_id + "_" + mode
        saved_draft = get_draft(case_id, mode)
        if description_key not in st.session_state:
            st.session_state[description_key] = saved_draft["description"]
        parent_id = st.session_state.get(parent_key)
        if parent_id:
            st.caption("Revising saved version " + parent_id[:8] + ". The original stays saved.")
            if st.button("Start an independent version", key="clear_parent_" + case_id):
                st.session_state.pop(parent_key, None)
                st.rerun()
        action_key = "draft_action_" + case_id + mode
        undo_key = "draft_undo_" + case_id + mode
        action = st.session_state.pop(action_key, None)
        if action:
            if action == "undo" and undo_key in st.session_state:
                previous = st.session_state.pop(undo_key)
                st.session_state[description_key] = previous["description"]
                guided_restore = previous["fields"]
            else:
                guided_restore = {}
                st.session_state[undo_key] = {
                    "description": st.session_state.get(description_key, saved_draft["description"]),
                    "fields": dict(saved_draft["fields"]),
                }
                st.session_state[description_key] = EXAMPLES[mode] if action == "example" else ""
            save_draft(case_id, mode, st.session_state[description_key], guided_restore)
            saved_draft = get_draft(case_id, mode)
            for field in list(st.session_state):
                if field.startswith("field_" + case_id + mode):
                    st.session_state.pop(field, None)
            st.session_state.pop(parent_key, None)
            parent_id = None
        with st.expander("Fictional example"):
            st.write(EXAMPLES[mode])
            if st.button("Use this example", key="example_" + case_id + mode):
                st.session_state[action_key] = "example"
                st.rerun()
        tools = st.columns(2)
        with tools[0]:
            if st.button("Clear draft", key="clear_" + case_id + mode):
                st.session_state[action_key] = "clear"
                st.rerun()
        with tools[1]:
            if st.button("Undo draft replacement", disabled=undo_key not in st.session_state,
                         key="undo_" + case_id + mode):
                st.session_state[action_key] = "undo"
                st.rerun()
        input_style = st.radio("Description entry", ["Free text", "Guided fields"],
                               horizontal=True, key="entry_" + case_id)
        if input_style == "Guided fields":
            st.caption("Leave anything unknown blank. These fields organize your account without guessing missing details.")
            if mode == "Face sketch":
                field_labels = [
                    ("Overall appearance", "Fictional adult, approximate age if recalled"),
                    ("Face shape and jaw", "Oval face, rounded chin"),
                    ("Hair", "Short curly hair"),
                    ("Eyes and eyebrows", "Thick eyebrows, hooded eyelids"),
                    ("Nose and mouth", "Broad nose, full lips"),
                    ("Facial hair", "Moustache, no beard"),
                    ("Distinctive visible features", "Small scar on left cheek"),
                    ("Pose and expression", "Front view, neutral expression"),
                ]
            else:
                field_labels = [
                    ("Setting", "Narrow street or indoor room"),
                    ("Buildings and landmarks", "Grocery shop on the left"),
                    ("Vehicles and objects", "Motorcycle, wooden bench"),
                    ("People and visible actions", "No people visible"),
                    ("Positions and relationships", "Motorcycle directly in front of the shop"),
                    ("Lighting and weather", "Daylight, overcast sky"),
                    ("Viewpoint", "Eye level, facing along the road"),
                    ("Other recalled details", "Only details explicitly recalled"),
                ]
            parts = []
            for label, example in field_labels:
                field_key = "field_" + case_id + mode + label
                if field_key not in st.session_state:
                    st.session_state[field_key] = saved_draft["fields"].get(label, "")
                value = st.text_input(label, placeholder=example, max_chars=350,
                                      key="field_" + case_id + mode + label)
                if value.strip():
                    parts.append(label + ": " + value.strip())
            assembled = ". ".join(parts)
            if st.button("Use fields in description", key="assemble_" + case_id + mode):
                if not assembled:
                    st.warning("Fill in at least one known detail first.")
                else:
                    st.session_state[undo_key] = {
                        "description": st.session_state.get(description_key, ""),
                        "fields": dict(saved_draft["fields"]),
                    }
                    st.session_state[description_key] = assembled
                    st.success("Fields copied below. Review and edit the description before generating.")
        description = st.text_area("Description to generate", height=230, max_chars=5000,
                                   key=description_key, placeholder=EXAMPLES[mode])
        prepared_references = []
        reference_authorized = False
        reference_key = ""
        reference_generate = False
        if mode == "Face sketch":
            prepared_references = render_reference_board(case_id)
            with st.expander("Paid reference generation", expanded=bool(prepared_references)):
                st.caption("Separate route: fal FLUX Kontext experimental multi-image. Get Image below remains text-only. References guide appearance; they cannot verify identity or guarantee feature accuracy.")
                reference_key = st.text_input("fal key for references (private)",type="password",key="reference_api_key")
                if not reference_key and signed_in_user and signed_in_user["role"] == "admin":
                    reference_key = os.environ.get("FAL_KEY", "")
                    if reference_key:
                        st.caption("Using the owner's configured fal key.")
                st.caption("A funded fal account is required. Pricing is set by the model provider; review it before enabling. No paid requests occur on upload, preview, or Get Brief.")
                st.link_button("View reference model pricing", "https://fal.ai/models/fal-ai/flux-pro/kontext/multi")
                reference_authorized = st.checkbox("I authorize paid reference requests and sending these crops and instructions to fal",value=False,key="reference_authorized_"+case_id)
                reference_generate = st.button("Generate with references · paid",type="secondary",
                    disabled=archived or not prepared_references or not reference_key or not reference_authorized or bool(st.session_state.get("unsaved_"+case_id)),key="reference_generate_"+case_id)
                if not reference_key:
                    st.caption("Enter your own fal key to enable. Guests cannot use the owner's server key.")
        actions = st.columns(2)
        with actions[0]:
            generate = st.button("Get Image", type="primary", width="stretch",
                                 disabled=archived or not provider_ready or bool(st.session_state.get("unsaved_" + case_id)))
        with actions[1]:
            get_brief = st.button("Get Brief", width="stretch", key="brief_action_" + case_id + mode)
        st.caption("Get Image uses the selected provider and may incur charges. Get Brief runs locally and does not analyze the image.")
        guided_values = dict(saved_draft["fields"])
        if input_style == "Guided fields":
            guided_values.update({label: st.session_state.get("field_" + case_id + mode + label, "")
                                  for label, _ in field_labels})
        try:
            save_draft(case_id, mode, description, guided_values)
        except sqlite3.Error:
            st.warning("Draft could not be saved. Copy your text before leaving this page.")
        st.caption(f"{len(description.strip())}/900 characters for generation. Drafts save when you leave the field.")
        st.caption("The character limit is a prototype safeguard, not a guarantee every detail fits the model's token budget.")
        if len(description.strip()) > 900:
            st.warning("This draft is too long to generate. Shorten it to 900 characters or fewer; the full draft remains saved.")
        with st.expander("Review the exact prompt before generation"):
            if description.strip():
                st.code(make_generation_prompt(description.strip(), mode)[0], language=None)
                st.caption("Blank guided fields add no text. The image model can still invent unspecified details.")
            else:
                st.write("Enter a description or assemble guided fields to preview the prompt.")
        preferences = load_preferences()
        _, influence_preview = make_generation_prompt(description.strip(), mode, preferences)
        with st.expander("Memory influencing this case", expanded=bool(influence_preview["applied_presentation"])):
            if influence_preview["applied_presentation"]:
                st.write("Saved presentation choices: " + ", ".join(influence_preview["applied_presentation"]))
            else:
                st.write("No saved presentation preferences are changing this prompt.")
            case_feedback = [row for row in refinement_records(case_id) if
                             get_revision(case_id,row["revision_id"])["mode"] == mode]
            if case_feedback:
                st.write("Recent feedback in this case, for reference only:")
                for entry in case_feedback[-3:]:
                    st.text(entry["issue"] + ": " + entry["expected"] + " [" + entry["basis"] + "]")
            if preferences["reuse_patterns"]:
                suggestions = pattern_suggestions(mode, case_id)
                for suggestion in suggestions:
                    st.write(suggestion["issue"] + ": " + suggestion["guidance"])
                    st.caption("Why shown: explicitly shared ratings in other cases: "
                               + str(suggestion["helpful"]) + " improved, " + str(suggestion["not_helpful"]) + " not improved.")
                if not suggestions:
                    st.write("No positively rated, shared guidance patterns are available yet.")
            st.caption("Suggestions do not add case facts to the prompt. Apply only guidance that fits the current account.")
        with st.expander("Before using generation quota"):
            st.write("Check which details are explicitly recalled, which are unknown, and what changed since the last version.")
            st.write("Use History and review to record mismatches. Edit the complete description before sending another request.")
            st.write("A fixed seed helps comparisons, but it does not lock facial identity or object positions.")
        with st.expander("Generation settings"):
            if seed_key not in st.session_state:
                st.session_state[seed_key] = 42
            seed = st.number_input("Seed", 0, 2147483647, value=None, step=1, key=seed_key)
            if seed is None:
                seed = 42
            vary = st.checkbox("Use a random seed", key="random_" + case_id)
            force_new = st.checkbox("Regenerate even when these exact settings already have a saved result",
                                    key="force_" + case_id, value=False, disabled=generation_provider == "fal.ai API")
            if generation_provider == "fal.ai API":
                force_new = False
                st.caption("Paid requests reuse the exact input's saved job. Choose a new seed for an intentionally new request.")
            st.caption("By default, matching saved results reopen without using your generation quota.")
        if get_brief:
            if not description.strip():
                st.warning("Enter a description before requesting a brief.")
            else:
                try:
                    content = make_brief(description, mode)
                    save_brief(case_id, mode, description.strip(), content)
                    invalidate_exports(case_id)
                    st.session_state["show_brief_" + case_id] = True
                    st.success("Brief saved. Open Written brief on the right. No image quota used.")
                except (ValueError, sqlite3.Error) as error:
                    st.error(str(error) if isinstance(error, ValueError) else "Could not save this brief.")
        if reference_generate:
            generation_provider = REFERENCE_PROVIDER
            fal_api_key = reference_key
            provider_ready = bool(reference_key) and reference_authorized
            force_new = False
        if (generate or reference_generate) and provider_ready and not st.session_state.get("unsaved_" + case_id):
            clean = description.strip()
            if not clean:
                st.warning("Enter a description first.")
            elif len(clean) > 900:
                st.warning("Shorten the description to 900 characters or fewer. Nothing was sent.")
            else:
                request_seed = secrets.randbelow(2147483648) if vary else int(seed)
                prompt, memory_influence = make_generation_prompt(clean, mode)
                reference_input_error = None
                if reference_generate:
                    try:
                        prompt, _, reference_plan = prepare_references(prompt, prepared_references)
                        memory_influence["reference_plan"] = reference_plan
                        memory_influence["reference_model"] = REFERENCE_MODEL
                    except ProviderError as error:
                        reference_input_error = str(error)
                if reference_input_error:
                    st.warning(reference_input_error)
                    st.stop()
                match_id = find_matching_revision(case_id, mode, prompt, request_seed)
                if match_id and not force_new:
                    st.session_state["latest_" + case_id] = match_id
                    st.session_state["flash_" + case_id] = "Opened the matching saved result without making a new generation request."
                    st.rerun()
                try:
                    st.session_state["request_count"] = st.session_state.get("request_count", 0) + 1
                    with st.spinner("Generating with the selected provider. There may be a queue...", show_time=True):
                        png, actual_seed = generate_with_provider(prompt, request_seed, generation_provider, fal_api_key, case_id,
                                                                        {"description": clean, "mode": mode, "parent_id": parent_id, "memory_influence": memory_influence},
                                                                        references=prepared_references if reference_generate else None)
                except Exception as error:
                    try:
                        record_quota_failure(generation_provider, error)
                    except sqlite3.Error:
                        pass
                    st.error(generation_error_message(error))
                else:
                    try:
                        record_generation_success(generation_provider)
                    except sqlite3.Error:
                        pass
                    # Retain the returned image even if disk persistence fails.
                    st.session_state["unsaved_" + case_id] = dict(
                        image=png, mode=mode, description=clean, prompt=prompt,
                        seed=actual_seed, parent_id=parent_id, created=now(), id=uuid.uuid4().hex,
                        provider=generation_provider, request_id=st.session_state.get("last_provider_request"),
                        memory_snapshot=memory_influence,
                    )
                    try:
                        new_id = save_revision(case_id, mode, clean, prompt, actual_seed, png, parent_id, generation_provider, st.session_state.get("last_provider_request"), memory_influence)
                    except (sqlite3.Error, OSError):
                        # Re-render immediately so generation locks while the unsaved result is recoverable.
                        st.rerun()
                    else:
                        st.session_state.pop("unsaved_" + case_id, None)
                        st.session_state["latest_" + case_id] = new_id
                        invalidate_exports(case_id)
                        st.rerun()
    with right:
        brief = latest_brief(case_id, mode)
        with st.expander("Written brief", expanded=bool(st.session_state.get("show_brief_" + case_id))):
            if brief:
                if brief["description"] != description.strip():
                    st.warning("This brief belongs to an earlier description. Select Get Brief to update it.")
                st.text(brief["content"])
                st.download_button("Download brief TXT", brief["content"],
                                   file_name="crimesketch_brief.txt", mime="text/plain",
                                   key="brief_download_" + case_id + mode)
            else:
                st.write("Select Get Brief to organize your description without an image-generation request.")
        st.subheader("Image workspace")
        unsaved = st.session_state.get("unsaved_" + case_id)
        latest_id = st.session_state.get("latest_" + case_id)
        if not latest_id and revisions:
            latest_id = revisions[0]["id"]
        latest = unsaved or (get_revision(case_id, latest_id) if latest_id else None)
        if latest:
            if unsaved:
                st.warning("This image has not saved to the case. Download it below or retry saving without generating again.")
                if st.button("Retry saving image", key="retry_save_" + case_id):
                    try:
                        recovered_id = save_revision(case_id, unsaved["mode"], unsaved["description"],
                                                     unsaved["prompt"], unsaved["seed"], unsaved["image"],
                                                     unsaved["parent_id"], unsaved.get("provider", "Hugging Face demo"),
                                                     unsaved.get("request_id"), unsaved.get("memory_snapshot"))
                    except (sqlite3.Error, OSError):
                        st.error("Still unable to save. Download the image before closing the app.")
                    else:
                        st.session_state.pop("unsaved_" + case_id, None)
                        st.session_state["latest_" + case_id] = recovered_id
                        invalidate_exports(case_id)
                        st.rerun()
            if latest["description"] != description.strip() or latest["mode"] != mode:
                st.caption("Saved result below belongs to the displayed description, not the current draft.")
            latest_rows={row["id"]:len(revisions)-index for index,row in enumerate(revisions)}
            if latest["id"] in latest_rows:
                st.caption("Viewing saved version " + str(latest_rows[latest["id"]]) + " / " +
                           get_review_status(latest["id"]))
            show_revision(latest, "latest_" + case_id)
            if not unsaved and st.button("Continue refining this image",key="continue_"+latest["id"]):
                st.session_state["refine_base_"+case_id]=latest["id"]
                st.session_state["flash_"+case_id]="Image selected for correction. Open Review & Compare, then Correct an image."
                st.rerun()
        else:
            st.write("Your generated illustration will appear here and save to this case.")

lookup = {item["id"]: item for item in revisions}
numbers = {item["id"]: len(revisions) - index for index, item in enumerate(revisions)}
def revision_label(revision_id):
    item = lookup[revision_id]
    preferred = " · Preferred" if get_preferred(case_id, item["mode"]) == revision_id else ""
    return f"Version {numbers[revision_id]} · {item['mode']} · Seed {item['seed']}" + preferred



with memory_tab:
    st.subheader("Editable workspace memory")
    st.write("This is application memory around a pretrained model. It does not retrain FLUX.")
    preferences = load_preferences()
    with st.form("memory_preferences"):
        enabled = st.checkbox("Apply saved presentation preferences", value=preferences["enabled"])
        style = st.selectbox("Preferred drawing style", PRESENTATION_STYLES,
                             index=PRESENTATION_STYLES.index(preferences["style"]))
        white_margin = st.checkbox("Prefer a small white margin", value=preferences["white_margin"])
        reuse_patterns = st.checkbox("Show helpful shared prompt-writing patterns",
                                      value=preferences["reuse_patterns"])
        st.caption("Only these presentation settings can change prompts. Shared patterns appear as suggestions.")
        save_memory = st.form_submit_button("Save memory settings")
    if save_memory:
        try:
            save_preferences({"enabled": enabled, "style": style,
                              "white_margin": white_margin, "reuse_patterns": reuse_patterns})
            st.rerun()
        except (ValueError, sqlite3.Error):
            st.error("Could not save memory settings.")
    st.write("Memory boundaries")
    st.write("People, facial features, objects, locations and account wording are not reused across cases. "
             "Unknown details stay unknown. Preferred pictures and review status are not truth labels.")
    st.caption("These preferences apply to your signed-in workspace. Other accounts have separate preferences and case data.")
    with st.expander("Shared patterns currently available"):
        found_patterns = False
        for memory_mode in EXAMPLES:
            for pattern in pattern_suggestions(memory_mode):
                found_patterns = True
                st.write(memory_mode + " / " + pattern["issue"])
                st.text(pattern["guidance"])
                st.caption(str(pattern["helpful"]) + " improved ratings; " + str(pattern["not_helpful"]) +
                           " neutral or worse ratings. User feedback, not measured forensic accuracy.")
        if not found_patterns:
            st.write("None yet. Rate actual revisions in Refine image and explicitly allow a rating to be shared.")
    if st.button("Stop reusing all shared ratings"):
        reset_cross_case_learning()
        st.success("Shared ratings disabled. Case feedback and review history are preserved.")

with timeline_tab:
    st.subheader("Revision timeline")
    st.caption("See the account wording, the requested correction and the resulting versions together.")
    timeline_mode = st.selectbox("Timeline mode", ["All modes"] + list(EXAMPLES), key="timeline_mode_" + case_id)
    timeline_rows = [row for row in revisions if timeline_mode == "All modes" or row["mode"] == timeline_mode]
    if not timeline_rows:
        st.info("No saved versions in this selection.")
    else:
        timeline_feedback = refinement_records(case_id)
        timeline_outcomes = list_outcomes(case_id)
        if len(timeline_rows) > 20:
            st.caption("Showing the latest 20 versions. All versions remain available in History.")
        for row in reversed(timeline_rows[:20]):
            with st.expander(revision_label(row["id"]) + " / " + row["created"],
                             expanded=row["id"] == timeline_rows[0]["id"]):
                if row["parent_id"]:
                    parent_label = revision_label(row["parent_id"]) if row["parent_id"] in lookup else row["parent_id"][:8]
                    st.write("Based on: " + parent_label)
                    requested = [entry for entry in timeline_feedback if entry["revision_id"] == row["parent_id"]]
                    for entry in requested:
                        st.text("Feedback on the parent image: " + entry["observed"] +
                                "\nRequested correction: " + entry["expected"] +
                                "\nBasis: " + entry["basis"])
                    if requested:
                        st.caption("These are recorded requests about the parent. Their presence does not prove every request influenced this child.")
                else:
                    st.caption("Independent version")
                show_revision(get_revision(case_id,row["id"]), "timeline_" + row["id"])
                for outcome in timeline_outcomes:
                    if outcome["child_id"] == row["id"]:
                        st.write("User-rated outcome: " + outcome["rating"])
                snapshot = revision_memory_snapshot(row["id"])
                with st.expander("Memory recorded for this version"):
                    st.json(snapshot or {"applied_presentation": [], "note": "No memory snapshot recorded."})


with account_tab:
    st.subheader("Preserve the account separately from the pictures")
    st.write("Record the account before reviewing generated details where possible. "
             "New account records append to the history; image feedback never overwrites them.")
    account_mode = st.selectbox("Account mode", list(EXAMPLES), key="account_mode_" + case_id)
    with st.form("record_account_" + case_id + account_mode):
        account_text = st.text_area(
            "Account text to preserve",
            value=get_draft(case_id, account_mode)["description"], max_chars=5000, height=180,
            key="account_text_" + case_id + account_mode,
        )
        st.caption("The starting text is the current draft, not a verified original statement. Check it before recording.")
        record_account = st.form_submit_button("Save new account record", disabled=archived)
    if record_account:
        try:
            save_account(case_id, account_mode, account_text)
            invalidate_exports(case_id)
            st.rerun()
        except ValueError as error:
            st.warning(str(error))
        except sqlite3.Error:
            st.error("Could not record the account. Your current draft remains available.")
    accounts = list_accounts(case_id, account_mode)
    if not accounts:
        st.info("Record an account to start its detail-provenance ledger.")
    else:
        account_map = {record["id"]: record for record in accounts}
        account_id = st.selectbox("Recorded account", list(account_map),
                                  format_func=lambda value: account_map[value]["created"] + " / " + value[:8],
                                  key="account_record_" + case_id + account_mode)
        st.text(account_map[account_id]["text"])
        st.caption("These classifications are user observations, not automated verification.")
        with st.form("detail_" + account_id):
            detail = st.text_input("Detail to classify", max_chars=500,
                                   placeholder="For example: glasses shown in the image")
            basis = st.selectbox("Detail source", [
                "Explicit in the account", "Uncertain recollection", "Not supplied in the account"])
            classify = st.form_submit_button("Add detail to ledger", disabled=archived)
        if classify:
            try:
                add_provenance(case_id, account_id, detail, basis)
                invalidate_exports(case_id)
                st.success("Detail classification saved.")
            except ValueError as error:
                st.warning(str(error))
            except sqlite3.Error:
                st.error("Could not save this detail.")
        details = list_provenance(case_id, account_id)
        if details:
            for entry in details:
                st.text(entry["basis"] + ": " + entry["detail"])
        st.caption("Not supplied means unknown. It does not mean a detail was absent from the real event.")

with refine_tab:
    st.subheader("Review, correct and try again")
    st.write("Tell the app what you see and what the account calls for. "
             "Suggestions use your feedback, not automated image analysis.")
    if not revisions:
        st.info("Generate an image first. You can then refine its saved description without retyping it.")
    else:
        base_id = st.selectbox("Image to refine", list(lookup), format_func=revision_label,
                               key="refine_base_" + case_id)
        base = get_revision(case_id, base_id)
        reference_col, feedback_col = st.columns(2, gap="large")
        with reference_col:
            show_revision(base, "refine_" + base_id)
            account_choices = list_accounts(case_id, base["mode"])
            account_lookup = {record["id"]: record for record in account_choices}
            reference_id = st.selectbox(
                "Account reference", [None] + list(account_lookup),
                format_func=lambda value: "No separate account selected" if value is None else
                    account_lookup[value]["created"] + " / " + value[:8],
                key="refine_account_" + base_id,
            )
            if reference_id:
                with st.expander("Account reference and uncertainty ledger", expanded=True):
                    st.text(account_lookup[reference_id]["text"])
                    for detail in list_provenance(case_id, reference_id):
                        st.write(detail["basis"] + ": " + detail["detail"])
            else:
                st.caption("Use Account and uncertainties to preserve an account independently of image revisions.")
        with feedback_col:
            issue = st.selectbox("What went wrong?", ISSUES, key="issue_" + base_id)
            st.info(refinement_guidance(issue))
            st.caption("This is a possible explanation based on the selected issue, not a diagnosis of the model.")
            candidate_key = "candidate_" + base_id
            prepared_key = "prepared_" + base_id
            with st.form("feedback_" + base_id):
                observed = st.text_area("What does this image show incorrectly?", max_chars=1000, height=90)
                expected = st.text_area("What should the next image show instead?", max_chars=1000, height=90)
                change_basis = st.selectbox("Basis for the correction", BASES)
                replace_phrase = st.text_input(
                    "Exact wording to replace (optional)", max_chars=500,
                    help="Copy a full phrase from the saved description. It will be replaced with your correction.")
                keep = st.text_input("Details to request unchanged (optional)", max_chars=300)
                suggest = st.form_submit_button("Save feedback and build revision", disabled=archived)
            if suggest:
                try:
                    candidate = suggest_description(base["description"], expected, replace_phrase, keep)
                    feedback_id = save_feedback(case_id, base_id, reference_id, issue, observed,
                                                expected, change_basis, candidate)
                    st.session_state[candidate_key] = candidate
                    st.session_state[prepared_key] = feedback_id
                    invalidate_exports(case_id)
                    st.success("Revision prepared without using image quota. Check the complete description below.")
                except ValueError as error:
                    st.warning(str(error))
                except sqlite3.Error:
                    st.error("Could not save the feedback. Your saved image remains available.")
            if candidate_key not in st.session_state:
                previous_feedback = [row for row in refinement_records(case_id) if row["revision_id"] == base_id]
                st.session_state[candidate_key] = previous_feedback[-1]["candidate"] if previous_feedback else base["description"]
                if previous_feedback:
                    st.session_state[prepared_key] = previous_feedback[-1]["id"]
            candidate = st.text_area("Revised description to review", height=180,
                                     max_chars=5000, key=candidate_key)
            st.caption(f"{len(candidate.strip())}/900 characters for generation. "
                       "Remove any conflicting wording before loading the revision.")
            if change_basis == "Uncertain recollection":
                st.caption("This correction represents a hypothesis, not a confirmed account detail.")
            ready = bool(st.session_state.get(prepared_key)) and 0 < len(candidate.strip()) <= 900
            if st.button("Use revised description in Create", disabled=archived or not ready,
                         key="apply_refinement_" + base_id, type="primary"):
                revised = dict(base)
                revised["description"] = candidate.strip()
                st.session_state["reuse_revision"] = revised
                st.session_state["flash_" + case_id] = (
                    "Reviewed revision loaded with the source seed. Open Create and select Get Image when ready.")
                st.rerun()
            st.caption("The next request regenerates the whole image. It does not edit pixels or lock the person's identity.")
        with st.expander("Saved feedback for this image"):
            feedback_rows = [row for row in refinement_records(case_id) if row["revision_id"] == base_id]
            if not feedback_rows:
                st.write("No feedback recorded yet.")
            for entry in feedback_rows:
                st.write(entry["issue"] + " / " + entry["created"])
                st.text("Observed: " + entry["observed"] + "\nRequested: " + entry["expected"] +
                        "\nBasis: " + entry["basis"])

                children = [row["id"] for row in revisions if row["parent_id"] == entry["revision_id"]]
                if children:
                    result_id = st.selectbox("Result revision to rate", children, format_func=revision_label,
                                             key="outcome_child_" + entry["id"])
                    current = next((rating for rating in list_outcomes(case_id) if
                                    rating["feedback_id"] == entry["id"] and rating["child_id"] == result_id), None)
                    with st.form("outcome_" + entry["id"] + result_id):
                        ratings = ["Not rated"] + OUTCOME_RATINGS
                        rating = st.selectbox("Did this revision improve the requested detail?", ratings,
                                              index=ratings.index(current["rating"]) if current else 0)
                        allow_reuse = st.checkbox("Allow this issue-category rating to inform other cases",
                                                  value=bool(current["allow_cross_case"]) if current else False)
                        st.caption("Only the issue category and rating are reused, never the account or correction text.")
                        rate = st.form_submit_button("Save revision outcome")
                    if rate:
                        if rating == "Not rated":
                            st.warning("Choose an outcome before saving.")
                        else:
                            try:
                                save_outcome(case_id, entry["id"], result_id, rating, allow_reuse)
                                invalidate_exports(case_id)
                                st.rerun()
                            except (ValueError, sqlite3.Error):
                                st.error("Could not save this outcome.")



with history_tab:
    if not revisions:
        st.info("Generate an illustration to start this case's history.")
    else:
        filter_columns = st.columns(2)
        with filter_columns[0]:
            history_mode = st.selectbox("History mode", ["All modes"] + list(EXAMPLES), key="history_mode_" + case_id)
        with filter_columns[1]:
            history_status = st.selectbox("Review filter", ["All statuses", "Not reviewed", "Needs revision", "Reviewed"],
                                          key="history_status_" + case_id)
        history_search = st.text_input("Find a saved description", key="history_search_" + case_id).strip().casefold()
        history_ids = [row["id"] for row in revisions
                       if (history_mode == "All modes" or row["mode"] == history_mode)
                       and (history_status == "All statuses" or get_review_status(row["id"]) == history_status)
                       and (not history_search or history_search in row["description"].casefold())]
        # Keep the rest of the workspace usable even if a history filter finds no matches.
        if not history_ids:
            st.info("No matching versions. Clear the history filters to see all saved work.")
        if history_ids:
            selected = st.selectbox("Saved version", history_ids, format_func=revision_label,
                                    key="history_" + case_id)
            item = get_revision(case_id, selected)
            image_col, details_col = st.columns(2, gap="large")
            with image_col:
                show_revision(item, "history_" + selected)
            with details_col:
                st.subheader(revision_label(selected))
                if item["parent_id"]:
                    st.caption("Based on saved version " + item["parent_id"][:8])
                with st.expander("Generation prompt"):
                    st.code(item["prompt"], language=None)
                preferred = get_preferred(case_id, item["mode"]) == selected
                if st.button("Remove preferred mark" if preferred else "Mark as preferred for this mode",
                             key="preferred_" + selected):
                    set_preferred(case_id, item["mode"], None if preferred else selected)
                    invalidate_exports(case_id)
                    st.rerun()
                st.caption("Preferred is your selection, not a claim that an image is accurate.")
                with st.form("review_" + selected):
                    notes = st.text_area("Review notes", value=item["notes"], height=200,
                                         help="Record missing, incorrect or unsupported details.")
                    statuses = ["Not reviewed", "Needs revision", "Reviewed"]
                    status = st.selectbox("Review status", statuses,
                                          index=statuses.index(get_review_status(selected)))
                    st.caption("Reviewed means someone checked the illustration. It does not establish factual accuracy.")
                    save_review = st.form_submit_button("Save review notes")
                if save_review:
                    try:
                        persist_review(case_id, selected, notes, status)
                        invalidate_exports(case_id)
                        st.success("Review notes saved.")
                    except sqlite3.Error:
                        st.error("Could not save notes. Copy your notes before leaving this page.")
                if st.button("Use this description and seed for a revision", key="reuse_" + selected):
                    st.session_state["reuse_revision"] = item
                    st.rerun()
                st.caption("After loading a revision, open Create to edit it and generate a new version. "
                       "This regenerates the whole image. Unchanged details may also change.")

with compare_tab:
    if len(revisions) < 2:
        st.info("Save at least two illustrations in this case to compare versions.")
    else:
        compare_mode = st.selectbox("Mode to compare", ["All modes"] + list(EXAMPLES),
                                    key="compare_mode_" + case_id)
        ids = [item["id"] for item in revisions
               if compare_mode == "All modes" or item["mode"] == compare_mode]
        if len(ids) < 2:
            st.info("This mode needs two saved versions.")
        else:
            a, b = st.columns(2, gap="large")
            with a:
                first = st.selectbox("First version", ids, format_func=revision_label,
                                     key="first_" + case_id + compare_mode)
                show_revision(get_revision(case_id, first), "compare_a_" + first)
            with b:
                remaining = [value for value in ids if value != first]
                second = st.selectbox("Second version", remaining, format_func=revision_label,
                                      key="second_" + case_id + compare_mode)
                show_revision(get_revision(case_id, second), "compare_b_" + second)
            first_item = lookup[first]
            second_item = lookup[second]
            with st.expander("What changed in the descriptions?"):
                changes = [line for line in difflib.ndiff(
                    first_item["description"].splitlines(), second_item["description"].splitlines())
                    if not line.startswith("? ")]
                st.code("\n".join(changes), language=None)
                st.caption("Lines beginning with minus belong to the first version. Plus marks the second version.")
                if first_item["seed"] != second_item["seed"]:
                    st.info("These versions use different seeds, so both randomness and description changes can affect the result.")
            st.caption("Compare the described features and positions. Visual similarity does not verify identity.")

with export_tab:
    st.write("Export all saved illustrations, source descriptions, prompts, seeds and review notes as a ZIP.")
    if revisions:
        if st.button("Prepare case export", key="prepare_" + case_id):
            try:
                bundle = export_case(case_id, names[case_id])
                report = export_report(case_id, names[case_id])
            except (OSError, sqlite3.Error):
                st.error("Could not prepare this export. Your saved case is unchanged. Try again.")
            else:
                st.session_state["export_" + case_id] = bundle
                st.session_state["report_" + case_id] = report
        archive = st.session_state.get("export_" + case_id)
        if archive:
            st.download_button("Download case ZIP", archive, file_name="crimesketch_case_" + case_id[:8] + ".zip",
                               mime="application/zip", key="zip_" + case_id)
            report = st.session_state.get("report_" + case_id)
            if report:
                st.download_button("Download printable report", report,
                                   file_name="crimesketch_report_" + case_id[:8] + ".html",
                                   mime="text/html", key="report_download_" + case_id)
            st.caption("The ZIP also contains the report. Open the HTML report in a browser and use Print to save a PDF.")
            st.caption("Prepare again after adding images or changing review notes to refresh the export.")
    else:
        st.info("Save an illustration before exporting.")

st.caption("CrimeSketchAI · Local prototype · AI illustrations require human review")
