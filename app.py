import io
import json
import os
import re
import sqlite3
import threading
import time
import uuid
import zipfile
from hashlib import sha256
from html import escape
from typing import Any

import azure.cognitiveservices.speech as speechsdk
import streamlit as st
import streamlit.components.v1 as components
from passlib.context import CryptContext

try:
    import psycopg
except ImportError:
    psycopg = None

VOICE_OPTIONS = [
    {
        "label": "Cantonese (HK) - Male - WanLung",
        "value": "zh-HK-WanLungNeural",
    },
    {
        "label": "Cantonese (HK) - Female - HiuGaai",
        "value": "zh-HK-HiuGaaiNeural",
    },
    {
        "label": "Cantonese (HK) - Female - HiuMaan",
        "value": "zh-HK-HiuMaanNeural",
    },
]

LANGUAGE_OVERRIDE_OPTIONS = [
    {"label": "Auto (no override)", "value": "auto"},
    {"label": "Cantonese (Hong Kong)", "value": "zh-HK"},
    {"label": "Mandarin (Mainland China)", "value": "zh-CN"},
    {"label": "English (United States)", "value": "en-US"},
    {"label": "Japanese", "value": "ja-JP"},
    {"label": "French", "value": "fr-FR"},
    {"label": "Spanish", "value": "es-ES"},
    {"label": "German", "value": "de-DE"},
]

OUTPUT_MODE_OPTIONS = [
    {"label": "Single audio file", "value": "single"},
    {
        "label": "Line by line (ZIP of numbered WAV files)",
        "value": "line_zip",
    },
]

DEFAULT_APP_CONFIG = {
    "max_single_input_chars": 5000,
    "max_batch_input_chars": 10000,
    "max_batch_lines": 120,
    "max_hourly_synthesis_units": 120,
    "rate_limit_window_seconds": 3600,
    "min_seconds_between_requests": 2,
    "rate_limit_db_path": ".rate_limit.sqlite",
    "max_pronunciation_mappings": 40,
    "enable_synthesis_cache": True,
    "synthesis_cache_ttl_seconds": 604800,
    "synthesis_cache_max_entries": 500,
    "synthesis_cache_max_total_bytes": 209715200,
    "enable_auth": True,
    "auth_db_path": "",
    "bootstrap_admin_username": "",
    "bootstrap_admin_password": "",
}


def load_app_config() -> dict[str, Any]:
    config = DEFAULT_APP_CONFIG.copy()
    config_path = os.getenv("APP_CONFIG_PATH", "app_config.json")

    if not os.path.exists(config_path):
        return config

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return config

    if not isinstance(data, dict):
        return config

    for key, value in data.items():
        if key not in config:
            continue
        config[key] = value

    return config


APP_CONFIG = load_app_config()


def int_config(name: str, minimum: int) -> int:
    raw_value = APP_CONFIG.get(name, DEFAULT_APP_CONFIG[name])
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        value = int(DEFAULT_APP_CONFIG[name])
    return max(minimum, value)


def bool_config(name: str, default: bool) -> bool:
    raw_value = APP_CONFIG.get(name, default)
    if isinstance(raw_value, bool):
        return raw_value
    if isinstance(raw_value, (int, float)):
        return raw_value != 0
    if isinstance(raw_value, str):
        return raw_value.strip().lower() in {"1", "true", "yes", "on"}
    return default


# Per-request safeguards.
MAX_SINGLE_INPUT_CHARS = int_config("max_single_input_chars", 1)
MAX_BATCH_INPUT_CHARS = int_config("max_batch_input_chars", MAX_SINGLE_INPUT_CHARS)
MAX_BATCH_LINES = int_config("max_batch_lines", 1)

# Hourly quota per client (IP when available, session fallback otherwise).
MAX_HOURLY_SYNTHESIS_UNITS = int_config("max_hourly_synthesis_units", 1)
RATE_LIMIT_WINDOW_SECONDS = int_config("rate_limit_window_seconds", 60)
MIN_SECONDS_BETWEEN_REQUESTS = int_config("min_seconds_between_requests", 0)
RATE_LIMIT_DB_PATH = str(APP_CONFIG.get("rate_limit_db_path", ".rate_limit.sqlite"))
RATE_LIMIT_LOCK = threading.Lock()
MAX_PRONUNCIATION_MAPPINGS = int_config("max_pronunciation_mappings", 1)

ENABLE_SYNTHESIS_CACHE = bool_config("enable_synthesis_cache", True)
SYNTHESIS_CACHE_TTL_SECONDS = int_config("synthesis_cache_ttl_seconds", 1)
SYNTHESIS_CACHE_MAX_ENTRIES = int_config("synthesis_cache_max_entries", 1)
SYNTHESIS_CACHE_MAX_TOTAL_BYTES = int_config("synthesis_cache_max_total_bytes", 1024)

ENABLE_AUTH = bool_config("enable_auth", True)
AUTH_DB_PATH = str(APP_CONFIG.get("auth_db_path", "")).strip() or RATE_LIMIT_DB_PATH
BOOTSTRAP_ADMIN_USERNAME = str(APP_CONFIG.get("bootstrap_admin_username", "")).strip()
BOOTSTRAP_ADMIN_PASSWORD = str(APP_CONFIG.get("bootstrap_admin_password", "")).strip()

# Use bcrypt_sha256 to avoid bcrypt's 72-byte input limit while still verifying
# existing bcrypt hashes created in earlier app versions.
PWD_CONTEXT = CryptContext(schemes=["bcrypt_sha256", "bcrypt"], deprecated="auto")


def get_config_value(name: str) -> str:
    secret_value = st.secrets.get(name, "")
    if secret_value:
        return str(secret_value).strip()
    return os.getenv(name, "").strip()


DATABASE_URL = get_config_value("DATABASE_URL")
USE_POSTGRES = DATABASE_URL.lower().startswith(("postgres://", "postgresql://"))


class DbConnection:
    def __init__(self, raw_connection: Any, use_postgres: bool):
        self._raw = raw_connection
        self._use_postgres = use_postgres

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> Any:
        if self._use_postgres:
            return self._raw.execute(query.replace("?", "%s"), params)
        return self._raw.execute(query, params)

    def commit(self) -> None:
        self._raw.commit()

    def close(self) -> None:
        self._raw.close()


def db_connect(local_db_path: str) -> DbConnection:
    if USE_POSTGRES:
        if psycopg is None:
            raise RuntimeError(
                "DATABASE_URL is set but psycopg is not installed. "
                "Add psycopg[binary] to requirements.txt."
            )
        return DbConnection(psycopg.connect(DATABASE_URL), use_postgres=True)

    return DbConnection(sqlite3.connect(local_db_path), use_postgres=False)


def db_bool(value: bool) -> bool | int:
    if USE_POSTGRES:
        return value
    return 1 if value else 0


def build_speech_config() -> tuple[speechsdk.SpeechConfig | None, str | None]:
    speech_key = get_config_value("AZURE_SPEECH_KEY")
    speech_region = get_config_value("AZURE_SPEECH_REGION")
    speech_endpoint = get_config_value("AZURE_SPEECH_ENDPOINT")

    if not speech_key:
        return None, "Missing AZURE_SPEECH_KEY. Set it in environment variables or Streamlit secrets."

    if not speech_region and not speech_endpoint:
        return None, "Set either AZURE_SPEECH_REGION or AZURE_SPEECH_ENDPOINT."

    try:
        if speech_endpoint:
            speech_config = speechsdk.SpeechConfig(
                subscription=speech_key,
                endpoint=speech_endpoint,
            )
        else:
            speech_config = speechsdk.SpeechConfig(
                subscription=speech_key,
                region=speech_region,
            )
    except Exception as exc:
        return None, f"Failed to create SpeechConfig: {exc}"

    speech_config.set_speech_synthesis_output_format(
        speechsdk.SpeechSynthesisOutputFormat.Riff24Khz16BitMonoPcm
    )
    return speech_config, None


def to_percent(value: int) -> str:
    if value > 0:
        return f"+{value}%"
    return f"{value}%"


def build_ssml(
    text: str,
    voice_name: str,
    rate_percent: int,
    pitch_percent: int,
    language_override: str,
    pronunciation_mappings: list[tuple[str, str]],
) -> str:
    text_node = apply_pronunciation_mappings(text, pronunciation_mappings)

    if language_override != "auto":
        text_node = f"<lang xml:lang='{language_override}'>{text_node}</lang>"

    prosody_open = (
        f"<prosody rate='{to_percent(rate_percent)}' pitch='{to_percent(pitch_percent)}'>"
    )
    prosody_close = "</prosody>"

    content = f"{prosody_open}{text_node}{prosody_close}"

    return (
        "<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' "
        "xmlns:mstts='http://www.w3.org/2001/mstts' xml:lang='en-US'>"
        f"<voice name='{voice_name}'>{content}</voice>"
        "</speak>"
    )


def synthesize_speech(
    text: str,
    voice_name: str,
    rate_percent: int,
    pitch_percent: int,
    language_override: str,
    pronunciation_mappings: list[tuple[str, str]],
) -> tuple[bytes | None, str | None]:
    cache_key = build_synthesis_cache_key(
        text=text,
        voice_name=voice_name,
        rate_percent=rate_percent,
        pitch_percent=pitch_percent,
        language_override=language_override,
        pronunciation_mappings=pronunciation_mappings,
    )

    cached_audio = get_cached_audio(cache_key)
    if cached_audio is not None:
        st.session_state["cache_hits"] = st.session_state.get("cache_hits", 0) + 1
        return cached_audio, None

    st.session_state["cache_misses"] = st.session_state.get("cache_misses", 0) + 1

    speech_config, config_error = build_speech_config()
    if config_error or speech_config is None:
        return None, config_error

    ssml = build_ssml(
        text=text,
        voice_name=voice_name,
        rate_percent=rate_percent,
        pitch_percent=pitch_percent,
        language_override=language_override,
        pronunciation_mappings=pronunciation_mappings,
    )

    synthesizer = speechsdk.SpeechSynthesizer(
        speech_config=speech_config,
        # Keep audio in-memory for Streamlit playback/download.
        audio_config=None,
    )

    try:
        result = synthesizer.speak_ssml_async(ssml).get()
    except Exception as exc:
        return None, f"Speech synthesis request failed: {exc}"

    if result is None:
        return None, "Speech synthesis returned no result."

    if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
        cache_audio(cache_key, result.audio_data)
        return result.audio_data, None

    if result.reason == speechsdk.ResultReason.Canceled:
        details = result.cancellation_details
        if details.reason == speechsdk.CancellationReason.Error:
            return (
                None,
                f"Synthesis canceled ({details.reason}): {details.error_details}",
            )
        return None, f"Synthesis canceled: {details.reason}"

    return None, f"Unexpected synthesis result: {result.reason}"


def is_speakable_text(value: str) -> bool:
    return bool(re.search(r"[A-Za-z0-9\u4e00-\u9fff]", value))


def split_into_line_clips(multiline_text: str) -> list[str]:
    lines_for_clips: list[str] = []
    for raw_line in multiline_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if is_speakable_text(line):
            lines_for_clips.append(line)

    return lines_for_clips


def render_live_line_stats(input_limit: int) -> None:
    html = r"""
<div id="live-sentence-stats" style="font-size:0.85rem;color:rgb(73, 80, 87);padding:2px 0;">
    Line mode stats: waiting for text area...
</div>
<script>
(function () {
    const inputLimit = __INPUT_LIMIT__;
    const output = document.getElementById("live-sentence-stats");

    function isSpeakable(text) {
        return /[A-Za-z0-9\u4e00-\u9fff]/.test(text);
    }

    function countLineClips(multilineText) {
        const lines = multilineText.split(/\r?\n/);
        let count = 0;

        for (let i = 0; i < lines.length; i += 1) {
            const line = lines[i].trim();
            if (!line) continue;

            if (isSpeakable(line)) count += 1;
        }

        return count;
    }

    function findTextarea(doc) {
        const byLabel = doc.querySelector('textarea[aria-label="Text to synthesize"]');
        if (byLabel) return byLabel;

        const textareas = Array.from(doc.querySelectorAll("textarea"));
        if (textareas.length === 0) return null;

        for (let i = 0; i < textareas.length; i += 1) {
            if (String(textareas[i].maxLength || "") === String(inputLimit)) {
                return textareas[i];
            }
        }

        return textareas[textareas.length - 1];
    }

    function update() {
        let textarea = null;
        try {
            textarea = findTextarea(window.parent.document);
        } catch (err) {
            textarea = null;
        }

        if (!textarea) {
            output.textContent = "Line mode stats: waiting for text area...";
            return;
        }

        const text = textarea.value || "";
        const lines = text.split(/\r?\n/);
        const nonEmpty = lines.filter((line) => line.trim().length > 0).length;
        const clipCount = countLineClips(text);

        output.textContent =
            "Line mode stats: " +
            lines.length + " line(s), " +
            nonEmpty + " non-empty line(s), " +
            clipCount + " clip(s) after filtering, " +
            text.length + "/" + inputLimit + " chars.";
    }

    update();
    setInterval(update, 150);
})();
</script>
"""
    components.html(html.replace("__INPUT_LIMIT__", str(input_limit)), height=42)


def parse_pronunciation_mappings(raw_text: str) -> tuple[list[tuple[str, str]], str | None]:
    mappings: list[tuple[str, str]] = []

    if not raw_text.strip():
        return mappings, None

    for line_no, raw_line in enumerate(raw_text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        if "=>" in line:
            word, ph = line.split("=>", 1)
        elif "=" in line:
            word, ph = line.split("=", 1)
        else:
            return [], f"Invalid mapping on line {line_no}. Use format: word = sapi_ph"

        word = word.strip()
        ph = ph.strip()
        if not word or not ph:
            return [], f"Invalid mapping on line {line_no}. Both word and sapi_ph are required."

        mappings.append((word, ph))

    if len(mappings) > MAX_PRONUNCIATION_MAPPINGS:
        return [], (
            f"Too many mappings: {len(mappings)}. "
            f"Maximum is {MAX_PRONUNCIATION_MAPPINGS}."
        )

    # Prefer longer words first to avoid shorter partial matches taking precedence.
    mappings.sort(key=lambda item: len(item[0]), reverse=True)
    return mappings, None


def apply_pronunciation_mappings(text: str, mappings: list[tuple[str, str]]) -> str:
    if not mappings:
        return escape(text)

    phoneme_by_word = {word: ph for word, ph in mappings}
    words = list(phoneme_by_word.keys())
    if not words:
        return escape(text)

    combined_pattern = "|".join(re.escape(word) for word in words)
    matches = list(re.finditer(combined_pattern, text))
    if not matches:
        return escape(text)

    output: list[str] = []
    previous_end = 0

    for match in matches:
        output.append(escape(text[previous_end : match.start()]))
        token = match.group(0)
        ph = phoneme_by_word.get(token, "")
        escaped_ph = escape(ph, quote=True)
        output.append(
            f"<phoneme alphabet='sapi' ph='{escaped_ph}'>{escape(token)}</phoneme>"
        )
        previous_end = match.end()

    output.append(escape(text[previous_end:]))
    return "".join(output)


def build_synthesis_cache_key(
    text: str,
    voice_name: str,
    rate_percent: int,
    pitch_percent: int,
    language_override: str,
    pronunciation_mappings: list[tuple[str, str]],
) -> str:
    payload = {
        "text": text,
        "voice_name": voice_name,
        "rate_percent": rate_percent,
        "pitch_percent": pitch_percent,
        "language_override": language_override,
        "pronunciation_mappings": pronunciation_mappings,
        "output_format": "riff24khz16bitmonopcm",
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


def ensure_synthesis_cache_table(conn: DbConnection) -> None:
    if USE_POSTGRES:
        return

    audio_type = "BYTEA" if USE_POSTGRES else "BLOB"
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS synthesis_cache (
            cache_key TEXT PRIMARY KEY,
            created_ts REAL NOT NULL,
            last_access_ts REAL NOT NULL,
            size_bytes INTEGER NOT NULL,
            audio {audio_type} NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_synthesis_cache_last_access
        ON synthesis_cache(last_access_ts)
        """
    )


def cleanup_synthesis_cache(conn: DbConnection) -> None:
    now = time.time()
    expiry_cutoff = now - SYNTHESIS_CACHE_TTL_SECONDS
    conn.execute(
        "DELETE FROM synthesis_cache WHERE last_access_ts < ?",
        (expiry_cutoff,),
    )

    while True:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) FROM synthesis_cache"
        ).fetchone()
        if row is None:
            break
        count, total_bytes = int(row[0]), int(row[1])

        if count <= SYNTHESIS_CACHE_MAX_ENTRIES and total_bytes <= SYNTHESIS_CACHE_MAX_TOTAL_BYTES:
            break

        conn.execute(
            """
            DELETE FROM synthesis_cache
            WHERE cache_key = (
                SELECT cache_key FROM synthesis_cache
                ORDER BY last_access_ts ASC
                LIMIT 1
            )
            """
        )


def get_cached_audio(cache_key: str) -> bytes | None:
    if not ENABLE_SYNTHESIS_CACHE:
        return None

    now = time.time()

    with RATE_LIMIT_LOCK:
        conn = db_connect(RATE_LIMIT_DB_PATH)
        try:
            ensure_synthesis_cache_table(conn)
            cleanup_synthesis_cache(conn)

            row = conn.execute(
                "SELECT audio FROM synthesis_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
            if row is None:
                conn.commit()
                return None

            conn.execute(
                "UPDATE synthesis_cache SET last_access_ts = ? WHERE cache_key = ?",
                (now, cache_key),
            )
            conn.commit()
            audio_value = row[0]
            if isinstance(audio_value, memoryview):
                return bytes(audio_value)
            return audio_value
        finally:
            conn.close()


def cache_audio(cache_key: str, audio_data: bytes) -> None:
    if not ENABLE_SYNTHESIS_CACHE:
        return
    if len(audio_data) > SYNTHESIS_CACHE_MAX_TOTAL_BYTES:
        return

    now = time.time()

    with RATE_LIMIT_LOCK:
        conn = db_connect(RATE_LIMIT_DB_PATH)
        try:
            ensure_synthesis_cache_table(conn)
            conn.execute(
                """
                INSERT INTO synthesis_cache (cache_key, created_ts, last_access_ts, size_bytes, audio)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    last_access_ts = excluded.last_access_ts,
                    size_bytes = excluded.size_bytes,
                    audio = excluded.audio
                """,
                (cache_key, now, now, len(audio_data), audio_data),
            )
            cleanup_synthesis_cache(conn)
            conn.commit()
        finally:
            conn.close()


def ensure_auth_tables(conn: DbConnection) -> None:
    if USE_POSTGRES:
        return

    is_active_type = "BOOLEAN" if USE_POSTGRES else "INTEGER"
    is_active_default = "TRUE" if USE_POSTGRES else "1"

    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            is_active {is_active_type} NOT NULL DEFAULT {is_active_default},
            created_ts REAL NOT NULL,
            updated_ts REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_metrics (
            username TEXT PRIMARY KEY,
            request_count INTEGER NOT NULL DEFAULT 0,
            cache_hits INTEGER NOT NULL DEFAULT 0,
            cache_misses INTEGER NOT NULL DEFAULT 0,
            clips_generated INTEGER NOT NULL DEFAULT 0,
            chars_processed INTEGER NOT NULL DEFAULT 0,
            error_count INTEGER NOT NULL DEFAULT 0,
            last_request_ts REAL NOT NULL DEFAULT 0,
            updated_ts REAL NOT NULL,
            FOREIGN KEY(username) REFERENCES users(username)
        )
        """
    )


def ensure_auth_schema() -> None:
    if not ENABLE_AUTH:
        return

    with RATE_LIMIT_LOCK:
        conn = db_connect(AUTH_DB_PATH)
        try:
            ensure_auth_tables(conn)
            conn.commit()
        finally:
            conn.close()


def ensure_bootstrap_admin() -> None:
    if not ENABLE_AUTH:
        return

    env_user = os.getenv("TTS_BOOTSTRAP_ADMIN_USERNAME", "").strip()
    env_pass = os.getenv("TTS_BOOTSTRAP_ADMIN_PASSWORD", "").strip()

    bootstrap_user = env_user or BOOTSTRAP_ADMIN_USERNAME
    bootstrap_pass = env_pass or BOOTSTRAP_ADMIN_PASSWORD
    if not bootstrap_user or not bootstrap_pass:
        return

    with RATE_LIMIT_LOCK:
        conn = db_connect(AUTH_DB_PATH)
        try:
            ensure_auth_tables(conn)
            row = conn.execute(
                "SELECT COUNT(*) FROM users WHERE role = ? AND is_active = ?",
                ("admin", db_bool(True)),
            ).fetchone()
            admin_count = int(row[0]) if row else 0
            if admin_count > 0:
                return

            now = time.time()
            conn.execute(
                """
                INSERT INTO users (username, password_hash, role, is_active, created_ts, updated_ts)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    password_hash = excluded.password_hash,
                    role = excluded.role,
                    is_active = excluded.is_active,
                    updated_ts = excluded.updated_ts
                """,
                (
                    bootstrap_user,
                    PWD_CONTEXT.hash(bootstrap_pass),
                    "admin",
                    db_bool(True),
                    now,
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()


def list_users() -> list[dict[str, Any]]:
    with RATE_LIMIT_LOCK:
        conn = db_connect(AUTH_DB_PATH)
        try:
            ensure_auth_tables(conn)
            rows = conn.execute(
                """
                SELECT username, role, is_active, created_ts, updated_ts
                FROM users
                ORDER BY username ASC
                """
            ).fetchall()
            return [
                {
                    "username": row[0],
                    "role": row[1],
                    "active": bool(row[2]),
                    "created_ts": row[3],
                    "updated_ts": row[4],
                }
                for row in rows
            ]
        finally:
            conn.close()


def create_user(username: str, password: str, role: str = "user") -> tuple[bool, str]:
    user = username.strip()
    if not user:
        return False, "Username is required."
    if len(password) < 8:
        return False, "Password must be at least 8 characters."
    if role not in {"admin", "user"}:
        return False, "Invalid role."

    now = time.time()
    with RATE_LIMIT_LOCK:
        conn = db_connect(AUTH_DB_PATH)
        try:
            ensure_auth_tables(conn)
            existing = conn.execute(
                "SELECT 1 FROM users WHERE username = ?",
                (user,),
            ).fetchone()
            if existing is not None:
                return False, "Username already exists."

            conn.execute(
                """
                INSERT INTO users (username, password_hash, role, is_active, created_ts, updated_ts)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user, PWD_CONTEXT.hash(password), role, db_bool(True), now, now),
            )
            conn.commit()
            return True, "User created successfully."
        finally:
            conn.close()


def set_user_password(username: str, new_password: str) -> tuple[bool, str]:
    user = username.strip()
    if not user:
        return False, "Username is required."
    if len(new_password) < 8:
        return False, "Password must be at least 8 characters."

    now = time.time()
    with RATE_LIMIT_LOCK:
        conn = db_connect(AUTH_DB_PATH)
        try:
            ensure_auth_tables(conn)
            row = conn.execute(
                "SELECT 1 FROM users WHERE username = ?",
                (user,),
            ).fetchone()
            if row is None:
                return False, "User not found."

            conn.execute(
                "UPDATE users SET password_hash = ?, updated_ts = ? WHERE username = ?",
                (PWD_CONTEXT.hash(new_password), now, user),
            )
            conn.commit()
            return True, "Password updated."
        finally:
            conn.close()


def set_user_active(username: str, is_active: bool) -> tuple[bool, str]:
    user = username.strip()
    if not user:
        return False, "Username is required."

    now = time.time()
    with RATE_LIMIT_LOCK:
        conn = db_connect(AUTH_DB_PATH)
        try:
            ensure_auth_tables(conn)
            row = conn.execute(
                "SELECT 1 FROM users WHERE username = ?",
                (user,),
            ).fetchone()
            if row is None:
                return False, "User not found."

            conn.execute(
                "UPDATE users SET is_active = ?, updated_ts = ? WHERE username = ?",
                (db_bool(is_active), now, user),
            )
            conn.commit()
            return True, "User status updated."
        finally:
            conn.close()


def authenticate_user(username: str, password: str) -> tuple[bool, str, str | None]:
    user = username.strip()
    if not user or not password:
        return False, "Username and password are required.", None

    with RATE_LIMIT_LOCK:
        conn = db_connect(AUTH_DB_PATH)
        try:
            ensure_auth_tables(conn)
            row = conn.execute(
                "SELECT password_hash, role, is_active FROM users WHERE username = ?",
                (user,),
            ).fetchone()
            if row is None:
                return False, "Invalid username or password.", None

            password_hash, role, is_active = row
            if not bool(is_active):
                return False, "This account is disabled.", None
            if not PWD_CONTEXT.verify(password, password_hash):
                return False, "Invalid username or password.", None
            return True, "Login successful.", str(role)
        finally:
            conn.close()


def upsert_user_metrics(
    username: str,
    requests: int,
    cache_hits: int,
    cache_misses: int,
    clips_generated: int,
    chars_processed: int,
    errors: int,
) -> None:
    if not ENABLE_AUTH:
        return

    user = username.strip()
    if not user:
        return

    now = time.time()

    with RATE_LIMIT_LOCK:
        conn = db_connect(AUTH_DB_PATH)
        try:
            ensure_auth_tables(conn)
            conn.execute(
                """
                INSERT INTO user_metrics (
                    username,
                    request_count,
                    cache_hits,
                    cache_misses,
                    clips_generated,
                    chars_processed,
                    error_count,
                    last_request_ts,
                    updated_ts
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    request_count = user_metrics.request_count + EXCLUDED.request_count,
                    cache_hits = user_metrics.cache_hits + EXCLUDED.cache_hits,
                    cache_misses = user_metrics.cache_misses + EXCLUDED.cache_misses,
                    clips_generated = user_metrics.clips_generated + EXCLUDED.clips_generated,
                    chars_processed = user_metrics.chars_processed + EXCLUDED.chars_processed,
                    error_count = user_metrics.error_count + EXCLUDED.error_count,
                    last_request_ts = EXCLUDED.last_request_ts,
                    updated_ts = EXCLUDED.updated_ts
                """,
                (
                    user,
                    max(0, requests),
                    max(0, cache_hits),
                    max(0, cache_misses),
                    max(0, clips_generated),
                    max(0, chars_processed),
                    max(0, errors),
                    now,
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()


def get_user_metrics_rows() -> list[dict[str, Any]]:
    with RATE_LIMIT_LOCK:
        conn = db_connect(AUTH_DB_PATH)
        try:
            ensure_auth_tables(conn)
            rows = conn.execute(
                """
                SELECT
                    u.username,
                    u.role,
                    u.is_active,
                    COALESCE(m.request_count, 0),
                    COALESCE(m.cache_hits, 0),
                    COALESCE(m.cache_misses, 0),
                    COALESCE(m.clips_generated, 0),
                    COALESCE(m.chars_processed, 0),
                    COALESCE(m.error_count, 0),
                    COALESCE(m.last_request_ts, 0)
                FROM users u
                LEFT JOIN user_metrics m ON m.username = u.username
                ORDER BY u.username ASC
                """
            ).fetchall()
            return [
                {
                    "username": row[0],
                    "role": row[1],
                    "active": bool(row[2]),
                    "requests": int(row[3]),
                    "cache_hits": int(row[4]),
                    "cache_misses": int(row[5]),
                    "clips": int(row[6]),
                    "chars": int(row[7]),
                    "errors": int(row[8]),
                    "last_request_ts": float(row[9]),
                }
                for row in rows
            ]
        finally:
            conn.close()


def build_zip_from_audio_items(audio_items: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zip_file:
        for filename, audio_bytes in audio_items:
            zip_file.writestr(filename, audio_bytes)
    return buffer.getvalue()


def synthesize_line_batch(
    lines_for_clips: list[str],
    voice_name: str,
    rate_percent: int,
    pitch_percent: int,
    language_override: str,
    pronunciation_mappings: list[tuple[str, str]],
) -> tuple[list[tuple[str, bytes]] | None, str | None]:
    audio_items: list[tuple[str, bytes]] = []

    for idx, line_text in enumerate(lines_for_clips, start=1):
        audio_data, error_message = synthesize_speech(
            text=line_text,
            voice_name=voice_name,
            rate_percent=rate_percent,
            pitch_percent=pitch_percent,
            language_override=language_override,
            pronunciation_mappings=pronunciation_mappings,
        )
        if error_message:
            return None, f"Line {idx} failed: {error_message}"
        if audio_data is None:
            return None, f"Line {idx} returned empty audio."

        filename = f"{idx:03d}.wav"
        audio_items.append((filename, audio_data))

    return audio_items, None


def get_header_value(headers: object, header_name: str) -> str:
    iterable_items: Any = None
    if isinstance(headers, dict):
        iterable_items = headers.items()
    else:
        items_callable = getattr(headers, "items", None)
        if callable(items_callable):
            try:
                iterable_items = items_callable()
            except Exception:
                iterable_items = None

    if iterable_items is None:
        return ""

    for key, value in iterable_items:
        if str(key).lower() == header_name.lower():
            return str(value).strip()
    return ""


def get_client_identifier() -> str:
    if ENABLE_AUTH:
        auth_user = st.session_state.get("auth_username", "")
        if auth_user:
            return f"user:{auth_user}"

    context = getattr(st, "context", None)
    headers = getattr(context, "headers", {}) if context is not None else {}

    forwarded_for = get_header_value(headers, "x-forwarded-for")
    real_ip = get_header_value(headers, "x-real-ip")

    raw_client = ""
    if forwarded_for:
        raw_client = forwarded_for.split(",")[0].strip()
    elif real_ip:
        raw_client = real_ip.strip()

    # Fallback for local/dev environments where IP headers are unavailable.
    if not raw_client:
        token = st.session_state.get("_client_token")
        if token is None:
            token = str(uuid.uuid4())
            st.session_state["_client_token"] = token
        raw_client = f"session:{token}"

    return sha256(raw_client.encode("utf-8")).hexdigest()


def ensure_rate_limit_table(conn: DbConnection) -> None:
    if USE_POSTGRES:
        return

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS usage_events (
            client_id TEXT NOT NULL,
            ts INTEGER NOT NULL,
            units INTEGER NOT NULL
        )
        """
    )


def ensure_burst_guard_table(conn: DbConnection) -> None:
    if USE_POSTGRES:
        return

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS request_attempts (
            client_id TEXT NOT NULL,
            ts REAL NOT NULL
        )
        """
    )


def check_burst_guard(client_id: str) -> tuple[bool, str | None]:
    if MIN_SECONDS_BETWEEN_REQUESTS <= 0:
        return True, None

    now = time.time()

    with RATE_LIMIT_LOCK:
        conn = db_connect(RATE_LIMIT_DB_PATH)
        try:
            ensure_burst_guard_table(conn)
            conn.execute(
                "DELETE FROM request_attempts WHERE ts < ?",
                (now - RATE_LIMIT_WINDOW_SECONDS,),
            )

            row = conn.execute(
                "SELECT MAX(ts) FROM request_attempts WHERE client_id = ?",
                (client_id,),
            ).fetchone()
            last_ts = row[0] if row else None

            if last_ts is not None:
                elapsed = now - float(last_ts)
                if elapsed < MIN_SECONDS_BETWEEN_REQUESTS:
                    wait_seconds = max(1, int(MIN_SECONDS_BETWEEN_REQUESTS - elapsed + 0.999))
                    return (
                        False,
                        f"Too many attempts in a short burst. Please wait about {wait_seconds} second(s) and try again.",
                    )

            conn.execute(
                "INSERT INTO request_attempts (client_id, ts) VALUES (?, ?)",
                (client_id, now),
            )
            conn.commit()
            return True, None
        finally:
            conn.close()


def consume_quota(client_id: str, units: int) -> tuple[bool, int, str | None]:
    now = int(time.time())
    window_start = now - RATE_LIMIT_WINDOW_SECONDS

    with RATE_LIMIT_LOCK:
        conn = db_connect(RATE_LIMIT_DB_PATH)
        try:
            ensure_rate_limit_table(conn)

            conn.execute("DELETE FROM usage_events WHERE ts < ?", (window_start,))

            used = conn.execute(
                "SELECT COALESCE(SUM(units), 0) FROM usage_events WHERE client_id = ? AND ts >= ?",
                (client_id, window_start),
            ).fetchone()[0]

            if used + units > MAX_HOURLY_SYNTHESIS_UNITS:
                remaining = max(0, MAX_HOURLY_SYNTHESIS_UNITS - used)
                message = (
                    "Hourly limit reached for this client. "
                    f"Used {used}/{MAX_HOURLY_SYNTHESIS_UNITS} unit(s). "
                    f"Remaining {remaining} unit(s) in this hour."
                )
                return False, remaining, message

            conn.execute(
                "INSERT INTO usage_events (client_id, ts, units) VALUES (?, ?, ?)",
                (client_id, now, units),
            )
            conn.commit()

            remaining = MAX_HOURLY_SYNTHESIS_UNITS - (used + units)
            return True, remaining, None
        finally:
            conn.close()


def format_ts(ts_value: float) -> str:
    if ts_value <= 0:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts_value))


def render_login_gate() -> None:
    st.subheader("Sign in")
    with st.form("login_form", clear_on_submit=False):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        login_clicked = st.form_submit_button("Login", type="primary")

    if login_clicked:
        ok, message, role = authenticate_user(username, password)
        if not ok or role is None:
            st.error(message)
            return

        st.session_state["auth_username"] = username.strip()
        st.session_state["auth_role"] = role
        st.success("Signed in successfully.")
        st.rerun()


def render_auth_toolbar() -> None:
    user = st.session_state.get("auth_username", "")
    role = st.session_state.get("auth_role", "user")

    left, right = st.columns([5, 1])
    with left:
        st.caption(f"Signed in as: {user} ({role})")
    with right:
        if st.button("Logout"):
            st.session_state["auth_username"] = ""
            st.session_state["auth_role"] = ""
            st.session_state["run_requested"] = False
            st.session_state["is_processing"] = False
            st.rerun()


def render_admin_user_panel() -> None:
    with st.expander("Admin: User Management", expanded=False):
        st.write("Create and manage users.")

        with st.form("admin_create_user"):
            col_u, col_r = st.columns([2, 1])
            with col_u:
                new_username = st.text_input("New username")
            with col_r:
                new_role = st.selectbox("Role", options=["user", "admin"], index=0)
            new_password = st.text_input("New user password", type="password")
            create_clicked = st.form_submit_button("Create user")

        if create_clicked:
            ok, msg = create_user(new_username, new_password, new_role)
            if ok:
                st.success(msg)
            else:
                st.error(msg)

        users = list_users()
        if users:
            rows = []
            for user in users:
                rows.append(
                    {
                        "username": user["username"],
                        "role": user["role"],
                        "active": user["active"],
                        "created": format_ts(float(user["created_ts"])),
                        "updated": format_ts(float(user["updated_ts"])),
                    }
                )
            st.dataframe(rows, use_container_width=True)

        with st.form("admin_update_password"):
            target_username = st.text_input("Reset password for username")
            target_password = st.text_input("New password", type="password")
            reset_clicked = st.form_submit_button("Reset password")

        if reset_clicked:
            ok, msg = set_user_password(target_username, target_password)
            if ok:
                st.success(msg)
            else:
                st.error(msg)

        with st.form("admin_toggle_status"):
            status_user = st.text_input("Set active status for username")
            status_value = st.selectbox("Status", options=["active", "disabled"], index=0)
            status_clicked = st.form_submit_button("Update status")

        if status_clicked:
            ok, msg = set_user_active(status_user, status_value == "active")
            if ok:
                st.success(msg)
            else:
                st.error(msg)


def render_admin_metrics_panel() -> None:
    with st.expander("Admin: Usage Metrics", expanded=False):
        rows = get_user_metrics_rows()
        if not rows:
            st.info("No user metrics yet.")
            return

        view_rows = []
        for row in rows:
            view_rows.append(
                {
                    "username": row["username"],
                    "role": row["role"],
                    "active": row["active"],
                    "requests": row["requests"],
                    "cache_hits": row["cache_hits"],
                    "cache_misses": row["cache_misses"],
                    "clips": row["clips"],
                    "chars": row["chars"],
                    "errors": row["errors"],
                    "last_request": format_ts(row["last_request_ts"]),
                }
            )
        st.dataframe(view_rows, use_container_width=True)


def initialize_session_state() -> None:
    if "is_processing" not in st.session_state:
        st.session_state["is_processing"] = False
    if "run_requested" not in st.session_state:
        st.session_state["run_requested"] = False
    if "cache_hits" not in st.session_state:
        st.session_state["cache_hits"] = 0
    if "cache_misses" not in st.session_state:
        st.session_state["cache_misses"] = 0
    if "auth_username" not in st.session_state:
        st.session_state["auth_username"] = ""
    if "auth_role" not in st.session_state:
        st.session_state["auth_role"] = ""


def ensure_auth_ready_for_page() -> bool:
    if not ENABLE_AUTH:
        return True

    try:
        ensure_auth_schema()
        ensure_bootstrap_admin()
    except Exception as exc:
        st.error(f"Failed to initialize auth system: {exc}")
        return False

    if not st.session_state.get("auth_username"):
        render_login_gate()
        if len(list_users()) == 0:
            st.info(
                "No users available yet. Set bootstrap admin credentials via "
                "`TTS_BOOTSTRAP_ADMIN_USERNAME` and `TTS_BOOTSTRAP_ADMIN_PASSWORD` "
                "or `bootstrap_admin_*` in app config, then restart the app."
            )
        return False

    return True


def hide_sidebar_ui() -> None:
    st.markdown(
        """
        <style>
        [data-testid="stSidebar"] {display: none;}
        [data-testid="collapsedControl"] {display: none;}
        </style>
        """,
        unsafe_allow_html=True,
    )


def run_main_page() -> None:
    st.set_page_config(
        page_title="Azure Text-to-Speech",
        page_icon="🔊",
        initial_sidebar_state="collapsed",
    )
    hide_sidebar_ui()
    st.title("🔊 Azure Speech Text-to-Speech")
    st.caption("Convert text into spoken audio using Azure AI Speech.")

    initialize_session_state()

    if ENABLE_AUTH:
        if not ensure_auth_ready_for_page():
            st.stop()

        render_auth_toolbar()
        if st.session_state.get("auth_role") == "admin":
            st.info("Admin tools are on a separate page.")
            if st.button("🛠️ Open Admin Page"):
                st.switch_page("pages/1_Admin.py")

    voice_option = st.selectbox(
        "Choose a voice",
        options=VOICE_OPTIONS,
        format_func=lambda item: item["label"],
        index=0,
    )

    output_mode = st.radio(
        "Output mode",
        options=OUTPUT_MODE_OPTIONS,
        format_func=lambda item: item["label"],
        index=0,
    )

    with st.expander("SSML options", expanded=False):
        col_rate, col_pitch, col_lang = st.columns([1, 1, 1.5])

        with col_rate:
            rate_percent = st.slider(
                "Rate (%)",
                min_value=-50,
                max_value=100,
                value=0,
                step=1,
                help="0 is neutral. Positive values speak faster.",
            )

        with col_pitch:
            pitch_percent = st.slider(
                "Pitch (%)",
                min_value=-50,
                max_value=50,
                value=0,
                step=1,
                help="0 is neutral. Positive values sound higher.",
            )

        with col_lang:
            language_selection = st.selectbox(
                "Language override",
                options=LANGUAGE_OVERRIDE_OPTIONS,
                format_func=lambda item: item["label"],
                index=0,
                help="Use with multilingual voices to force pronunciation language.",
            )

        use_pronunciation_mappings = st.toggle("Use pronunciation mappings", value=False)
        pronunciation_mappings_text = ""
        if use_pronunciation_mappings:
            pronunciation_mappings_text = st.text_area(
                "Mappings (one per line: word = sapi_ph)",
                value="港 = gong2",
                height=110,
                help="Example:\n港 = gong2\n香港 = heung1 gong2",
            )

    current_input_limit = (
        MAX_SINGLE_INPUT_CHARS
        if output_mode["value"] == "single"
        else MAX_BATCH_INPUT_CHARS
    )

    text_input = st.text_area(
        "Text to synthesize",
        value="你好，歡迎使用 Azure AI Speech。這是一個測試。",
        height=180,
        max_chars=current_input_limit,
    )

    if output_mode["value"] == "line_zip":
        render_live_line_stats(current_input_limit)

    st.caption(
        f"Limits: single mode max {MAX_SINGLE_INPUT_CHARS:,} chars; "
        f"line mode max {MAX_BATCH_INPUT_CHARS:,} chars and {MAX_BATCH_LINES} lines; "
        f"hourly quota {MAX_HOURLY_SYNTHESIS_UNITS} synthesis units per client; "
        f"burst guard {MIN_SECONDS_BETWEEN_REQUESTS}s between attempts."
    )

    if st.button(
        "Convert to Speech",
        type="primary",
        disabled=st.session_state["is_processing"],
    ):
        st.session_state["is_processing"] = True
        st.session_state["run_requested"] = True

    if st.session_state["run_requested"]:
        metric_username = st.session_state.get("auth_username", "")
        metric_requests = 0
        metric_clips = 0
        metric_chars = 0
        metric_errors = 0

        try:
            st.session_state["cache_hits"] = 0
            st.session_state["cache_misses"] = 0

            text_value = text_input.strip()
            if not text_value:
                st.warning("Please enter some text.")
                metric_errors += 1
                st.stop()

            metric_requests = 1

            selected_voice = voice_option["value"]
            selected_language = language_selection["value"]
            client_id = get_client_identifier()

            allowed, burst_message = check_burst_guard(client_id)
            if not allowed:
                st.error(burst_message)
                metric_errors += 1
                st.stop()

            pronunciation_mappings, mappings_error = parse_pronunciation_mappings(
                pronunciation_mappings_text
            )
            if mappings_error:
                st.error(mappings_error)
                metric_errors += 1
                st.stop()

            if output_mode["value"] == "single":
                if len(text_value) > MAX_SINGLE_INPUT_CHARS:
                    st.error(
                        f"Single mode input is too long ({len(text_value)} chars). "
                        f"Maximum is {MAX_SINGLE_INPUT_CHARS}."
                    )
                    metric_errors += 1
                    st.stop()

                metric_clips = 1
                metric_chars = len(text_value)

                allowed, remaining_units, limit_message = consume_quota(client_id, units=1)
                if not allowed:
                    st.error(limit_message)
                    metric_errors += 1
                    st.stop()

                with st.spinner("Synthesizing audio..."):
                    audio_data, error_message = synthesize_speech(
                        text=text_value,
                        voice_name=selected_voice,
                        rate_percent=rate_percent,
                        pitch_percent=pitch_percent,
                        language_override=selected_language,
                        pronunciation_mappings=pronunciation_mappings,
                    )

                if error_message:
                    metric_errors += 1
                    st.error(error_message)
                elif audio_data:
                    st.success("Speech synthesis completed.")
                    if ENABLE_SYNTHESIS_CACHE:
                        st.caption(
                            f"Cache: {st.session_state['cache_hits']} hit(s), "
                            f"{st.session_state['cache_misses']} miss(es)"
                        )
                    st.caption(f"Hourly quota remaining: {remaining_units} unit(s)")
                    st.audio(audio_data, format="audio/wav")
                    st.download_button(
                        label="Download WAV",
                        data=audio_data,
                        file_name="speech.wav",
                        mime="audio/wav",
                    )
            else:
                lines_for_clips = split_into_line_clips(text_value)
                if not lines_for_clips:
                    st.warning("No valid lines found. Empty lines or symbol-only lines are ignored.")
                    metric_errors += 1
                    st.stop()

                total_clip_chars = sum(len(line) for line in lines_for_clips)
                if len(lines_for_clips) > MAX_BATCH_LINES:
                    st.error(
                        f"Line mode found {len(lines_for_clips)} lines for clips. "
                        f"Maximum is {MAX_BATCH_LINES} per request."
                    )
                    metric_errors += 1
                    st.stop()
                if total_clip_chars > MAX_BATCH_INPUT_CHARS:
                    st.error(
                        f"Line mode has {total_clip_chars} total chars. "
                        f"Maximum is {MAX_BATCH_INPUT_CHARS}."
                    )
                    metric_errors += 1
                    st.stop()

                metric_clips = len(lines_for_clips)
                metric_chars = total_clip_chars

                units = len(lines_for_clips)
                allowed, remaining_units, limit_message = consume_quota(client_id, units=units)
                if not allowed:
                    st.error(limit_message)
                    metric_errors += 1
                    st.stop()

                with st.spinner(f"Synthesizing {len(lines_for_clips)} line clips..."):
                    audio_items, error_message = synthesize_line_batch(
                        lines_for_clips=lines_for_clips,
                        voice_name=selected_voice,
                        rate_percent=rate_percent,
                        pitch_percent=pitch_percent,
                        language_override=selected_language,
                        pronunciation_mappings=pronunciation_mappings,
                    )

                if error_message:
                    metric_errors += 1
                    st.error(error_message)
                elif audio_items:
                    zip_data = build_zip_from_audio_items(audio_items)
                    st.success(f"Created {len(audio_items)} line clips.")
                    if ENABLE_SYNTHESIS_CACHE:
                        st.caption(
                            f"Cache: {st.session_state['cache_hits']} hit(s), "
                            f"{st.session_state['cache_misses']} miss(es)"
                        )
                    st.caption(f"Hourly quota remaining: {remaining_units} unit(s)")
                    st.caption(f"Previewing first clip: {audio_items[0][0]}")
                    st.audio(audio_items[0][1], format="audio/wav")
                    st.download_button(
                        label="Download ZIP",
                        data=zip_data,
                        file_name="speech_clips.zip",
                        mime="application/zip",
                    )
        finally:
            if ENABLE_AUTH and metric_username:
                try:
                    upsert_user_metrics(
                        username=metric_username,
                        requests=metric_requests,
                        cache_hits=int(st.session_state.get("cache_hits", 0)),
                        cache_misses=int(st.session_state.get("cache_misses", 0)),
                        clips_generated=metric_clips,
                        chars_processed=metric_chars,
                        errors=metric_errors,
                    )
                except Exception as exc:
                    st.warning(f"Usage metrics update failed: {exc}")
            st.session_state["run_requested"] = False
            st.session_state["is_processing"] = False


if __name__ == "__main__":
    run_main_page()
