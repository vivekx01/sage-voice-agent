# Sage voice agent

Sage is a phone-capable voice agent. Callers reach it through a SIP trunk, LiveKit places each call in a room, and Sage listens, thinks, and talks back in real time.

This repo contains everything needed to run it on your own server: the agent, a small web control app for managing phone routing and Sage's settings, and the scripts that set up the SIP trunk.

---

## How it works

```
Phone or softphone
      │  SIP
      ▼
LiveKit SIP service ── matches the trunk, applies the dispatch rule
      │
      ▼
LiveKit room (call-…)  ◀──── Sage joins the room (agent: "sage")
                                 │
                 ┌───────────────┼────────────────┐
                 ▼               ▼                ▼
            Deepgram        OpenRouter           Fish
           (speech→text)   (LLM reply)      (text→speech)
```

- **LiveKit** routes audio between participants in rooms. It doesn't do any AI.
- **Sage** is the agent. It registers with LiveKit and joins a room when a call arrives.
- **Control app** is a web UI for trunks, dispatch rules, and Sage's settings. Sage reads its settings from it at the start of each call.
- **Providers** do the individual jobs: Deepgram for speech-to-text, OpenRouter for the LLM, Fish for text-to-speech, and Silero for detecting speech locally.

---

## Repository layout

| Path | Purpose |
|---|---|
| `sage.py` | The agent. Runs as a LiveKit worker and handles each call. |
| `setup_sip.py` | Creates and lists the SIP inbound trunk and dispatch rule through the LiveKit API. |
| `make_token.py` | Creates a join token for a browser or softphone test. |
| `Dockerfile` | Builds the Sage container for Coolify or any Docker host. |
| `control/` | The web control app (FastAPI). See `control/` for its own Dockerfile and settings. |
| `.env.example` | Every variable Sage needs, with placeholder values. |

---

## Requirements

- Python 3.13 and [uv](https://docs.astral.sh/uv/)
- A running LiveKit server (self-hosted or LiveKit Cloud)
- Accounts and API keys for Deepgram, OpenRouter, and Fish Audio
- For phone calls: a SIP trunking provider and a phone number
- Docker, if you deploy with containers

---

## Quick start (local)

1. **Install dependencies**

   ```bash
   uv sync
   ```

2. **Create your settings file**

   ```bash
   cp .env.example .env
   ```

   Fill in the LiveKit URL and key pair, plus the three provider keys. Never commit `.env`.

3. **Run Sage in development mode**

   ```bash
   uv run python sage.py dev
   ```

   Wait for a `registered worker` line for `sage`.

4. **Test your microphone** (no LiveKit needed for this step)

   ```bash
   uv run python sage.py console
   ```

---

## Setting up phone calls

1. **Create the dispatch rule**, which sends Sage to each new call:

   ```bash
   uv run python setup_sip.py dispatch
   ```

2. **Create an inbound trunk** for your phone number:

   ```bash
   uv run python setup_sip.py trunk +15551234567
   ```

   To restrict which addresses can call, set `SIP_ALLOWED_ADDRESSES` in `.env` before creating the trunk. Leave it empty only for testing.

3. **Check** what exists:

   ```bash
   uv run python setup_sip.py list
   ```

4. **Test** with a softphone, such as Linphone, dialing `sip:<number>@<your-sip-domain>`.

The SIP service itself runs separately, on your server, with a DNS record for its subdomain and ports 5060 and 10000–10100 open.

---

## Control app

The control app manages trunks, dispatch rules, and Sage's settings from a web browser. It holds the LiveKit key pair on the server, so the browser never sees it.

Run it locally:

```bash
cd control
uv sync
export CONTROL_USERNAME=admin CONTROL_PASSWORD=<password> SESSION_SECRET=<random> \
       INTERNAL_TOKEN=<random> HTTPS_ONLY=false
uv run --env-file ../.env uvicorn app:app --port 8000
```

Then open http://localhost:8000.

**Pages:**
- **Overview:** trunks and dispatch rules from the LiveKit server.
- **Trunks:** create and delete inbound trunks.
- **Dispatch rules:** create and delete rules.
- **Agent settings:** greeting, instructions, and the LLM, speech-to-text, and text-to-speech models. Changes apply to the next call.

**Internal endpoint:** `GET /api/internal/settings` returns Sage's settings. It requires `Authorization: Bearer <INTERNAL_TOKEN>`.

---

## Deploying on Coolify

**Sage**
1. Create a resource from this repo, using the **Dockerfile** build pack and the `/` base directory.
2. Leave the domain and port mappings empty. Sage only makes outgoing connections.
3. Set the environment variables from `.env.example`. Add `SETTINGS_URL` and `INTERNAL_TOKEN` if you use the control app.

**Control app**
1. Create a resource from this repo, using the **Dockerfile** build pack and the `/control` base directory.
2. Set a domain such as `control.<your-domain>` with HTTPS, and set the port to `8000`.
3. Add a persistent volume mounted at `/data`.
4. Set the variables from `control/.env.example`. Use a long random value for `SESSION_SECRET` and `INTERNAL_TOKEN`.

**LiveKit and SIP** are deployed as separate Coolify resources, using the `livekit/livekit-server` and `livekit/sip` images. They aren't part of this repo.

---

## Security

- **Never commit secrets.** `.env`, token files, and the control app's database are ignored by `.gitignore`. Keep them out of git and out of chats.
- **Rotate any key or password** that has been shared outside a password manager.
- **Restrict SIP access.** Set `SIP_ALLOWED_ADDRESSES` to your provider's addresses before using a real phone number. Otherwise anyone who knows the number can reach Sage.
- **Use a strong login** for the control app, and keep it on HTTPS.

---

## Costs

Each call is billed by the providers it uses: the phone carrier, Deepgram, OpenRouter, and Fish. The LiveKit server and Sage's compute are on your own server. Check each provider's pricing page for current rates.

---

## Troubleshooting

| Symptom | Check |
|---|---|
| Sage doesn't register | The `LIVEKIT_URL` and key pair match the LiveKit server |
| Calls connect but Sage doesn't answer | A dispatch rule exists (`setup_sip.py list`), and Sage is running |
| Sage ignores settings changes | `SETTINGS_URL` and `INTERNAL_TOKEN` match the control app. Without them Sage uses its built-in defaults. |
| Control app shows "could not reach the LiveKit server" | `LIVEKIT_URL` and the key pair in the control app are correct |
| `WRONGPASS` errors on the LiveKit server or SIP service | The Redis password in those services' settings is current |

---

## Status

This is a prototype. It works end to end with a softphone test. Before using a real public number, restrict SIP access, rotate shared credentials, and confirm the provider's trunk setup.
