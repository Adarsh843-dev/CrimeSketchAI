"""Optional feature references. Crops are returned for the explicitly selected paid route."""
from io import BytesIO
import hashlib
import json
from PIL import Image, ImageOps, UnidentifiedImageError

FEATURES = ("Eyes", "Eyebrows", "Nose", "Mouth", "Hair", "Face shape")
MAX_BYTES = 5 * 1024 * 1024
MAX_PIXELS = 16_000_000

def load_reference(raw):
    if len(raw) > MAX_BYTES:
        raise ValueError("Choose an image smaller than 5 MB.")
    try:
        with Image.open(BytesIO(raw)) as source:
            if source.width * source.height > MAX_PIXELS:
                raise ValueError("Choose an image with no more than 16 million pixels.")
            if source.format not in ("PNG", "JPEG", "WEBP"):
                raise ValueError("Use a PNG, JPEG or WebP image.")
            image = ImageOps.exif_transpose(source).convert("RGB")
            image.thumbnail((1600, 1600))
            return image.copy()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise ValueError("This image could not be opened. Use a valid PNG, JPEG or WebP.") from None

def crop_reference(image, horizontal, vertical):
    left, right = horizontal
    top, bottom = vertical
    if not (0 <= left < right <= 100 and 0 <= top < bottom <= 100):
        raise ValueError("The crop needs a non-zero width and height.")
    x1, y1 = int(image.width * left / 100), int(image.height * top / 100)
    x2, y2 = max(x1+1, int(image.width * right / 100)), max(y1+1, int(image.height * bottom / 100))
    return image.crop((x1, y1, min(image.width,x2), min(image.height,y2)))

def render_reference_board(scope):
    import streamlit as st
    epoch_key = "reference_epoch_" + scope
    epoch = st.session_state.get(epoch_key, 0)
    prefix = "reference_" + scope + "_" + str(epoch)
    entries = []
    with st.expander("Add reference images · optional", expanded=False):
        st.info("Get Image uses text only. Generate with references is a separate paid fal request: it sends your selected crops and instructions to FLUX Kontext (experimental multi-image). No upload is sent to fal until you authorize generation.")
        st.caption("Use fictional or permission-cleared images. Uploads stay in this app session and are not added to case history or reports. On a deployed app, uploads are processed by its server.")
        if st.button("Clear all references", key=prefix+"_clear"):
            st.session_state[epoch_key] = epoch + 1
            # Remove previous uploader/crop/note state for this case only.
            for key in list(st.session_state):
                if key.startswith("reference_"+scope+"_") and key != epoch_key:
                    del st.session_state[key]
            st.rerun()
        feature = st.selectbox("Reference feature", FEATURES, key=prefix+"_feature")
        st.caption("Choose a feature to upload or edit. You can prepare one reference for each feature.")
        # Render all uploaders so Streamlit retains their values when changing features.
        for current in FEATURES:
            with st.expander(current, expanded=current==feature):
                uploaded = st.file_uploader("Upload " + current.lower() + " reference", type=["png","jpg","jpeg","webp"], key=prefix+"_upload_"+current)
                if uploaded is None:
                    continue
                try:
                    raw = uploaded.getvalue()
                    image = load_reference(raw)
                    item_key = prefix+"_"+current+"_"+hashlib.sha256(raw).hexdigest()[:16]
                    st.image(image, caption="Uploaded reference · " + current, width="stretch")
                    horizontal = st.slider("Left / right crop (%)",0,100,(0,100),key=item_key+"_horizontal")
                    vertical = st.slider("Top / bottom crop (%)",0,100,(0,100),key=item_key+"_vertical")
                    crop = crop_reference(image,horizontal,vertical)
                    st.image(crop,caption="Selected feature crop",width="stretch")
                    instruction = st.text_area("What should this reference guide?",placeholder="Shape only; ignore colour and unrelated features.",max_chars=500,key=item_key+"_instruction")
                    entries.append({"feature":current,"instruction":instruction,"crop_percent":{"horizontal":horizontal,"vertical":vertical},"status":"prepared_reference","image_bytes":encode_crop(crop)})
                except ValueError as error:
                    st.warning(str(error))
        if entries:
            st.markdown("**Reference plan · used only by paid reference generation**")
            for item in entries:
                st.write(item["feature"] + ": " + (item["instruction"].strip() or "Instruction not entered"))
            st.download_button("Download reference plan (JSON)",json.dumps({"references":[{k:v for k,v in e.items() if k != "image_bytes"} for e in entries]},indent=2),file_name="reference_plan.json",mime="application/json",key=prefix+"_download")
            st.caption("The plan contains instructions and crop coordinates, not uploaded images. It is separate from the generated result.")
    return entries


def encode_crop(image):
    buffer=BytesIO()
    image.save(buffer,format="PNG")
    return buffer.getvalue()
