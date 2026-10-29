# Editing generated images and renaming cases

## Rename a case
Select the case, click **Rename case** beside its title, enter a name, then **Save new case name**. Blank names are rejected. **Cancel rename** makes no change. The case ID, saved revisions and notes are retained. Renaming clears the case search so a renamed case does not disappear behind its previous name filter. The older Settings / Case settings control remains available.

## Edit a generated result
After generating an image with either text-only FLUX or fal, open **Edit this generated image** below the displayed result. No editor or paid edit action is shown without a saved source image. Save an unsaved result before editing it. Archived cases must be restored first.

Use **Image to view or edit** to select an existing version from the current case. The editor shows its source version ID. Changes create a new child revision, preserving the original description and source image. **View previous version** returns to the parent without deleting anything; all branches remain in Saved versions and the revision timeline.

### Free local adjustments
Brightness, contrast and sharpness run locally and preview on the actual source image. **Save adjusted version** creates a revision without calling an AI provider.

### Paid detail changes
Enter a specific instruction (for example, make the eyebrows thinner while retaining the eye shape). Provide a fal key privately and explicitly authorize sending the generated image to fal. The **Apply AI edit** action uses `fal-ai/flux-pro/kontext` with the saved image as `image_url`; it does not generate a new face solely from the original text. Guests supply their own key. Signed-in admins may use configured FAL_KEY. Model charges and accuracy limitations still apply. Uploading references is not necessary for editing an existing result.

The default selected-area option composites only the model's edited rectangle into the original image. Outside image-content pixels are copied exactly. The 48px disclosure footer is excluded from model input and redrawn once on output. Selected-area compositing is an application operation, not masked inpainting: the model receives the full source image and an instruction describing the region. Boundaries can show seams and intended feature changes are not guaranteed. Disable the region lock for a whole-image edit, which can alter unrelated details.

## Billing and recovery
No paid call occurs on preview, slider changes, rename, or local saves. One explicit paid action submits one queued request. Exact source/instruction/region/seed inputs reuse the saved job. Use Recover provider requests to retrieve a pending image edit without a new submission. Unknown submissions are blocked from automatic retry; inspect fal history before trying another input. Key material and input image data URIs are not stored in job JSON. Edit instructions, source revision ID/hash, model, region and request ID are recorded in the new revision's metadata and can be exported.

## Validation scope
Unit and Streamlit interface tests use mocked AI providers. They verify actual source-image payloads, authorization, parent links, pixel preservation outside the selected region, no paid calls for local adjustments, pending-request recovery, renaming and invalid-name handling. No real paid image edit or credit purchase was performed; live quality, endpoint acceptance and billing remain untested.
