import datetime
import os
import sys

from dotenv import load_dotenv
from livekit import api

load_dotenv(".env")

room_name = sys.argv[1] if len(sys.argv) > 1 else "sage-test"
identity = sys.argv[2] if len(sys.argv) > 2 else "tester"

token = (
    api.AccessToken(os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
    .with_identity(identity)
    .with_name(identity)
    .with_grants(api.VideoGrants(room_join=True, room=room_name, can_publish=True, can_subscribe=True))
    .with_room_config(
        api.RoomConfiguration(agents=[api.RoomAgentDispatch(agent_name="sage")])
    )
    .with_ttl(datetime.timedelta(hours=2))
    .to_jwt()
)

print(f"Room: {room_name}")
print(f"Server: {os.environ['LIVEKIT_URL']}")
print(f"Token:\n{token}")
