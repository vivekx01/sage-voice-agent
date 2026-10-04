from dotenv import load_dotenv
from livekit import agents
from livekit.agents import Agent, AgentServer, AgentSession, TurnHandlingOptions
from livekit.plugins import deepgram, fishaudio, openai, silero

load_dotenv(".env")

INSTRUCTIONS = (
    "You are Sage, a friendly voice assistant. "
    "Keep replies short and conversational, since they will be spoken aloud. "
    "Avoid lists, markdown, and emojis."
)


class Sage(Agent):
    def __init__(self) -> None:
        super().__init__(instructions=INSTRUCTIONS)


server = AgentServer()


@server.rtc_session(agent_name="sage")
async def sage_session(ctx: agents.JobContext):
    session = AgentSession(
        stt=deepgram.STTv2(model="flux-general-en", eager_eot_threshold=0.4),
        llm=openai.LLM.with_openrouter(model="openai/gpt-4o-mini"),
        tts=fishaudio.TTS(
            model="s2.1-pro-free",
            voice_id="933563129e564b19a115bedd57b7406a",
        ),
        vad=silero.VAD.load(),
        turn_handling=TurnHandlingOptions(turn_detection="stt"),
    )

    await session.start(room=ctx.room, agent=Sage())

    await session.generate_reply(instructions="Greet the user briefly and ask how you can help.")


if __name__ == "__main__":
    agents.cli.run_app(server)
