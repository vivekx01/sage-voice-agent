import asyncio
import json
import logging
import os
import urllib.request

from dotenv import load_dotenv
from livekit import agents
from livekit.agents import Agent, AgentServer, AgentSession, TurnHandlingOptions
from livekit.plugins import deepgram, fishaudio, openai, silero

load_dotenv(".env")

logger = logging.getLogger("sage")

# Used when the control app is not configured or can't be reached.
DEFAULT_SETTINGS = {
    "greeting": "Greet the user briefly and ask how you can help.",
    "instructions": (
        "You are Sage, a friendly voice assistant. "
        "Keep replies short and conversational, since they will be spoken aloud. "
        "Avoid lists, markdown, and emojis."
    ),
    "llm_model": "openai/gpt-4o-mini",
    "tts_model": "s2.1-pro-free",
    "tts_voice_id": "933563129e564b19a115bedd57b7406a",
    "stt_model": "flux-general-en",
}


AGENT_NAME = "sage"


def fetch_settings() -> dict:
    """Read this agent's settings from the control panel. Falls back to defaults on any failure."""
    base = os.environ.get("CONTROL_URL", "").rstrip("/")
    token = os.environ.get("INTERNAL_TOKEN")
    if not base or not token:
        logger.info("control panel not configured, using default settings")
        return dict(DEFAULT_SETTINGS)
    url = f"{base}/api/internal/agents/{AGENT_NAME}/settings"
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            remote = json.loads(resp.read().decode())
        return {**DEFAULT_SETTINGS, **remote}
    except Exception as exc:
        logger.warning("could not load settings from control app, using defaults: %s", exc)
        return dict(DEFAULT_SETTINGS)


class Sage(Agent):
    def __init__(self, instructions: str) -> None:
        super().__init__(instructions=instructions)


server = AgentServer()


@server.rtc_session(agent_name=AGENT_NAME)
async def sage_session(ctx: agents.JobContext):
    # Load settings per call, so changes in the control app apply to the next call.
    settings = await asyncio.to_thread(fetch_settings)

    session = AgentSession(
        stt=deepgram.STTv2(model=settings["stt_model"], eager_eot_threshold=0.4),
        llm=openai.LLM.with_openrouter(model=settings["llm_model"]),
        tts=fishaudio.TTS(
            model=settings["tts_model"],
            voice_id=settings["tts_voice_id"],
        ),
        vad=silero.VAD.load(),
        turn_handling=TurnHandlingOptions(turn_detection="stt"),
    )

    await session.start(room=ctx.room, agent=Sage(settings["instructions"]))

    await session.generate_reply(instructions=settings["greeting"])


if __name__ == "__main__":
    agents.cli.run_app(server)
