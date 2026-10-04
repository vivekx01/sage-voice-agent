"""Create LiveKit SIP inbound trunk and dispatch rule via the server API.

Usage:
    uv run python setup_sip.py list
    uv run python setup_sip.py dispatch
    uv run python setup_sip.py dispatch --trunk <trunk-id>
    uv run python setup_sip.py trunk +1XXXXXXXXXX

Reads LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET from .env.
Optional: SIP_ALLOWED_ADDRESSES (comma-separated IPs or CIDRs) in .env.
"""

import asyncio
import os
import sys

from dotenv import load_dotenv
from livekit import api

load_dotenv(".env")

AGENT_NAME = "sage"
ROOM_PREFIX = "call-"
TRUNK_NAME = "sage-inbound"
DISPATCH_NAME = "sage-calls"


def client() -> api.LiveKitAPI:
    return api.LiveKitAPI(
        url=os.environ["LIVEKIT_URL"],
        api_key=os.environ["LIVEKIT_API_KEY"],
        api_secret=os.environ["LIVEKIT_API_SECRET"],
    )


async def create_dispatch_rule(lkapi: api.LiveKitAPI, trunk_ids: list[str]) -> None:
    rule = await lkapi.sip.create_dispatch_rule(
        api.CreateSIPDispatchRuleRequest(
            name=DISPATCH_NAME,
            trunk_ids=trunk_ids,
            rule=api.SIPDispatchRule(
                dispatch_rule_individual=api.SIPDispatchRuleIndividual(room_prefix=ROOM_PREFIX),
            ),
            room_config=api.RoomConfiguration(
                agents=[api.RoomAgentDispatch(agent_name=AGENT_NAME)],
            ),
        )
    )
    scope = ", ".join(trunk_ids) if trunk_ids else "all trunks"
    print(f"Created dispatch rule: {rule.sip_dispatch_rule_id} ({rule.name}) for {scope}")


async def create_inbound_trunk(lkapi: api.LiveKitAPI, number: str) -> None:
    allowed = [a.strip() for a in os.environ.get("SIP_ALLOWED_ADDRESSES", "").split(",") if a.strip()]
    trunk = await lkapi.sip.create_inbound_trunk(
        api.CreateSIPInboundTrunkRequest(
            trunk=api.SIPInboundTrunkInfo(
                name=TRUNK_NAME,
                numbers=[number],
                allowed_addresses=allowed,
            )
        )
    )
    print(f"Created inbound trunk: {trunk.sip_trunk_id} ({trunk.name}) for {number}")
    if allowed:
        print(f"Allowed addresses: {', '.join(allowed)}")
    else:
        print("Warning: no SIP_ALLOWED_ADDRESSES set. Any caller can reach this trunk.")


async def list_all(lkapi: api.LiveKitAPI) -> None:
    trunks = await lkapi.sip.list_inbound_trunk(api.ListSIPInboundTrunkRequest())
    print("Inbound trunks:")
    for t in trunks.items:
        print(f"  {t.sip_trunk_id}  {t.name}  numbers={list(t.numbers)}")
    rules = await lkapi.sip.list_dispatch_rule(api.ListSIPDispatchRuleRequest())
    print("Dispatch rules:")
    for r in rules.items:
        scope = ", ".join(r.trunk_ids) if r.trunk_ids else "all trunks"
        print(f"  {r.sip_dispatch_rule_id}  {r.name}  agents={[a.agent_name for a in r.room_config.agents]}  trunks={scope}")


async def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        return

    command = sys.argv[1]
    async with client() as lkapi:
        if command == "list":
            await list_all(lkapi)
        elif command == "dispatch":
            trunk_ids = []
            if "--trunk" in sys.argv:
                i = sys.argv.index("--trunk")
                if i + 1 >= len(sys.argv):
                    print("Usage: setup_sip.py dispatch [--trunk <trunk-id>]")
                    return
                trunk_ids = [sys.argv[i + 1]]
            await create_dispatch_rule(lkapi, trunk_ids)
        elif command == "trunk":
            if len(sys.argv) < 3:
                print("Usage: setup_sip.py trunk +1XXXXXXXXXX")
                return
            await create_inbound_trunk(lkapi, sys.argv[2])
        else:
            print(f"Unknown command: {command}")
            print(__doc__)


if __name__ == "__main__":
    asyncio.run(main())
