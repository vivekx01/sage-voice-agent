"""Sage control app: a small web UI for the LiveKit SIP setup and Sage's settings.

Runs behind Coolify's HTTPS proxy. Holds the LiveKit key pair on the server side,
so the browser never sees it. Settings are stored in SQLite and read by Sage
through an internal endpoint, authenticated with a separate token.
"""

import os
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from livekit import api
from starlette.middleware.sessions import SessionMiddleware

load_dotenv()

HERE = Path(__file__).parent
DB_PATH = Path(os.environ.get("DB_PATH", HERE / "data" / "control.db"))
ROOM_PREFIX = "call-"
AGENT_NAME = "sage"

DEFAULT_SETTINGS = {
    "greeting": "Greet the user briefly and ask how you can help.",
    "instructions": "You are Sage, a friendly voice assistant. Keep replies short and conversational. Avoid lists, markdown, and emojis.",
    "llm_model": "openai/gpt-4o-mini",
    "tts_model": "s2.1-pro-free",
    "tts_voice_id": "933563129e564b19a115bedd57b7406a",
    "stt_model": "flux-general-en",
}

app = FastAPI(title="Sage control")
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ["SESSION_SECRET"],
    same_site="lax",
    https_only=os.environ.get("HTTPS_ONLY", "true").lower() == "true",
)
templates = Jinja2Templates(directory=HERE / "templates")


# ---------- database ----------

def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def load_settings() -> dict:
    with db() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    settings = dict(DEFAULT_SETTINGS)
    settings.update({k: v for k, v in rows})
    return settings


init_db()


# ---------- auth ----------

def require_login(request: Request) -> None:
    if not request.session.get("user"):
        raise HTTPException(status_code=303, headers={"Location": "/login"})


def check_password(username: str, password: str) -> bool:
    expected_user = os.environ["CONTROL_USERNAME"]
    expected_pass = os.environ["CONTROL_PASSWORD"]
    return secrets.compare_digest(username, expected_user) and secrets.compare_digest(password, expected_pass)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if check_password(username, password):
        request.session["user"] = username
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": "Wrong username or password."}, status_code=401)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---------- LiveKit helpers ----------

def lk_client() -> api.LiveKitAPI:
    return api.LiveKitAPI(
        url=os.environ["LIVEKIT_URL"],
        api_key=os.environ["LIVEKIT_API_KEY"],
        api_secret=os.environ["LIVEKIT_API_SECRET"],
    )


async def fetch_trunks() -> list[dict]:
    async with lk_client() as lk:
        res = await lk.sip.list_inbound_trunk(api.ListSIPInboundTrunkRequest())
    return [
        {"id": t.sip_trunk_id, "name": t.name, "numbers": list(t.numbers), "allowed": list(t.allowed_addresses)}
        for t in res.items
    ]


async def fetch_rules() -> list[dict]:
    async with lk_client() as lk:
        res = await lk.sip.list_dispatch_rule(api.ListSIPDispatchRuleRequest())
    return [
        {"id": r.sip_dispatch_rule_id, "name": r.name, "agents": [a.agent_name for a in r.room_config.agents]}
        for r in res.items
    ]


def friendly_error(exc: Exception) -> str:
    """Return LiveKit's own message when there is one, otherwise the exception text."""
    message = getattr(exc, "message", None) or str(exc)
    return message or exc.__class__.__name__


async def safe_fetch(fetch) -> tuple[list[dict], str | None]:
    """Run a list call. On failure return an empty list and the message, instead of a 500 page."""
    try:
        return await fetch(), None
    except Exception as exc:
        return [], friendly_error(exc)


async def lk_action(request: Request, back: str, action, success: str) -> RedirectResponse:
    """Run a LiveKit write. On success or failure, flash a message and go back to the page."""
    try:
        await action()
        request.session["flash"] = {"kind": "ok", "text": success}
    except Exception as exc:
        request.session["flash"] = {"kind": "error", "text": friendly_error(exc)}
    return RedirectResponse(back, status_code=303)


def take_flash(request: Request) -> dict | None:
    """Read and clear the one-time message shown at the top of a page."""
    return request.session.pop("flash", None)


templates.env.globals["take_flash"] = take_flash


# ---------- pages ----------

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    require_login(request)
    try:
        trunks = await fetch_trunks()
        rules = await fetch_rules()
        error = None
    except Exception as exc:  # show the failure instead of a blank page
        trunks, rules, error = [], [], str(exc)
    return templates.TemplateResponse(
        request,
        "home.html",
        {"trunks": trunks, "rules": rules, "error": error, "user": request.session["user"]},
    )


@app.get("/trunks", response_class=HTMLResponse)
async def trunks_page(request: Request):
    require_login(request)
    trunks, error = await safe_fetch(fetch_trunks)
    return templates.TemplateResponse(
        request, "trunks.html", {"trunks": trunks, "error": error, "user": request.session["user"]}
    )


@app.post("/trunks")
async def create_trunk(request: Request, name: str = Form(...), number: str = Form(...), allowed: str = Form("")):
    require_login(request)
    number = number.strip()
    allowed_list = [a.strip() for a in allowed.split(",") if a.strip()]

    async def action():
        async with lk_client() as lk:
            await lk.sip.create_inbound_trunk(
                api.CreateSIPInboundTrunkRequest(
                    trunk=api.SIPInboundTrunkInfo(name=name.strip(), numbers=[number], allowed_addresses=allowed_list)
                )
            )

    return await lk_action(request, "/trunks", action, f"Trunk created for {number}.")


@app.post("/trunks/{trunk_id}/delete")
async def delete_trunk(request: Request, trunk_id: str):
    require_login(request)

    async def action():
        async with lk_client() as lk:
            await lk.sip.delete_trunk(api.DeleteSIPTrunkRequest(sip_trunk_id=trunk_id))

    return await lk_action(request, "/trunks", action, "Trunk deleted.")


@app.get("/rules", response_class=HTMLResponse)
async def rules_page(request: Request):
    require_login(request)
    rules, error = await safe_fetch(fetch_rules)
    return templates.TemplateResponse(
        request, "rules.html", {"rules": rules, "error": error, "user": request.session["user"]}
    )


@app.post("/rules")
async def create_rule(request: Request, name: str = Form(...), room_prefix: str = Form(ROOM_PREFIX)):
    require_login(request)
    prefix = room_prefix.strip() or ROOM_PREFIX

    async def action():
        async with lk_client() as lk:
            await lk.sip.create_dispatch_rule(
                api.CreateSIPDispatchRuleRequest(
                    name=name.strip(),
                    rule=api.SIPDispatchRule(
                        dispatch_rule_individual=api.SIPDispatchRuleIndividual(room_prefix=prefix),
                    ),
                    room_config=api.RoomConfiguration(agents=[api.RoomAgentDispatch(agent_name=AGENT_NAME)]),
                )
            )

    return await lk_action(request, "/rules", action, "Dispatch rule created.")


@app.post("/rules/{rule_id}/delete")
async def delete_rule(request: Request, rule_id: str):
    require_login(request)

    async def action():
        async with lk_client() as lk:
            await lk.sip.delete_dispatch_rule(api.DeleteSIPDispatchRuleRequest(sip_dispatch_rule_id=rule_id))

    return await lk_action(request, "/rules", action, "Dispatch rule deleted.")


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    require_login(request)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {"settings": load_settings(), "defaults": DEFAULT_SETTINGS, "user": request.session["user"], "saved": request.query_params.get("saved")},
    )


@app.post("/settings")
def save_settings(
    request: Request,
    greeting: str = Form(...),
    instructions: str = Form(...),
    llm_model: str = Form(...),
    tts_model: str = Form(...),
    tts_voice_id: str = Form(...),
    stt_model: str = Form(...),
):
    require_login(request)
    values = {
        "greeting": greeting.strip(),
        "instructions": instructions.strip(),
        "llm_model": llm_model.strip(),
        "tts_model": tts_model.strip(),
        "tts_voice_id": tts_voice_id.strip(),
        "stt_model": stt_model.strip(),
    }
    with db() as conn:
        for key, value in values.items():
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    return RedirectResponse("/settings?saved=1", status_code=303)


# ---------- internal API for Sage ----------

@app.get("/api/internal/settings")
def internal_settings(request: Request):
    expected = os.environ.get("INTERNAL_TOKEN", "")
    header = request.headers.get("authorization", "")
    token = header.removeprefix("Bearer ").strip()
    if not expected or not secrets.compare_digest(token, expected):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return load_settings()
