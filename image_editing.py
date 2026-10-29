"""Edits of an existing, saved application-generated image. No network on preview."""
import base64
from hashlib import sha256
from io import BytesIO
import json
from PIL import Image, ImageDraw, ImageEnhance, ImageOps
from fal_backend import run_fal, job_path, ProviderError

EDIT_MODEL="fal-ai/flux-pro/kontext"
EDIT_PROVIDER="fal image edit · FLUX Kontext"
NOTICE="AI-GENERATED ILLUSTRATION - NOT VERIFIED EVIDENCE"

def source_picture(raw):
    with Image.open(BytesIO(raw)) as original:
        picture=original.convert("RGB")
    if picture.height <= 48:
        raise ValueError("The saved image is too small to edit.")
    # Every app output has a 48px disclosure footer; never send it as image content.
    return picture.crop((0,0,picture.width,picture.height-48))

def png_bytes(picture):
    buffer=BytesIO();picture.save(buffer,format="PNG")
    return buffer.getvalue()

def label_picture(picture):
    labelled=Image.new("RGB",(picture.width,picture.height+48),"white")
    labelled.paste(picture,(0,0))
    ImageDraw.Draw(labelled).text((12,picture.height+17),NOTICE,fill="black")
    return png_bytes(labelled)

def region_box(size,region):
    width,height=size
    left,right=region["horizontal"];top,bottom=region["vertical"]
    if not (0<=left<right<=100 and 0<=top<bottom<=100):
        raise ValueError("Select an area with non-zero width and height.")
    x1,y1=int(width*left/100),int(height*top/100)
    return (x1,y1,min(width,max(x1+1,int(width*right/100))),min(height,max(y1+1,int(height*bottom/100))))

def local_edit(base,brightness,contrast,sharpness):
    values=(float(brightness),float(contrast),float(sharpness))
    if not all(0.5<=v<=2 for v in values):
        raise ValueError("Adjustment values must be between 0.5 and 2.")
    if values==(1.0,1.0,1.0):
        raise ValueError("Change at least one adjustment before saving.")
    picture=source_picture(base["image"])
    for cls,value in zip((ImageEnhance.Brightness,ImageEnhance.Contrast,ImageEnhance.Sharpness),values):
        picture=cls(picture).enhance(value)
    settings=dict(zip(("brightness","contrast","sharpness"),values))
    prompt="Local image adjustments: "+json.dumps(settings,sort_keys=True)+"; source version "+base["id"]
    return {"image":label_picture(picture),"prompt":prompt,"seed":base["seed"],"provider":"Local image adjustments","request_id":None,
        "memory_snapshot":{"image_edit":{"source_revision":base["id"],"source_sha256":sha256(base["image"]).hexdigest(),"kind":"local",**settings}}}

def prepare_edit(base,instruction,region=None):
    instruction=instruction.strip()
    if not instruction or len(instruction)>900:
        raise ValueError("Describe the image change in 1–900 characters.")
    picture=source_picture(base["image"])
    if region is not None:region_box(picture.size,region)
    metadata={"source_revision":base["id"],"source_sha256":sha256(base["image"]).hexdigest(),"instruction":instruction,"region":region,"kind":"ai","model":EDIT_MODEL}
    prompt="Edit the supplied fictional illustration. Requested change: "+instruction+". Preserve the composition and all unrelated details. Keep the black-and-white sketch style; do not add labels or text."
    if region:
        prompt+=" Target region in percent from top-left: horizontal "+str(region["horizontal"])+", vertical "+str(region["vertical"])+"."
    prompt+=" Source fingerprint: "+metadata["source_sha256"]
    return prompt,picture,metadata

def finish_edit(raw,base,metadata,seed,request_id,prompt):
    original=source_picture(base["image"])
    with Image.open(BytesIO(raw)) as result:
        edited=ImageOps.grayscale(result).convert("RGB").resize(original.size,Image.Resampling.LANCZOS)
    region=metadata.get("region")
    if region:
        box=region_box(original.size,region)
        # Composite only the selected rectangle. Outside pixels are copied exactly.
        original.paste(edited.crop(box),box)
        edited=original
    return {"image":label_picture(edited),"prompt":prompt,"seed":seed,"provider":EDIT_PROVIDER,"request_id":request_id,
        "memory_snapshot":{"image_edit":metadata}}

def run_image_edit(base,instruction,seed,key,data_dir,case_id,*,authorized=False,region=None,**kwargs):
    if not authorized or not key.strip():
        raise ProviderError("Enter a fal key and authorize the paid image edit first.")
    prompt,picture,metadata=prepare_edit(base,instruction,region)
    payload={"prompt":prompt,"image_url":"data:image/png;base64,"+base64.b64encode(png_bytes(picture)).decode("ascii"),
        "seed":int(seed),"num_images":1,"output_format":"png","safety_tolerance":"2","enhance_prompt":False}
    context={"description":base["description"],"mode":base["mode"],"parent_id":base["id"],"image_edit":metadata}
    raw,actual_seed,request_id=run_fal(prompt,seed,key,data_dir,case_id,model=EDIT_MODEL,payload=payload,context=context,**kwargs)
    return finish_edit(raw,base,metadata,actual_seed,request_id,prompt)

def resume_image_edit(job,base,key,data_dir,case_id,*,authorized=False,**kwargs):
    if not authorized or not key.strip():raise ProviderError("Authorize retrieval using your fal key.")
    path=job_path(data_dir,case_id,job["prompt"],job["seed"],EDIT_MODEL)
    if not path.is_file():raise ProviderError("Saved edit request was not found.")
    saved=json.loads(path.read_text(encoding="utf-8"))
    metadata=saved.get("context",{}).get("image_edit",{})
    if saved.get("case_id")!=case_id or saved.get("model")!=EDIT_MODEL or saved.get("state") not in ("pending","completed"):
        raise ProviderError("This edit cannot be resumed. Check the provider request history.")
    if metadata.get("source_revision")!=base["id"] or metadata.get("source_sha256")!=sha256(base["image"]).hexdigest():
        raise ProviderError("The saved source image does not match this edit request.")
    raw,seed,request_id=run_fal(saved["prompt"],saved["seed"],key,data_dir,case_id,model=EDIT_MODEL,**kwargs)
    return finish_edit(raw,base,metadata,seed,request_id,saved["prompt"])

def render_image_editor(base,*,data_dir,case_id,owner_key,on_save):
    import streamlit as st
    key="image_editor_"+case_id+"_"+base["id"]
    with st.expander("Edit this generated image",expanded=False):
        st.caption("Source: saved version "+base["id"][:8]+" · "+base.get("provider","generated image")+". Every saved edit creates a new version; the source remains available in Saved versions.")
        st.subheader("Quick adjustments · free")
        brightness=st.slider("Brightness",0.5,2.0,1.0,0.05,key=key+"_brightness")
        contrast=st.slider("Contrast",0.5,2.0,1.0,0.05,key=key+"_contrast")
        sharpness=st.slider("Sharpness",0.5,2.0,1.0,0.05,key=key+"_sharpness")
        changed=(brightness,contrast,sharpness)!=(1.0,1.0,1.0)
        if changed:
            st.image(local_edit(base,brightness,contrast,sharpness)["image"],caption="Local adjustment preview · not saved",width="stretch")
        if st.button("Save adjusted version",disabled=not changed,key=key+"_save_local"):
            try:
                on_save(base,local_edit(base,brightness,contrast,sharpness))
            except (ValueError,OSError) as error:st.error("Could not save the adjustment. Your source is unchanged.")
        st.subheader("Change image details · paid AI edit")
        instruction=st.text_area("What should change in this image?",placeholder="Make the eyebrows slightly thinner. Keep the eyes, nose, mouth and hairstyle unchanged.",max_chars=900,key=key+"_instruction")
        selected=st.checkbox("Keep pixels outside a selected area unchanged",value=True,key=key+"_lock")
        region=None
        valid_region=True
        if selected:
            horizontal=st.slider("Edit area: left / right (%)",0,100,(20,80),key=key+"_horizontal")
            vertical=st.slider("Edit area: top / bottom (%)",0,100,(20,70),key=key+"_vertical")
            region={"horizontal":horizontal,"vertical":vertical}
            try:
                preview=source_picture(base["image"])
                ImageDraw.Draw(preview).rectangle(region_box(preview.size,region),outline="#00A6C8",width=3)
                st.image(preview,caption="Only this rectangle will be replaced in the saved result",width="stretch")
            except ValueError as error:
                valid_region=False;st.warning(str(error))
            st.caption("The model receives the full generated image for context. We copy only the edited rectangle back into the original; boundaries may need review. Exact feature changes are not guaranteed.")
        else:
            st.caption("Whole-image edits can change other details. Compare the new version with the source.")
        api_key=st.text_input("fal key for image edits (private)",type="password",key=key+"_key") or owner_key
        authorization=st.checkbox("I authorize a paid image edit and sending this generated image to fal",value=False,key=key+"_authorize")
        st.link_button("View image-edit model pricing","https://fal.ai/models/fal-ai/flux-pro/kontext",key=key+"_pricing")
        seed=st.number_input("Image edit seed",0,2147483647,int(base["seed"]),key=key+"_seed")
        if st.button("Apply AI edit · paid",disabled=not instruction.strip() or not api_key or not authorization or not valid_region,key=key+"_apply"):
            try:
                with st.spinner("Editing this saved image with fal...",show_time=True):
                    result=run_image_edit(base,instruction,seed,api_key,data_dir,case_id,authorized=authorization,region=region)
                on_save(base,result)
            except (ProviderError,ValueError,OSError):
                st.error("The edit could not finish or save. Your source is unchanged. Check Recover provider requests for pending/uncertain submissions before trying again. A returned result remains available through the saved request or unsaved-result recovery.")
