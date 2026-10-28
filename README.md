# CrimeSketchAI
Streamlit hackathon prototype for fictional scene illustrations and face sketches, iterative review and written briefs. Opens in guest mode; login is optional.

## Run
Use Python 3.12. Install requirements.txt and run `streamlit run app.py`.

## Deploy
1. Upload this folder's CONTENTS to the GitHub repository root. Include the hidden .streamlit folder. Keep any organizer-required files already there.
2. Open https://share.streamlit.io and sign in with GitHub. Choose Create app / deploy from GitHub.
3. Select Hackfest-X-Saividya/techbros-t1-41, its actual branch, and app.py. Choose Python 3.12 in Advanced settings.
4. In Advanced settings > Secrets, enter HF_TOKEN, OWNER_USERNAME and OWNER_PASSWORD using secrets.example.toml as a format guide. Use a private owner password of at least 15 characters. Do not upload real values to GitHub.
5. Deploy. If the repository is missing, its organization may need to approve Streamlit access; ask the organizers.
6. Check guest Get Brief, one fictional image request, and optional owner login. Download important work.

## Demo limits
Cloud local SQLite/files are not durable storage. Accounts, cases and guest files can be lost on redeploy/restart. This build does not add a hosted database. Owner secrets initialize the owner only when no accounts exist; changing the secret does not change an existing account password.
Public visitors cannot claim the first owner through the UI. Guest session directories are separate. This remains prototype authentication, not an audited public case-management service. Use fictional descriptions only.
All users share the hosted HF token's provider quota. Deployment does not reset quota. No automatic paid fallback. The app can still create briefs while image quota is unavailable.

Never commit .env, .streamlit/secrets.toml, local databases, user data, or credentials. No credentials or case data are included in this package.
