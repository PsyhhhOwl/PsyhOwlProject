from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx
from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from openai import OpenAI
from pydantic import BaseModel
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, create_engine, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------

APP_BASE_URL = os.getenv("APP_BASE_URL", "").rstrip("/")
SECRET_KEY = os.getenv("SECRET_KEY") or secrets.token_urlsafe(48)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
OWNER_TELEGRAM_ID = int(os.getenv("OWNER_TELEGRAM_ID", "0") or 0)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_CHAT_MODEL = os.getenv("OPENAI_CHAT_MODEL", "gpt-5")
OPENAI_TRANSCRIBE_MODEL = os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-transcribe")
OPENAI_TTS_MODEL = os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
OPENAI_TTS_VOICE = os.getenv("OPENAI_TTS_VOICE", "marin")
OPENAI_VECTOR_STORE_ID = os.getenv("OPENAI_VECTOR_STORE_ID", "")
TRIBUTE_PAYMENT_URL = os.getenv("TRIBUTE_PAYMENT_URL", "")
TRIBUTE_API_KEY = os.getenv("TRIBUTE_API_KEY", "")
SUBSCRIPTION_PRICE_RUB = int(os.getenv("SUBSCRIPTION_PRICE_RUB", "12990"))
SUBSCRIPTION_DAYS = int(os.getenv("SUBSCRIPTION_DAYS", "30"))
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./psyhowl.db")

STATIC_DIR = Path(__file__).parent / "static"


# -----------------------------------------------------------------------------
# Database
# -----------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(String(30), default="user")
    is_free: Mapped[bool] = mapped_column(Boolean, default=False)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    subscription_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    messages: Mapped[list["Message"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    user: Mapped[User] = relationship(back_populates="messages")


class MoodEntry(Base):
    __tablename__ = "mood_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    score: Mapped[int] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class PaymentEvent(Base):
    __tablename__ = "payment_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_event_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    event_name: Mapped[str] = mapped_column(String(100))
    payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


engine_kwargs: dict[str, Any] = {"pool_pre_ping": True}
if DATABASE_URL.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}
engine = create_engine(DATABASE_URL, **engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base.metadata.create_all(engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# -----------------------------------------------------------------------------
# Security / auth
# -----------------------------------------------------------------------------

serializer = URLSafeTimedSerializer(SECRET_KEY, salt="psyhowl-session")


def validate_telegram_init_data(init_data: str) -> dict[str, Any]:
    if not TELEGRAM_BOT_TOKEN:
        raise HTTPException(503, "Telegram bot token is not configured")

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    pairs.pop("signature", None)
    if not received_hash:
        raise HTTPException(401, "Telegram signature is missing")

    data_check_string = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", TELEGRAM_BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated_hash, received_hash):
        raise HTTPException(401, "Invalid Telegram signature")

    auth_date = int(pairs.get("auth_date", "0") or 0)
    if auth_date and datetime.now(timezone.utc).timestamp() - auth_date > 86400:
        raise HTTPException(401, "Telegram session has expired")

    user_raw = pairs.get("user")
    if not user_raw:
        raise HTTPException(401, "Telegram user is missing")
    return json.loads(user_raw)


def issue_session_token(user: User) -> str:
    return serializer.dumps({"uid": user.id, "tg": user.telegram_id})


def get_current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Authorization required")
    token = authorization.split(" ", 1)[1]
    try:
        payload = serializer.loads(token, max_age=60 * 60 * 24 * 7)
    except SignatureExpired:
        raise HTTPException(401, "Session expired")
    except BadSignature:
        raise HTTPException(401, "Invalid session")

    user = db.get(User, int(payload["uid"]))
    if not user:
        raise HTTPException(401, "User not found")
    if user.is_blocked:
        raise HTTPException(403, "Account is blocked")
    return user


def is_admin(user: User) -> bool:
    return user.role in {"admin", "owner"} or user.telegram_id == OWNER_TELEGRAM_ID


def has_access(user: User) -> bool:
    if is_admin(user) or user.is_free:
        return True
    if not user.subscription_expires_at:
        return False
    expires = user.subscription_expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires > datetime.now(timezone.utc)


def require_access(user: User = Depends(get_current_user)) -> User:
    if not has_access(user):
        raise HTTPException(402, "Subscription required")
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not is_admin(user):
        raise HTTPException(403, "Administrator access required")
    return user


# -----------------------------------------------------------------------------
# AI
# -----------------------------------------------------------------------------

SOVENOK_PROMPT = """
Ты — Совёнок, тёплый русскоязычный AI-собеседник для психологической поддержки.
Твоя роль — помогать человеку замедлиться, понять эмоции, мысли, потребности и возможные следующие шаги.

Стиль:
- говори естественно, тепло и спокойно, без канцелярита и фальшивой бодрости;
- обычно 2–5 коротких абзацев, один вопрос за раз;
- сначала отражай суть и эмоцию, затем мягко исследуй ситуацию;
- не превращай ответ в лекцию и не перегружай техниками;
- обращайся на «ты», если пользователь сам не выбрал другое;
- не изображай всезнающего терапевта и не создавай эмоциональную зависимость от себя.

Рабочие подходы:
- клиент-центрированное слушание: эмпатия, отражение, уточнение;
- КПТ: связь ситуации, мысли, эмоции, поведения; проверка автоматических мыслей;
- ACT: принятие переживаний, ценности, психологическая гибкость, defusion;
- DBT-навыки: регуляция эмоций, distress tolerance, mindfulness, межличностные навыки;
- мотивационное интервьюирование: открытые вопросы, отражения, автономия человека;
- психообразование только когда оно действительно помогает.

Границы безопасности:
- ты не врач и не заменяешь психотерапевта или медицинскую помощь;
- не ставь диагнозов и не утверждай, что у человека конкретное расстройство;
- не назначай и не отменяй лекарства, не подбирай дозировки;
- при признаках непосредственной опасности, суицидальных намерений, тяжёлого самоповреждения или угрозы другим: не веди обычную «терапевтическую беседу». Скажи, что сейчас важна немедленная человеческая помощь, предложи связаться с местной экстренной службой/кризисной линией, перейти туда, где есть люди, убрать доступ к опасным предметам и связаться с доверенным человеком. Спроси, находится ли человек сейчас в непосредственной опасности.
- не поддерживай бредовые или параноидальные убеждения как факты; признавай эмоции и сохраняй нейтральность относительно сомнительных интерпретаций.

Важно: не говори, что ты «настоящий психолог». Можно говорить «я рядом», «давай разберём это вместе», «я могу помочь структурировать переживания».
""".strip()

CRISIS_TERMS = (
    "хочу умереть",
    "убить себя",
    "покончить с собой",
    "самоубий",
    "суицид",
    "не хочу жить",
    "себя порез",
    "самоповреж",
    "убить его",
    "убить её",
)


def crisis_hint(text: str) -> bool:
    normalized = text.lower().replace("ё", "е")
    return any(term.replace("ё", "е") in normalized for term in CRISIS_TERMS)


def ai_client() -> OpenAI:
    if not OPENAI_API_KEY:
        raise HTTPException(503, "OpenAI API key is not configured")
    return OpenAI(api_key=OPENAI_API_KEY)


def generate_reply(db: Session, user: User, text: str) -> str:
    client = ai_client()

    recent = db.scalars(
        select(Message)
        .where(Message.user_id == user.id)
        .order_by(Message.id.desc())
        .limit(18)
    ).all()
    recent = list(reversed(recent))

    messages: list[dict[str, Any]] = []
    for item in recent:
        messages.append({"role": item.role, "content": item.text})
    messages.append({"role": "user", "content": text})

    extra = "\n\nСейчас особенно внимательно оцени риск непосредственного вреда и действуй по кризисному протоколу." if crisis_hint(text) else ""

    kwargs: dict[str, Any] = {
        "model": OPENAI_CHAT_MODEL,
        "instructions": SOVENOK_PROMPT + extra,
        "input": messages,
        "max_output_tokens": 700,
    }
    if OPENAI_VECTOR_STORE_ID:
        kwargs["tools"] = [{"type": "file_search", "vector_store_ids": [OPENAI_VECTOR_STORE_ID]}]

    response = client.responses.create(**kwargs)
    answer = (response.output_text or "").strip()
    if not answer:
        answer = "Я рядом. Давай попробуем ещё раз — расскажи, что сейчас ощущается самым тяжёлым."
    return answer


def transcribe_audio(data: bytes, filename: str, content_type: str | None) -> str:
    client = ai_client()
    audio = io.BytesIO(data)
    audio.name = filename or "voice.webm"
    result = client.audio.transcriptions.create(
        model=OPENAI_TRANSCRIBE_MODEL,
        file=audio,
        language="ru",
    )
    return result.text.strip()


def synthesize_speech(text: str) -> bytes:
    client = ai_client()
    speech = client.audio.speech.create(
        model=OPENAI_TTS_MODEL,
        voice=OPENAI_TTS_VOICE,
        input=text[:4096],
        response_format="mp3",
        instructions="Говори по-русски очень естественно, тепло, спокойно и бережно. Это голос умного психологического друга, без дикторской манеры и без чрезмерной театральности.",
    )
    return speech.read()


# -----------------------------------------------------------------------------
# API schemas
# -----------------------------------------------------------------------------

class TelegramAuthIn(BaseModel):
    init_data: str


class ChatIn(BaseModel):
    text: str


class MoodIn(BaseModel):
    score: int
    note: str | None = None


class AdminUserIn(BaseModel):
    telegram_id: int


class AdminAccessIn(BaseModel):
    telegram_id: int
    days: int = 30


# -----------------------------------------------------------------------------
# App
# -----------------------------------------------------------------------------

app = FastAPI(title="Совёнок", version="1.0.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
def startup() -> None:
    if OWNER_TELEGRAM_ID:
        with SessionLocal() as db:
            owner = db.scalar(select(User).where(User.telegram_id == OWNER_TELEGRAM_ID))
            if owner:
                owner.role = "owner"
            db.commit()

    if TELEGRAM_BOT_TOKEN and APP_BASE_URL:
        webhook_url = f"{APP_BASE_URL}/telegram/webhook"
        payload: dict[str, Any] = {"url": webhook_url, "allowed_updates": ["message", "callback_query"]}
        if TELEGRAM_WEBHOOK_SECRET:
            payload["secret_token"] = TELEGRAM_WEBHOOK_SECRET
        try:
            httpx.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook",
                json=payload,
                timeout=10,
            )
        except Exception:
            pass


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/auth/telegram")
def telegram_auth(body: TelegramAuthIn, db: Session = Depends(get_db)) -> dict[str, Any]:
    tg = validate_telegram_init_data(body.init_data)
    telegram_id = int(tg["id"])
    user = db.scalar(select(User).where(User.telegram_id == telegram_id))
    if not user:
        user = User(
            telegram_id=telegram_id,
            username=tg.get("username"),
            first_name=tg.get("first_name"),
            role="owner" if telegram_id == OWNER_TELEGRAM_ID else "user",
        )
        db.add(user)
    else:
        user.username = tg.get("username")
        user.first_name = tg.get("first_name")
        if telegram_id == OWNER_TELEGRAM_ID:
            user.role = "owner"
    db.commit()
    db.refresh(user)
    return {"token": issue_session_token(user), "user": serialize_user(user)}


def serialize_user(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "telegram_id": user.telegram_id,
        "username": user.username,
        "first_name": user.first_name,
        "role": user.role,
        "is_admin": is_admin(user),
        "is_free": user.is_free,
        "has_access": has_access(user),
        "subscription_expires_at": user.subscription_expires_at.isoformat() if user.subscription_expires_at else None,
    }


@app.get("/api/me")
def me(user: User = Depends(get_current_user)) -> dict[str, Any]:
    return {
        "user": serialize_user(user),
        "payment_url": TRIBUTE_PAYMENT_URL,
        "subscription_price_rub": SUBSCRIPTION_PRICE_RUB,
        "support_username": SUPPORT_USERNAME,
    }


@app.post("/api/chat")
def chat(body: ChatIn, user: User = Depends(require_access), db: Session = Depends(get_db)) -> dict[str, Any]:
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "Message is empty")
    if len(text) > 8000:
        raise HTTPException(400, "Message is too long")

    db.add(Message(user_id=user.id, role="user", text=text))
    db.flush()
    answer = generate_reply(db, user, text)
    db.add(Message(user_id=user.id, role="assistant", text=answer))
    db.commit()
    return {"text": answer, "crisis": crisis_hint(text)}


@app.post("/api/voice")
async def voice(
    audio: UploadFile = File(...),
    user: User = Depends(require_access),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    data = await audio.read()
    if not data:
        raise HTTPException(400, "Audio is empty")
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(413, "Audio is too large")

    transcript = transcribe_audio(data, audio.filename or "voice.webm", audio.content_type)
    if not transcript:
        raise HTTPException(422, "Could not recognize speech")

    db.add(Message(user_id=user.id, role="user", text=transcript))
    db.flush()
    answer = generate_reply(db, user, transcript)
    db.add(Message(user_id=user.id, role="assistant", text=answer))
    db.commit()
    return {"transcript": transcript, "text": answer, "crisis": crisis_hint(transcript)}


@app.post("/api/speech")
def speech(body: ChatIn, user: User = Depends(require_access)) -> Response:
    audio = synthesize_speech(body.text)
    return Response(content=audio, media_type="audio/mpeg")


@app.get("/api/history")
def history(user: User = Depends(require_access), db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.scalars(
        select(Message).where(Message.user_id == user.id).order_by(Message.id.desc()).limit(80)
    ).all()
    rows = list(reversed(rows))
    return {
        "messages": [
            {"id": m.id, "role": m.role, "text": m.text, "created_at": m.created_at.isoformat()}
            for m in rows
        ]
    }


@app.post("/api/mood")
def mood(body: MoodIn, user: User = Depends(require_access), db: Session = Depends(get_db)) -> dict[str, bool]:
    if not 1 <= body.score <= 10:
        raise HTTPException(400, "Mood score must be from 1 to 10")
    db.add(MoodEntry(user_id=user.id, score=body.score, note=(body.note or "").strip() or None))
    db.commit()
    return {"ok": True}


@app.get("/api/mood")
def mood_history(user: User = Depends(require_access), db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.scalars(
        select(MoodEntry).where(MoodEntry.user_id == user.id).order_by(MoodEntry.id.desc()).limit(14)
    ).all()
    return {
        "entries": [
            {"score": x.score, "note": x.note, "created_at": x.created_at.isoformat()}
            for x in reversed(rows)
        ]
    }


# -----------------------------------------------------------------------------
# Tribute webhook
# -----------------------------------------------------------------------------

def find_first_value(obj: Any, keys: set[str]) -> Any:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in keys and value not in (None, ""):
                return value
        for value in obj.values():
            found = find_first_value(value, keys)
            if found not in (None, ""):
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = find_first_value(value, keys)
            if found not in (None, ""):
                return found
    return None


@app.post("/tribute/webhook")
async def tribute_webhook(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    if not TRIBUTE_API_KEY:
        raise HTTPException(503, "Tribute API key is not configured")

    body = await request.body()
    signature = request.headers.get("trbt-signature", "")
    expected = hmac.new(TRIBUTE_API_KEY.encode(), body, hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(signature.lower(), expected.lower()):
        raise HTTPException(401, "Invalid Tribute signature")

    try:
        event = json.loads(body.decode("utf-8"))
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    event_name = str(event.get("name") or event.get("event") or event.get("type") or "unknown")
    event_id = str(
        event.get("id")
        or find_first_value(event, {"event_id", "transaction_id", "subscription_id", "purchase_id"})
        or hashlib.sha256(body).hexdigest()
    )

    if db.scalar(select(PaymentEvent).where(PaymentEvent.provider_event_id == event_id)):
        return JSONResponse({"ok": True, "duplicate": True})

    db.add(PaymentEvent(provider_event_id=event_id, event_name=event_name, payload=body.decode("utf-8")))

    tg_id_raw = find_first_value(event, {"telegram_user_id", "telegram_id", "tg_user_id"})
    if tg_id_raw is not None:
        try:
            tg_id = int(tg_id_raw)
        except (TypeError, ValueError):
            tg_id = 0

        if tg_id:
            user = db.scalar(select(User).where(User.telegram_id == tg_id))
            if not user:
                user = User(telegram_id=tg_id)
                db.add(user)
                db.flush()

            positive_events = {
                "new_subscription",
                "renewed_subscription",
                "new_digital_product",
                "digital_product_purchase",
            }
            negative_events = {"cancelled_subscription", "expired_subscription", "subscription_expired"}

            if event_name in positive_events:
                now = datetime.now(timezone.utc)
                current = user.subscription_expires_at
                if current and current.tzinfo is None:
                    current = current.replace(tzinfo=timezone.utc)
                start = current if current and current > now else now
                user.subscription_expires_at = start + timedelta(days=SUBSCRIPTION_DAYS)
            elif event_name in negative_events:
                # Cancellation may mean auto-renew is disabled while paid time remains,
                # therefore only explicit expiration events revoke immediately.
                if "expired" in event_name:
                    user.subscription_expires_at = datetime.now(timezone.utc)

    db.commit()
    return JSONResponse({"ok": True})


# -----------------------------------------------------------------------------
# Telegram Bot webhook
# -----------------------------------------------------------------------------

async def telegram_api(method: str, payload: dict[str, Any]) -> None:
    if not TELEGRAM_BOT_TOKEN:
        return
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}", json=payload)


def bot_keyboard() -> dict[str, Any]:
    rows: list[list[dict[str, Any]]] = []
    if APP_BASE_URL:
        rows.append([{"text": "🦉 Открыть Совёнка", "web_app": {"url": APP_BASE_URL}}])
    if TRIBUTE_PAYMENT_URL:
        rows.append([{"text": "✨ Оформить подписку", "url": TRIBUTE_PAYMENT_URL}])
    return {"inline_keyboard": rows}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request) -> dict[str, bool]:
    if TELEGRAM_WEBHOOK_SECRET:
        supplied = request.headers.get("x-telegram-bot-api-secret-token", "")
        if not hmac.compare_digest(supplied, TELEGRAM_WEBHOOK_SECRET):
            raise HTTPException(401, "Invalid Telegram webhook secret")

    update = await request.json()
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    text = str(message.get("text") or "")
    chat_id = chat.get("id")

    if chat_id and text.startswith("/start"):
        await telegram_api(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": (
                    "Привет. Я Совёнок 🦉\n\n"
                    "Я — голосовой AI-собеседник для бережной психологической поддержки. "
                    "Можем разбирать чувства, тревогу, отношения и сложные мысли в спокойном темпе.\n\n"
                    "Важно: я не заменяю врача или психотерапевта и не ставлю диагнозы."
                ),
                "reply_markup": bot_keyboard(),
            },
        )
    elif chat_id and text.startswith("/help"):
        support = f"\nПоддержка: @{SUPPORT_USERNAME.lstrip('@')}" if SUPPORT_USERNAME else ""
        await telegram_api(
            "sendMessage",
            {"chat_id": chat_id, "text": "Открой мини‑приложение кнопкой ниже. Если возникла техническая проблема — напиши в поддержку." + support, "reply_markup": bot_keyboard()},
        )
    return {"ok": True}


# -----------------------------------------------------------------------------
# Admin API
# -----------------------------------------------------------------------------

@app.get("/api/admin/stats")
def admin_stats(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    users_count = db.scalar(select(func.count()).select_from(User)) or 0
    messages_count = db.scalar(select(func.count()).select_from(Message)) or 0
    active_count = 0
    for user in db.scalars(select(User)).all():
        if has_access(user):
            active_count += 1
    return {"users": users_count, "active_access": active_count, "messages": messages_count}


@app.get("/api/admin/users")
def admin_users(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    users = db.scalars(select(User).order_by(User.id.desc()).limit(250)).all()
    return {"users": [serialize_user(u) | {"is_blocked": u.is_blocked} for u in users]}


def get_or_create_by_tg(db: Session, telegram_id: int) -> User:
    user = db.scalar(select(User).where(User.telegram_id == telegram_id))
    if not user:
        user = User(telegram_id=telegram_id)
        db.add(user)
        db.flush()
    return user


@app.post("/api/admin/admins/add")
def admin_add(body: AdminUserIn, current: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    user = get_or_create_by_tg(db, body.telegram_id)
    if user.telegram_id == OWNER_TELEGRAM_ID:
        user.role = "owner"
    else:
        user.role = "admin"
    db.commit()
    return {"user": serialize_user(user)}


@app.post("/api/admin/admins/remove")
def admin_remove(body: AdminUserIn, current: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    if body.telegram_id == OWNER_TELEGRAM_ID:
        raise HTTPException(400, "Owner cannot be removed")
    if body.telegram_id == current.telegram_id:
        raise HTTPException(400, "You cannot remove your own admin access")
    user = get_or_create_by_tg(db, body.telegram_id)
    user.role = "user"
    db.commit()
    return {"user": serialize_user(user)}


@app.post("/api/admin/access/grant")
def admin_grant(body: AdminAccessIn, _: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    days = max(1, min(body.days, 3650))
    user = get_or_create_by_tg(db, body.telegram_id)
    now = datetime.now(timezone.utc)
    current = user.subscription_expires_at
    if current and current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    start = current if current and current > now else now
    user.subscription_expires_at = start + timedelta(days=days)
    db.commit()
    return {"user": serialize_user(user)}


@app.post("/api/admin/access/revoke")
def admin_revoke(body: AdminUserIn, _: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    user = get_or_create_by_tg(db, body.telegram_id)
    user.subscription_expires_at = datetime.now(timezone.utc)
    user.is_free = False
    db.commit()
    return {"user": serialize_user(user)}


@app.post("/api/admin/free/toggle")
def admin_free(body: AdminUserIn, _: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    user = get_or_create_by_tg(db, body.telegram_id)
    user.is_free = not user.is_free
    db.commit()
    return {"user": serialize_user(user)}


@app.post("/api/admin/block/toggle")
def admin_block(body: AdminUserIn, current: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    if body.telegram_id in {OWNER_TELEGRAM_ID, current.telegram_id}:
        raise HTTPException(400, "This account cannot be blocked here")
    user = get_or_create_by_tg(db, body.telegram_id)
    user.is_blocked = not user.is_blocked
    db.commit()
    return {"user": serialize_user(user) | {"is_blocked": user.is_blocked}}
