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
DATABASE_URL = "postgresql://tts_app:<password>@<pooler-host>:6543/postgres?sslmode=require"

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

- With `DATABASE_URL` set, auth/metrics/rate-limit/cache persist in Postgres.
- Without `DATABASE_URL`, the app falls back to local SQLite.

## Optional: Manual Cache Cleanup SQL (Supabase)

Use this in Supabase SQL Editor if you need to reclaim storage immediately:

```sql
-- Remove cache entries older than 24 hours
DELETE FROM public.synthesis_cache
WHERE last_access_ts < EXTRACT(EPOCH FROM NOW()) - 86400;

-- Keep only the 120 most recently accessed entries
DELETE FROM public.synthesis_cache
WHERE cache_key IN (
	SELECT cache_key
	FROM public.synthesis_cache
	ORDER BY last_access_ts DESC
	OFFSET 120
);
```
