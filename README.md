# Azure TTS Streamlit App

A Streamlit app that converts text to speech with Azure AI Speech.

## Features

- Single WAV output or line-by-line ZIP output
- SSML controls (rate, pitch, language override)
- Optional pronunciation mappings
- Local auth with admin user management page
- Per-user usage metrics
- Rate limit and burst guard
- Optional synthesis cache with bounded storage settings
- PostgreSQL persistence via Supabase using DATABASE_URL

## Project Files

- app.py: Main Streamlit page and core logic
- pages/1_Admin.py: Admin-only page (user management and metrics)
- app_config.json: Runtime limits and feature toggles
- requirements.txt: Python dependencies
- DEPLOYMENT_CHECKLIST.md: Streamlit Cloud deployment steps

## Required Secrets

Set in Streamlit Cloud App Settings -> Secrets:

- AZURE_SPEECH_KEY
- AZURE_SPEECH_ENDPOINT (or AZURE_SPEECH_REGION)
- DATABASE_URL (for persistent users/metrics/rate-limit/cache)

Optional first-run admin bootstrap:

- TTS_BOOTSTRAP_ADMIN_USERNAME
- TTS_BOOTSTRAP_ADMIN_PASSWORD

## Local Run

1. Create and activate a virtual environment.
2. Install dependencies from requirements.txt.
3. Add secrets in .streamlit/secrets.toml.
4. Run: streamlit run app.py

## Deployment

- Recommended host: Streamlit Community Cloud
- Main file path: app.py
- Follow DEPLOYMENT_CHECKLIST.md for setup and smoke tests

## Notes

- If DATABASE_URL is set, app state persists in Postgres.
- If DATABASE_URL is not set, app falls back to local SQLite files.
- Keep secrets out of git.
