"""Image-conditioned fal route. No network is performed while preparing inputs."""
import base64
from hashlib import sha256
from fal_backend import run_fal, ProviderError

REFERENCE_MODEL="fal-ai/flux-pro/kontext/multi"
REFERENCE_PROVIDER="fal reference · FLUX Kontext multi"

def prepare_references(prompt, references):
    if not references or len(references)>6:
        raise ProviderError("Upload between one and six valid feature references.")
    urls=[];plan=[];instructions=[]
    for index,ref in enumerate(references,1):
        raw=ref.get("image_bytes",b"")
        if not isinstance(raw,bytes) or not raw or len(raw)>10_000_000:
            raise ProviderError("A prepared reference image is invalid or too large.")
        instruction=str(ref.get("instruction","")).strip()
        if not instruction or len(instruction)>500:
            raise ProviderError("Explain what to use or change for every uploaded reference (up to 500 characters each).")
        feature=ref["feature"]
        digest=sha256(raw).hexdigest()
        urls.append("data:image/png;base64,"+base64.b64encode(raw).decode("ascii"))
        plan.append({"feature":feature,"instruction":instruction,"crop_percent":ref.get("crop_percent"),"sha256":digest})
        instructions.append(f"Reference image {index}: use only its {feature.lower()} as guidance. Requested use/differences: {instruction}. Reference fingerprint: {digest}.")
    combined=prompt+"\nCreate one coherent fictional composite portrait. Do not copy unrelated traits from references. Follow the written differences.\n"+"\n".join(instructions)
    return combined,urls,plan

def reference_payload(prompt,seed,urls):
    return {"prompt":prompt,"seed":int(seed),"image_urls":urls,"num_images":1,"output_format":"png","aspect_ratio":"1:1","safety_tolerance":"2","enhance_prompt":False}

def run_reference(prompt,seed,key,data_dir,case_id,*,references=None,authorized=False,context=None,**kwargs):
    if not authorized or not key or not key.strip():
        raise ProviderError("A fal key and explicit paid-reference authorization are required.")
    payload=None
    if references is not None:
        # prompt is already prepared by the caller; keep bytes out of saved job context.
        _, urls, _ = prepare_references("", references)
        if not urls:raise ProviderError("Upload at least one reference.")
        payload=reference_payload(prompt,seed,urls)
    else:
        from fal_backend import job_path
        path=job_path(data_dir,case_id,prompt,seed,REFERENCE_MODEL)
        if not path.is_file():
            raise ProviderError("No saved reference request exists to resume. Upload references before generating.")
        import json
        job=json.loads(path.read_text(encoding="utf-8"))
        if job.get("state") not in ("pending","completed"):
            raise ProviderError("This reference request cannot be resumed. Check fal request history before submitting again.")
    return run_fal(prompt,seed,key,data_dir,case_id,context=context,model=REFERENCE_MODEL,payload=payload,**kwargs)
