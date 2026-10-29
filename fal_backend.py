"""Optional fal.ai backend. Does not submit without an explicitly supplied API key.
No automatic fallback, purchase, or automatic retry of a generation submission.
"""
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, HTTPRedirectHandler, build_opener

MODEL = "fal-ai/flux/schnell"
SUBMIT_URL = "https://queue.fal.run/" + MODEL

class ProviderError(RuntimeError):
    pass

class RejectedSubmission(ProviderError):
    pass

class PendingJob(ProviderError):
    pass

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProviderError("Provider returned an unexpected redirect.")

def validate_queue_url(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "queue.fal.run" or parsed.port not in (None, 443) or parsed.username:
        raise ProviderError("Provider returned an unexpected queue URL.")
    return url

def request_json(url, key, payload=None):
    validate_queue_url(url)
    data = json.dumps(payload).encode() if payload is not None else None
    request = Request(url, data=data, headers={
        "Authorization": "Key " + key,
        "Content-Type": "application/json",
    }, method="POST" if payload is not None else "GET")
    try:
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            return json.loads(response.read(2_000_000))
    except HTTPError as error:
        if error.code in (401, 403):
            raise RejectedSubmission("fal authentication failed. Check your fal API key and account access.") from None
        if error.code == 402:
            raise RejectedSubmission("fal account has insufficient funds. Check billing before retrying.") from None
        if error.code == 429:
            retry = error.headers.get("Retry-After", "") if error.headers else ""
            estimate = (" Try again in " + retry + " seconds.") if retry.isdigit() and int(retry)>0 else ""
            raise RejectedSubmission("fal rate limit reached. Wait and resume the saved request if one exists." + estimate) from None
        raise ProviderError("fal returned HTTP " + str(error.code) + ". Check the provider dashboard.") from None
    except (URLError, OSError, ValueError):
        raise ProviderError("Could not read a response from fal. Check your connection.") from None

def download_image(url):
    parsed = urlparse(url)
    host = parsed.hostname or ""
    allowed = any(host == domain or host.endswith("." + domain) for domain in ("fal.media", "fal.ai", "fal.run"))
    if parsed.scheme != "https" or not allowed or parsed.username or parsed.port not in (None, 443):
        raise ProviderError("Provider returned an unexpected image URL.")
    try:
        # No API key is sent to the image CDN.
        with build_opener(NoRedirect()).open(Request(url), timeout=30) as response:
            data = response.read(20_000_001)
        if not data or len(data) > 20_000_000:
            raise ProviderError("Provider image was empty or exceeded the download limit.")
        return data
    except (HTTPError, URLError, OSError):
        raise ProviderError("The image download failed. Resume the request to retry only the download.") from None

def write_job(path, job):
    path = Path(path)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)
    finally:
        if temp.exists():
            temp.unlink()

def job_path(data_dir, case_id, prompt, seed, model=MODEL):
    digest = sha256(json.dumps([case_id, model, prompt, int(seed)], ensure_ascii=False).encode()).hexdigest()
    folder = Path(data_dir) / "generation_jobs"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / (digest + ".json")

def list_pending(data_dir, case_id):
    folder = Path(data_dir) / "generation_jobs"
    result = []
    for path in folder.glob("*.json"):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
            if job.get("case_id") == case_id and job.get("state") in ("pending", "unknown", "submitting"):
                result.append(job)
        except (OSError, ValueError):
            continue
    return result

def run_fal(prompt, seed, key, data_dir, case_id, *, context=None, max_wait=240,
            transport=None, downloader=None, clock=None, sleep=None, model=MODEL, payload=None):
    if not key or not key.strip():
        raise ProviderError("A fal API key and funded account are required.")
    transport = transport or request_json
    downloader = downloader or download_image
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    if model not in (MODEL, "fal-ai/flux-pro/kontext/multi"):
        raise ProviderError("Unsupported generation model.")
    path = job_path(data_dir, case_id, prompt, seed, model)
    job = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    if job and job.get("state") in ("submitting", "unknown"):
        raise PendingJob("The previous submission has an uncertain outcome. Check your fal request history before starting another request.")
    if job and job.get("state") == "rejected":
        job = None
    if not job:
        job = {"case_id": case_id, "prompt": prompt, "seed": int(seed),  "model": model, "state": "submitting", "context": context or {}}
        # Write before sending. A lost submit response must never trigger a blind duplicate charge.
        write_job(path, job)
        try:
            response = transport("https://queue.fal.run/" + model, key, payload or {
                "prompt": prompt, "seed": int(seed),
                "image_size": {"width": 768, "height": 768},
                "num_inference_steps": 4, "num_images": 1,
                "output_format": "png", "enable_safety_checker": True,
            })
            job.update(request_id=response["request_id"],
                       status_url=validate_queue_url(response["status_url"]),
                       response_url=validate_queue_url(response["response_url"]),
                       state="pending")
            write_job(path, job)
        except RejectedSubmission:
            job["state"] = "rejected"
            write_job(path, job)
            raise
        except Exception:
            job["state"] = "unknown"
            write_job(path, job)
            raise PendingJob("Submission was not confirmed. It might have reached fal. Check your provider request history before retrying.") from None
    if job.get("state") == "failed":
        raise ProviderError("The provider reported that this saved request failed. Check its request history before generating again.")
    deadline = clock() + max_wait
    while True:
        status = transport(job["status_url"], key)
        if status.get("status") == "COMPLETED":
            if status.get("error"):
                job["state"] = "failed"
                write_job(path, job)
                raise ProviderError("fal reported a failed generation. Check the request in your provider dashboard.")
            break
        if clock() >= deadline:
            raise PendingJob("The request is still pending. Use Resume provider request; do not create another generation.")
        sleep(2)
    result = transport(job["response_url"], key)
    if any(result.get("has_nsfw_concepts", [])):
        job["state"] = "failed"
        write_job(path, job)
        raise ProviderError("The provider safety checker declined this output. Review the description.")
    images = result.get("images") or []
    if not images or not images[0].get("url"):
        raise ProviderError("The provider did not return an image. Check its request history.")
    raw = downloader(images[0]["url"])
    job["state"] = "completed"
    write_job(path, job)
    return raw, int(result.get("seed", seed)), job["request_id"]
