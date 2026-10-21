"""Settings, from the environment or a .env file.

Only LiveKit Cloud credentials are required. Speech to text (Deepgram Nova 3), the language model
(GPT-4.1 mini) and text to speech (Cartesia Sonic) are all reached through LiveKit Inference with the
same key, so there is one account to set up. Each model can be swapped with an environment variable.
"""

import os

from dotenv import load_dotenv

load_dotenv()

STT = os.getenv("CLAIMCALL_STT", "deepgram/nova-3")
LLM = os.getenv("CLAIMCALL_LLM", "openai/gpt-4.1-mini")
# Two voices, so a listener can tell the agent from the helpline.
CALLER_TTS = os.getenv("CLAIMCALL_CALLER_TTS", "cartesia/sonic-3:a167e0f3-df7e-4d52-a9c3-f949145efdab")
HELPLINE_TTS = os.getenv("CLAIMCALL_HELPLINE_TTS", "cartesia/sonic-3:9626c31c-bec5-4cca-baa8-f8ba9e84c8bc")

CALLER_AGENT = "claim-caller"
HELPLINE_AGENT = "mock-insurer"
HELPLINE_IDENTITY = "helpline"  # the participant the caller listens to, phone line or loopback

# The outbound SIP trunk to dial through (scripts/setup_telephony.py prints it).
OUTBOUND_TRUNK_ID = os.getenv("SIP_OUTBOUND_TRUNK_ID", "")
# Bonus: a person to bring onto the call when the agent is stuck. Empty: the agent ends the call and
# logs that a person is needed.
HANDOFF_NUMBER = os.getenv("HANDOFF_PHONE_NUMBER", "")

LOG_DIR = os.getenv("CLAIMCALL_LOG_DIR", "logs")
# A call that has not found the status by then is a call that is stuck.
MAX_CALL_SECONDS = float(os.getenv("CLAIMCALL_MAX_CALL_SECONDS", "240"))
# Stuck: the line says the same thing this many times, or this long passes, with no progress (no key pressed,
# no claim number given). Fragments are not counted: one menu arrives as six of them.
REPEATS_BEFORE_STUCK = int(os.getenv("CLAIMCALL_REPEATS_BEFORE_STUCK", "3"))
NO_PROGRESS_SECONDS = float(os.getenv("CLAIMCALL_NO_PROGRESS_SECONDS", "60"))
