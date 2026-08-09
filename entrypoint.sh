#!/bin/sh
set -eu

# Timeweb can expose configured variables as empty strings. Normalize those
# to safe defaults so the web process can always boot and answer /health.
if [ -z "${DATABASE_URL:-}" ]; then
  export DATABASE_URL="sqlite:///./psyhowl.db"
fi

# SQLAlchemy's plain postgresql:// URL normally looks for psycopg2, while this
# image intentionally ships psycopg v3. Normalize common provider URLs.
case "$DATABASE_URL" in
  postgresql://*)
    export DATABASE_URL="postgresql+psycopg://${DATABASE_URL#postgresql://}"
    ;;
  postgres://*)
    export DATABASE_URL="postgresql+psycopg://${DATABASE_URL#postgres://}"
    ;;
esac

if [ -z "${SUBSCRIPTION_PRICE_RUB:-}" ]; then
  export SUBSCRIPTION_PRICE_RUB="12990"
fi

if [ -z "${SUBSCRIPTION_DAYS:-}" ]; then
  export SUBSCRIPTION_DAYS="30"
fi

if [ -z "${OPENAI_CHAT_MODEL:-}" ]; then
  export OPENAI_CHAT_MODEL="gpt-5"
fi

if [ -z "${OPENAI_TRANSCRIBE_MODEL:-}" ]; then
  export OPENAI_TRANSCRIBE_MODEL="gpt-4o-transcribe"
fi

if [ -z "${OPENAI_TTS_MODEL:-}" ]; then
  export OPENAI_TTS_MODEL="gpt-4o-mini-tts"
fi

if [ -z "${OPENAI_TTS_VOICE:-}" ]; then
  export OPENAI_TTS_VOICE="marin"
fi

PORT="${PORT:-8080}"
echo "[psyhowl] starting web server on 0.0.0.0:${PORT}"
exec uvicorn app:app --host 0.0.0.0 --port "$PORT" --proxy-headers --forwarded-allow-ips='*'
