"""Connect the two agents to the phone network.

The helpline gets a phone number; the agent gets a way to dial out.

    # a LiveKit phone number for the mock helpline (inbound only; LiveKit bills it to the project)
    python scripts/setup_telephony.py helpline --livekit-number [--area-code 415]

    # or your own carrier's number, if it sends calls to this project's SIP URI
    python scripts/setup_telephony.py helpline --number +15550000001

    # an outbound trunk through any SIP carrier (Twilio Elastic SIP Trunking, Telnyx, ...)
    python scripts/setup_telephony.py outbound --address example.pstn.twilio.com \\
        --number +15550000002 --username USER --password PASS

    # or no carrier at all: the agent calls the helpline over SIP, straight into this project
    python scripts/setup_telephony.py sip-only --sip-uri abc123.sip.livekit.cloud

Each step prints what it made. Put the outbound trunk id in .env as SIP_OUTBOUND_TRUNK_ID.
LiveKit Phone Numbers only take calls in (docs.livekit.io/telephony/start/phone-numbers), so the agent's own
calls go through a carrier's trunk, or, with sip-only, through a trunk that points back at this project.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
from pathlib import Path

import aiohttp
from livekit import api
from livekit.protocol import sip

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from claimcall import config  # noqa: E402


async def livekit_numbers(method: str, body: dict) -> dict:
    """LiveKit's phone number service (Twirp), signed with the project's key."""
    base = os.environ["LIVEKIT_URL"].replace("wss://", "https://")
    token = api.AccessToken().with_sip_grants(api.SIPGrants(admin=True)).to_jwt()
    async with (
        aiohttp.ClientSession() as http,
        http.post(
            f"{base}/twirp/livekit.PhoneNumberService/{method}", json=body, headers={"Authorization": f"Bearer {token}"}
        ) as resp,
    ):
        if resp.status != 200:
            raise SystemExit(f"LiveKit refused {method}: {resp.status} {(await resp.text())[:200]}")
        return await resp.json()


async def dispatch_rule(lk: api.LiveKitAPI, trunk_id: str, label: str, variant: str | None = None) -> str:
    """Calls to the helpline number start a room with the mock insurer in it, one room per call. A LiveKit number
    is its own trunk, so the rule is limited to it by its id and never sees calls meant for anything else in the
    project. (Setting the rule at purchase, or on the number afterwards, is refused; this is how the project's
    other numbers are wired.) A variant tells the helpline which menu to play, as --variant does in a loopback
    call."""
    agent = api.RoomAgentDispatch(
        agent_name=config.HELPLINE_AGENT, metadata=json.dumps({"variant": variant}) if variant else ""
    )
    rule = sip.SIPDispatchRuleInfo(
        name=f"{config.HELPLINE_AGENT}-{label}",
        rule=sip.SIPDispatchRule(dispatch_rule_individual=sip.SIPDispatchRuleIndividual(room_prefix="helpline-")),
        trunk_ids=[trunk_id],
        room_config=api.RoomConfiguration(agents=[agent]),
    )
    made = await lk.sip.create_dispatch_rule(api.CreateSIPDispatchRuleRequest(dispatch_rule=rule))
    return made.sip_dispatch_rule_id


async def helpline(args: argparse.Namespace) -> None:
    lk = api.LiveKitAPI()
    try:
        if args.livekit_number:
            body = {"country_code": "US", "limit": 5, **({"area_code": args.area_code} if args.area_code else {})}
            items = (await livekit_numbers("SearchPhoneNumbers", body)).get("items") or []
            bought = None
            for item in items:  # a number can be taken between the search and the purchase
                number = item.get("e164_format") or item.get("e164Format")
                try:
                    out = await livekit_numbers("PurchasePhoneNumber", {"phone_numbers": [number]})
                except SystemExit as exc:
                    print(f"could not buy {number}: {exc}")
                    continue
                bought = (number, (out.get("phone_numbers") or out.get("phoneNumbers"))[0]["id"])
                break
            if bought is None:
                raise SystemExit("no LiveKit number could be bought; try another area code")
            number, number_id = bought
            rule_id = await dispatch_rule(lk, number_id, number)
            print(f"helpline number: {number} (LiveKit {number_id}, dispatch rule {rule_id})")
        else:
            trunk = await lk.sip.create_inbound_trunk(
                api.CreateSIPInboundTrunkRequest(
                    trunk=api.SIPInboundTrunkInfo(name=f"{config.HELPLINE_AGENT} inbound", numbers=[args.number])
                )
            )
            rule_id = await dispatch_rule(lk, trunk.sip_trunk_id, args.number)
            print(f"helpline number: {args.number} (inbound trunk {trunk.sip_trunk_id}, dispatch rule {rule_id})")
            print("point the number at this project's SIP URI (LiveKit Cloud, Settings)")
    finally:
        await lk.aclose()


async def outbound(args: argparse.Namespace) -> None:
    lk = api.LiveKitAPI()
    try:
        trunk = await lk.sip.create_outbound_trunk(
            api.CreateSIPOutboundTrunkRequest(
                trunk=api.SIPOutboundTrunkInfo(
                    name=f"{config.CALLER_AGENT} outbound",
                    address=args.address,
                    numbers=[args.number],
                    auth_username=args.username,
                    auth_password=args.password,
                )
            )
        )
        print(f"SIP_OUTBOUND_TRUNK_ID={trunk.sip_trunk_id}")
    finally:
        await lk.aclose()


async def sip_only(args: argparse.Namespace) -> None:
    """A real SIP call with no carrier: the agent's outbound trunk points at this project's own SIP URI, and the
    helpline answers it on an inbound trunk. Both ends share a generated password (kept only in LiveKit, never
    printed), because a project's SIP URI is easy to find and an open trunk would answer anyone who dials it.
    The helpline answers on two numbers: the menu with a claims option, and the variant without one. Each number
    has its own trunk, because a dispatch rule's own number filter is not kept (LiveKit Cloud dropped it and sent
    both numbers to one rule, 22 Oct 2026); a rule limited to a trunk id is what holds."""
    username, password = f"claimcall-{secrets.token_hex(4)}", secrets.token_urlsafe(24)
    lk = api.LiveKitAPI()
    try:
        lines = []
        for number, variant in ((args.number, None), (args.representative_number, "representative")):
            inbound = await lk.sip.create_inbound_trunk(
                api.CreateSIPInboundTrunkRequest(
                    trunk=api.SIPInboundTrunkInfo(
                        name=f"{config.HELPLINE_AGENT} sip-only {number}",
                        numbers=[number],
                        auth_username=username,
                        auth_password=password,
                    )
                )
            )
            rule = await dispatch_rule(lk, inbound.sip_trunk_id, f"sip-{number}", variant)
            lines.append(f"helpline, {variant or 'the menu'}: {number} (trunk {inbound.sip_trunk_id}, rule {rule})")
        outbound = await lk.sip.create_outbound_trunk(
            api.CreateSIPOutboundTrunkRequest(
                trunk=api.SIPOutboundTrunkInfo(
                    name=f"{config.CALLER_AGENT} sip-only",
                    address=args.sip_uri.removeprefix("sip:"),
                    numbers=[args.caller_id],
                    auth_username=username,
                    auth_password=password,
                )
            )
        )
        print("\n".join(lines))
        print(f"SIP_OUTBOUND_TRUNK_ID={outbound.sip_trunk_id}")
        print(f"then: python scripts/call.py --to {args.number} --claim 48213")
    finally:
        await lk.aclose()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="what", required=True)
    h = sub.add_parser("helpline", help="a phone number that reaches the mock helpline")
    g = h.add_mutually_exclusive_group(required=True)
    g.add_argument("--livekit-number", action="store_true", help="buy a LiveKit phone number for it")
    g.add_argument("--number", help="your own carrier's number, E.164, sent to this project's SIP URI")
    h.add_argument("--area-code", help="with --livekit-number: the area code to look in")
    o = sub.add_parser("outbound", help="a trunk the agent dials out through")
    o.add_argument("--address", required=True, help="the carrier's SIP host, e.g. example.pstn.twilio.com")
    o.add_argument("--number", required=True, help="the caller ID to call from, E.164, owned on that carrier")
    o.add_argument("--username", required=True)
    o.add_argument("--password", required=True)
    s = sub.add_parser("sip-only", help="a helpline and an outbound trunk that meet inside this project")
    s.add_argument("--sip-uri", required=True, help="this project's SIP URI (LiveKit Cloud, Settings)")
    s.add_argument("--number", default="+12025550143", help="the helpline's number there (555-01xx, a test range)")
    s.add_argument("--representative-number", default="+12025550144", help="the same line with no claims option")
    s.add_argument("--caller-id", default="+12025550188", help="the number the agent calls from")
    args = ap.parse_args()
    asyncio.run({"helpline": helpline, "outbound": outbound, "sip-only": sip_only}[args.what](args))


if __name__ == "__main__":
    main()
