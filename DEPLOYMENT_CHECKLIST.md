# Streamlit Cloud Deployment Checklist

## 1) Security Before Push
- Keep secrets out of git.
- Ensure `app_config.json` has empty bootstrap values.
- Set credentials only in Streamlit Cloud app secrets.
- If any secret was previously exposed, rotate it before deploy.

## 2) Required Streamlit Cloud Secrets
Add these in app settings -> Secrets:

```toml
AZURE_SPEECH_KEY = "<your-azure-speech-key>"
AZURE_SPEECH_ENDPOINT = "https://<your-region>.cognitiveservices.azure.com"

# Optional first-run admin bootstrap
TTS_BOOTSTRAP_ADMIN_USERNAME = "admin"
TTS_BOOTSTRAP_ADMIN_PASSWORD = "<strong-random-password>"
```

## 3) Git Setup
```bash
git init
git add .
git status
```
Verify `.streamlit/secrets.toml` is NOT staged.

## 4) Deploy
- Push to a private GitHub repository.
- In Streamlit Community Cloud, create a new app.
- Repo: your private repo
- Branch: `main`
- Main file path: `app.py`

## 5) Smoke Tests
- Login gate appears when auth is enabled.
- Admin can open Admin page and manage users.
- Single WAV synthesis works.
- Line ZIP synthesis works.
- Cache hit/miss captions update.
- Quota and burst guard messages work.

## Notes
- Current auth/metrics/rate-limit storage uses local SQLite.
- On cloud restart/redeploy, local state may reset.
