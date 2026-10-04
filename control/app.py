"""Sage control app: a small web UI for the LiveKit SIP setup and Sage's settings.

Runs behind Coolify's HTTPS proxy. Holds the LiveKit key pair on the server side,
so the browser never sees it. Settings are stored in SQLite and read by Sage
through an internal endpoint, authenticated with a separate token.
"""

import logging
import os
import re
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

logger = logging.getLogger("control")

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

AGENT_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def init_db() -> None:
    """Create the per-agent settings table. Copies the old single-agent settings to 'sage' once."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS agent_settings ("
            " agent TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY (agent, key))"
        )
        legacy = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='settings'").fetchone()
        has_rows = conn.execute("SELECT 1 FROM agent_settings LIMIT 1").fetchone()
        if legacy and not has_rows:
            conn.execute("INSERT OR IGNORE INTO agent_settings (agent, key, value) SELECT 'sage', key, value FROM settings")


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def valid_agent_name(name: str) -> bool:
    return bool(AGENT_NAME_PATTERN.match(name))


def agent_exists(agent: str) -> bool:
    with db() as conn:
        return conn.execute("SELECT 1 FROM agent_settings WHERE agent = ? LIMIT 1", (agent,)).fetchone() is not None


def list_agents() -> list[str]:
    with db() as conn:
        rows = conn.execute("SELECT DISTINCT agent FROM agent_settings ORDER BY agent").fetchall()
    return [r[0] for r in rows]


def load_settings(agent: str) -> dict:
    """Defaults, overridden by whatever is saved for this agent."""
    with db() as conn:
        rows = conn.execute("SELECT key, value FROM agent_settings WHERE agent = ?", (agent,)).fetchall()
    settings = dict(DEFAULT_SETTINGS)
    settings.update({k: v for k, v in rows})
    return settings


def save_settings_for(agent: str, values: dict) -> None:
    with db() as conn:
        for key, value in values.items():
            conn.execute(
                "INSERT OR REPLACE INTO agent_settings (agent, key, value) VALUES (?, ?, ?)",
                (agent, key, value),
            )


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
        {
            "id": r.sip_dispatch_rule_id,
            "name": r.name,
            "agents": [a.agent_name for a in r.room_config.agents],
            "trunk_ids": list(r.trunk_ids),
            "prefix": r.rule.dispatch_rule_individual.room_prefix if r.rule.HasField("dispatch_rule_individual") else "",
        }
        for r in res.items
    ]


async def fetch_trunk(trunk_id: str) -> dict | None:
    trunks = await fetch_trunks()
    return next((t for t in trunks if t["id"] == trunk_id), None)


async def fetch_rule(rule_id: str) -> dict | None:
    rules = await fetch_rules()
    return next((r for r in rules if r["id"] == rule_id), None)


def describe_trunks(rule: dict, trunks: list[dict]) -> str:
    """Human-readable list of the trunks a rule applies to."""
    if not rule["trunk_ids"]:
        return "All trunks"
    names = {t["id"]: f"{t['name']} ({', '.join(t['numbers'])})" for t in trunks}
    return ", ".join(names.get(tid, tid) for tid in rule["trunk_ids"])


def friendly_error(exc: Exception) -> str:
    """Return LiveKit's message, with a plain explanation for the errors people hit most."""
    message = getattr(exc, "message", None) or str(exc)
    if "already exists" in message and "dispatch rule" in message:
        return (
            "Another dispatch rule already covers this trunk, number, and PIN. "
            "Only one rule can apply to all trunks, and a rule can't use the same trunk as another. "
            "Change the trunks on this rule, or delete the existing one first. "
            f"LiveKit said: {message}"
        )
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


def list_update(current: list[str], new: list[str]) -> api.ListUpdate | None:
    """Change a list field from current to new, sending only what LiveKit accepts.

    Returns None when nothing changed, so the field is left alone. LiveKit rejects
    an empty update, and 'set' with an empty list. Emptying a list is done with 'remove'.
    """
    add = [v for v in new if v not in current]
    remove = [v for v in current if v not in new]
    if not add and not remove:
        return None
    if add and not remove:
        return api.ListUpdate(add=add)
    if remove and not add:
        return api.ListUpdate(remove=remove)
    # Both adding and removing: the server accepts one operation per update, so replace the whole list
    return api.ListUpdate(set=new)


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


@app.get("/trunks/{trunk_id}/edit", response_class=HTMLResponse)
async def trunk_edit_page(request: Request, trunk_id: str):
    require_login(request)
    trunk = await fetch_trunk(trunk_id)
    if trunk is None:
        request.session["flash"] = {"kind": "error", "text": "That trunk no longer exists."}
        return RedirectResponse("/trunks", status_code=303)
    return templates.TemplateResponse(request, "trunk_edit.html", {"trunk": trunk, "user": request.session["user"]})


@app.post("/trunks/{trunk_id}/edit")
async def update_trunk(
    request: Request,
    trunk_id: str,
    name: str = Form(...),
    number: str = Form(...),
    allowed: str = Form(""),
):
    require_login(request)
    number = number.strip()
    allowed_list = [a.strip() for a in allowed.split(",") if a.strip()]
    existing = await fetch_trunk(trunk_id)
    current_numbers = existing["numbers"] if existing else []
    current_allowed = existing["allowed"] if existing else []

    async def action():
        async with lk_client() as lk:
            await lk.sip.update_inbound_trunk_fields(
                trunk_id,
                name=name.strip(),
                numbers=list_update(current_numbers, [number]),
                allowed_addresses=list_update(current_allowed, allowed_list),
            )

    return await lk_action(request, "/trunks", action, "Trunk updated. The change applies to new calls.")


async def rules_context(request: Request) -> dict:
    trunks, trunk_error = await safe_fetch(fetch_trunks)
    rules, rule_error = await safe_fetch(fetch_rules)
    # Rules with no trunk apply to every trunk, so they overlap with every other rule.
    overlap = len(rules) > 1 and any(not r["trunk_ids"] for r in rules)
    return {
        "rules": rules,
        "trunks": trunks,
        "error": rule_error or trunk_error,
        "overlap_warning": overlap,
        "default_agent": AGENT_NAME,
        "user": request.session["user"],
    }


@app.get("/rules", response_class=HTMLResponse)
async def rules_page(request: Request):
    require_login(request)
    context = await rules_context(request)
    context["describe"] = lambda rule: describe_trunks(rule, context["trunks"])
    return templates.TemplateResponse(request, "rules.html", context)


@app.get("/rules/{rule_id}/edit", response_class=HTMLResponse)
async def rule_edit_page(request: Request, rule_id: str):
    require_login(request)
    rule = await fetch_rule(rule_id)
    trunks, error = await safe_fetch(fetch_trunks)
    if rule is None:
        request.session["flash"] = {"kind": "error", "text": "That rule no longer exists."}
        return RedirectResponse("/rules", status_code=303)
    return templates.TemplateResponse(
        request, "rule_edit.html", {"rule": rule, "trunks": trunks, "error": error, "user": request.session["user"]}
    )


def parse_agents(raw: str) -> list[str]:
    """Turn 'sage, sage2' into ['sage', 'sage2'], dropping blanks and duplicates."""
    names: list[str] = []
    for part in raw.split(","):
        name = part.strip()
        if name and name not in names:
            names.append(name)
    return names


@app.post("/rules")
async def create_rule(
    request: Request,
    name: str = Form(...),
    room_prefix: str = Form(ROOM_PREFIX),
    agents: str = Form(AGENT_NAME),
    trunk_ids: list[str] = Form([]),
):
    require_login(request)
    prefix = room_prefix.strip() or ROOM_PREFIX
    selected = [t for t in trunk_ids if t]
    agent_names = parse_agents(agents)
    if not agent_names:
        request.session["flash"] = {"kind": "error", "text": "Enter at least one agent name, such as sage."}
        return RedirectResponse("/rules", status_code=303)

    async def action():
        async with lk_client() as lk:
            await lk.sip.create_dispatch_rule(
                api.CreateSIPDispatchRuleRequest(
                    name=name.strip(),
                    trunk_ids=selected,
                    rule=api.SIPDispatchRule(
                        dispatch_rule_individual=api.SIPDispatchRuleIndividual(room_prefix=prefix),
                    ),
                    room_config=api.RoomConfiguration(
                        agents=[api.RoomAgentDispatch(agent_name=a) for a in agent_names],
                    ),
                )
            )

    scope = f"{len(selected)} trunk(s)" if selected else "all trunks"
    return await lk_action(
        request, "/rules", action, f"Dispatch rule created for {scope}, dispatching {', '.join(agent_names)}."
    )


@app.post("/rules/{rule_id}/edit")
async def update_rule(
    request: Request,
    rule_id: str,
    name: str = Form(...),
    room_prefix: str = Form(ROOM_PREFIX),
    agents: str = Form(AGENT_NAME),
    trunk_ids: list[str] = Form([]),
):
    require_login(request)
    prefix = room_prefix.strip() or ROOM_PREFIX
    selected = [t for t in trunk_ids if t]
    agent_names = parse_agents(agents)
    if not agent_names:
        request.session["flash"] = {"kind": "error", "text": "Enter at least one agent name, such as sage."}
        return RedirectResponse("/rules", status_code=303)

    async def action():
        async with lk_client() as lk:
            # Replace the whole rule. LiveKit doesn't let us change the agent list on its own,
            # so read the current rule, change the parts we need, and write it back.
            items = (await lk.sip.list_dispatch_rule(api.ListSIPDispatchRuleRequest())).items
            raw = next((x for x in items if x.sip_dispatch_rule_id == rule_id), None)
            if raw is None:
                raise ValueError("That rule no longer exists.")
            raw.name = name.strip()
            raw.rule.CopyFrom(
                api.SIPDispatchRule(dispatch_rule_individual=api.SIPDispatchRuleIndividual(room_prefix=prefix))
            )
            del raw.trunk_ids[:]
            raw.trunk_ids.extend(selected)
            del raw.room_config.agents[:]
            for agent_name in agent_names:
                raw.room_config.agents.add(agent_name=agent_name)
            await lk.sip.update_dispatch_rule(rule_id, raw)

    return await lk_action(
        request, "/rules", action, f"Dispatch rule updated. Agents: {', '.join(agent_names)}."
    )


@app.post("/rules/{rule_id}/delete")
async def delete_rule(request: Request, rule_id: str):
    require_login(request)

    async def action():
        async with lk_client() as lk:
            await lk.sip.delete_dispatch_rule(api.DeleteSIPDispatchRuleRequest(sip_dispatch_rule_id=rule_id))

    return await lk_action(request, "/rules", action, "Dispatch rule deleted.")


@app.get("/agents", response_class=HTMLResponse)
def agents_page(request: Request):
    require_login(request)
    return templates.TemplateResponse(
        request,
        "agents.html",
        {"agents": list_agents(), "user": request.session["user"]},
    )


@app.post("/agents")
def create_agent(request: Request, name: str = Form(...)):
    require_login(request)
    name = name.strip()
    if not valid_agent_name(name):
        request.session["flash"] = {
            "kind": "error",
            "text": "Agent names use lowercase letters, numbers, - and _, and must start with a letter or number.",
        }
        return RedirectResponse("/agents", status_code=303)
    if not agent_exists(name):
        save_settings_for(name, dict(DEFAULT_SETTINGS))
    return RedirectResponse(f"/agents/{name}/settings", status_code=303)


@app.post("/agents/{agent}/delete")
def delete_agent(request: Request, agent: str):
    require_login(request)
    with db() as conn:
        conn.execute("DELETE FROM agent_settings WHERE agent = ?", (agent,))
    request.session["flash"] = {
        "kind": "ok",
        "text": f"Settings for {agent} deleted. Rules that dispatch this agent are not changed.",
    }
    return RedirectResponse("/agents", status_code=303)


@app.get("/agents/{agent}/settings", response_class=HTMLResponse)
def agent_settings_page(request: Request, agent: str):
    require_login(request)
    if not agent_exists(agent):
        request.session["flash"] = {"kind": "error", "text": f"No settings found for {agent}. Create it first."}
        return RedirectResponse("/agents", status_code=303)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "agent": agent,
            "settings": load_settings(agent),
            "defaults": DEFAULT_SETTINGS,
            "user": request.session["user"],
        },
    )


@app.post("/agents/{agent}/settings")
def save_agent_settings(
    request: Request,
    agent: str,
    greeting: str = Form(...),
    instructions: str = Form(...),
    llm_model: str = Form(...),
    tts_model: str = Form(...),
    tts_voice_id: str = Form(...),
    stt_model: str = Form(...),
):
    require_login(request)
    if not valid_agent_name(agent):
        raise HTTPException(status_code=404)
    values = {
        "greeting": greeting.strip(),
        "instructions": instructions.strip(),
        "llm_model": llm_model.strip(),
        "tts_model": tts_model.strip(),
        "tts_voice_id": tts_voice_id.strip(),
        "stt_model": stt_model.strip(),
    }
    save_settings_for(agent, values)
    request.session["flash"] = {"kind": "ok", "text": f"Settings saved for {agent}. New calls use them."}
    return RedirectResponse(f"/agents/{agent}/settings", status_code=303)


# ---------- internal API for agents ----------

def check_internal_token(request: Request) -> JSONResponse | None:
    expected = os.environ.get("INTERNAL_TOKEN", "")
    header = request.headers.get("authorization", "")
    token = header.removeprefix("Bearer ").strip()
    if not expected or not secrets.compare_digest(token, expected):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return None


@app.get("/api/internal/agents/{agent}/settings")
def internal_agent_settings(request: Request, agent: str):
    denied = check_internal_token(request)
    if denied:
        return denied
    if not agent_exists(agent):
        # Don't break calls for an agent that has no saved settings yet. Use defaults and say so in the log.
        logger.warning("no saved settings for agent %r, serving defaults", agent)
    return load_settings(agent)


@app.get("/api/internal/settings")
def internal_settings_legacy(request: Request):
    """Kept so the original Sage deployment keeps working. Same as asking for the 'sage' agent."""
    denied = check_internal_token(request)
    if denied:
        return denied
    return load_settings("sage")
