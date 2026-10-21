"""The mock insurer helpline: Northwind Health's claims line.

A phone menu, not a model. Every prompt is fixed text and every branch is a key press (DTMF), the way a
real insurer's line works, so the agent is tested against something that will not adapt to it. The
claim number can be keyed or said. Press 0 and a representative, played by a language model, takes the
call, so the agent can also be tried against a person.

It answers a real phone number (a SIP inbound trunk, scripts/setup_telephony.py) or, for a test with no
phone line, joins the same LiveKit room as the agent (scripts/call.py --loopback).

Run: python -m claimcall.helpline dev
"""

from __future__ import annotations

import asyncio
import json
import logging

from livekit import api, rtc
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    RunContext,
    StopResponse,
    WorkerOptions,
    cli,
    function_tool,
    get_job_context,
    room_io,
)

from claimcall import claims, config

logger = logging.getLogger("claimcall.helpline")

MENU = (
    "Thank you for calling Northwind Health. For claims, press 1. For billing, press 2. "
    "For pharmacy benefits, press 3. To speak with a representative, press 0."
)
# A variant with no claims option, so the agent has to reach a person (scripts/call.py --variant representative).
MENU_NO_CLAIMS = (
    "Thank you for calling Northwind Health. For billing, press 2. For pharmacy benefits, press 3. "
    "To speak with a representative, press 0."
)
ASK_CLAIM = (
    "Claims. Please enter your five digit claim number on your keypad, followed by the pound key. "
    "You can also say the number."
)
SILENCE_SECONDS = 7.0  # how long the menu waits for a key before it asks again
MAX_REPROMPTS = 2  # then it says goodbye, as real lines do
MAX_WRONG_CLAIMS = 2


async def hang_up() -> None:
    """End the call for everyone: deleting the room drops a phone caller too."""
    ctx = get_job_context()
    await ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name))


class Menu(Agent):
    """The phone tree: menu -> claims -> the status read back. Keys interrupt a prompt, as on any phone line."""

    def __init__(self, menu: str = MENU) -> None:
        super().__init__(instructions="You are a phone menu. You never decide what to say.")
        self.menu = menu
        self.state = "menu"
        self.digits = ""
        self.reprompts = 0
        self.wrong_claims = 0
        self._silence: asyncio.Task | None = None
        self._ending = False

    async def on_enter(self) -> None:
        self.prompt(self.menu)

    # --- what the menu says --------------------------------------------------------------------------------

    def prompt(self, text: str, *, listen: bool = True) -> None:
        """Say a fixed line; once it has played, wait SILENCE_SECONDS for the caller before asking again."""
        self._cancel_silence()
        handle = self.session.say(text, allow_interruptions=False, add_to_chat_ctx=False)
        if listen:
            self._silence = asyncio.create_task(self._wait_then_reprompt(handle))

    async def _wait_then_reprompt(self, handle) -> None:
        await handle.wait_for_playout()
        await asyncio.sleep(SILENCE_SECONDS)
        if self.reprompts >= MAX_REPROMPTS:
            await self.goodbye("We did not receive a selection. Please call again. Goodbye.")
            return
        self.reprompts += 1
        if self.state == "status":
            self._read_status(self.digits)
        else:
            self.prompt(self.menu if self.state == "menu" else ASK_CLAIM)

    def _cancel_silence(self) -> None:
        if self._silence is not None and self._silence is not asyncio.current_task():
            self._silence.cancel()
        self._silence = None

    async def goodbye(self, text: str) -> None:
        if self._ending:
            return
        self._ending = True
        self._cancel_silence()
        await self.session.say(text, allow_interruptions=False, add_to_chat_ctx=False).wait_for_playout()
        await asyncio.sleep(0.5)
        await hang_up()

    # --- what the caller does -----------------------------------------------------------------------------

    def on_key(self, key: str) -> None:
        """A key from the caller's keypad. A key that means something here cuts off whatever the menu is
        saying; any other key is ignored, so a stray "#" after the claim number never silences the status."""
        logger.info("key %s in %s", key, self.state)
        if self._ending or not self._means_something(key):
            return
        self._cancel_silence()
        self.session.interrupt(force=True)
        self.reprompts = 0
        if self.state == "menu":
            self._menu_key(key)
        elif self.state == "claim":
            self._claim_key(key)
        elif key == "9":
            self._read_status(self.digits)
        else:  # "*" in the status
            self.state, self.digits = "menu", ""
            self.prompt(self.menu)

    def _means_something(self, key: str) -> bool:
        if self.state == "status":
            return key in "9*"
        if self.state == "claim":
            return key.isdigit() or (key == "#" and bool(self.digits))
        return True

    def _menu_key(self, key: str) -> None:
        if key == "1" and self.menu == MENU:
            self.state, self.digits = "claim", ""
            self.prompt(ASK_CLAIM)
        elif key == "2":
            self.prompt(
                "Billing is open Monday to Friday, eight A M to six P M. Returning to the main menu. " + self.menu
            )
        elif key == "3":
            self.prompt("For pharmacy benefits, please call the number on the back of your card. " + self.menu)
        elif key == "0":
            self._cancel_silence()
            self.session.say(
                "Please hold while I connect you to a representative.", allow_interruptions=False, add_to_chat_ctx=False
            )
            self.session.update_agent(Representative())
        else:
            self.prompt("Sorry, that is not a valid option. " + self.menu)

    def _claim_key(self, key: str) -> None:
        if key == "#" or (key.isdigit() and len(self.digits) + 1 >= claims.CLAIM_DIGITS):
            if key.isdigit():
                self.digits += key
            self._check_claim(self.digits)
        elif key.isdigit():
            self.digits += key
            # still collecting: wait for the rest without a prompt, but not forever
            self._silence = asyncio.create_task(self._wait_for_digits())

    async def _wait_for_digits(self) -> None:
        await asyncio.sleep(SILENCE_SECONDS)
        self._check_claim(self.digits)

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        """Speech. Only the claim number can be said; anything else is not understood, as on a real line."""
        text = new_message.text_content or ""
        if not self._ending:
            if self.state == "claim" and claims.digits_in(text):
                self._cancel_silence()
                self._check_claim(claims.digits_in(text))
            elif self.state == "menu":
                self._cancel_silence()
                self.prompt("Sorry, I did not understand. " + self.menu)
        raise StopResponse()  # the menu never answers with a model

    # --- the claim ----------------------------------------------------------------------------------------

    def _check_claim(self, number: str) -> None:
        self._cancel_silence()
        self.digits = number
        if claims.status_of(number):
            self.state = "status"
            self._read_status(number)
            return
        self.wrong_claims += 1
        if self.wrong_claims > MAX_WRONG_CLAIMS:
            asyncio.create_task(self.goodbye("We could not find that claim. Please call again later. Goodbye."))
            return
        self.digits = ""
        heard = f"We could not find claim number {claims.spoken(number)}. " if number else ""
        self.prompt(heard + "Please enter your five digit claim number, followed by the pound key.")

    def _read_status(self, number: str) -> None:
        self.prompt(
            f"Claim number {claims.spoken(number)}. The status of this claim is: {claims.status_of(number)}. "
            "To hear this again, press 9. To return to the main menu, press star. Otherwise, you may hang up."
        )


class Representative(Agent):
    """A person at the insurer, for the agent's live person path (task bonus)."""

    def __init__(self) -> None:
        super().__init__(
            instructions=(
                "You are Dana, a claims representative at Northwind Health, on the phone. A caller wants a claim's "
                "status. If they have given a claim number, look it up with look_up_claim straight away; "
                "if not, ask for it. Tell "
                "them the status in one sentence. Speak naturally and briefly, one or two short sentences at a time. "
                "Never invent a status: if the lookup finds nothing, say so and ask them to check the number."
            )
        )

    async def on_enter(self) -> None:
        await self.session.say(
            "Thanks for holding, this is Dana in claims. How can I help you today?", allow_interruptions=True
        )

    @function_tool
    async def look_up_claim(self, context: RunContext, claim_number: str) -> str:
        """Look up a claim's status by its number.

        Args:
            claim_number: the claim number the caller gave, digits only
        """
        number = claims.digits_in(claim_number)
        status = claims.status_of(number)
        return f"Claim {number}: {status}." if status else f"No claim {number} was found."


async def entrypoint(ctx: JobContext) -> None:
    meta = json.loads(ctx.job.metadata or "{}")
    loopback = meta.get("loopback", False)
    await ctx.connect()
    # Mark this participant as the line, so the agent can find it in a loopback room.
    await ctx.room.local_participant.set_attributes({"claimcall.role": "helpline"})
    kinds = [rtc.ParticipantKind.PARTICIPANT_KIND_AGENT] if loopback else [rtc.ParticipantKind.PARTICIPANT_KIND_SIP]
    caller = await ctx.wait_for_participant(kind=kinds)
    sip_attrs = {k: v for k, v in caller.attributes.items() if k.startswith("sip.")}
    logger.info("call from %s, variant %s, %s", caller.identity, meta.get("variant", "standard"), sip_attrs)

    session = AgentSession(stt=config.STT, llm=config.LLM, tts=config.HELPLINE_TTS)
    menu = Menu(MENU_NO_CLAIMS if meta.get("variant") == "representative" else MENU)

    def on_dtmf(ev: rtc.SipDTMF) -> None:
        if ev.participant is not None and ev.participant.identity == caller.identity:
            agent = session.current_agent
            if isinstance(agent, Menu):
                agent.on_key(ev.digit)

    ctx.room.on("sip_dtmf_received", on_dtmf)
    await session.start(
        agent=menu,
        room=ctx.room,
        record=False,
        room_options=room_io.RoomOptions(
            participant_identity=caller.identity, participant_kinds=kinds, delete_room_on_close=True
        ),
    )


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, agent_name=config.HELPLINE_AGENT))
