"""ElevenLabs Agents as JARVIS's real-time voice front end.

The browser talks straight to an ElevenLabs agent over a WebSocket: ElevenLabs does
speech-to-text, turn-taking, barge-in and the voice. Anything beyond small talk goes
back to JARVIS through the agent's `ask_jarvis` client tool, which the page answers by
calling /api/agent/ask, so memory, routing and actions stay JARVIS's own.

The API key never reaches the browser: this side mints a short-lived signed URL.
The agent itself is created once with `jarvis voice agent-setup` (or in the dashboard
with a client tool named `ask_jarvis` and the TTS voice override enabled).
"""

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from .speech import Voice, list_voices, save_secrets

API = "https://api.elevenlabs.io/v1"
ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
TOOL = "ask_jarvis"
PROMPT = """You are JARVIS, David's personal assistant, speaking out loud. You are only the
voice: the real JARVIS (with David's memory, notes, tasks, files and tools) answers
through the ask_jarvis tool.

- For any request, question or command beyond a greeting or small talk, call ask_jarvis
  with David's words as close to verbatim as you can. Never make up facts about him.
- While waiting, say at most a few words ("One moment.").
- Speak the tool's answer naturally and briefly. Skip code, links and markdown; say
  it's in the console instead.
- Calm, dry, British understatement. Address him as David, or sir sparingly."""


def _call(method, path, key, body=None, timeout=20):
    request = urllib.request.Request(
        API + path, method=method, data=None if body is None else json.dumps(body).encode(),
        headers={"xi-api-key": key, "Accept": "application/json",
                 **({"Content-Type": "application/json"} if body is not None else {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read(2_000_000) or b"{}")
    except urllib.error.HTTPError as exc:
        exc.close()
        hint = {401: "the API key was refused (needs the ElevenLabs Agents permission)",
                404: "the agent wasn't found; run `jarvis voice agent-setup`"}.get(exc.code, "")
        raise RuntimeError(f"ElevenLabs returned HTTP {exc.code}"
                           + (f": {hint}." if hint else ".")) from None


class Agent:
    def __init__(self, state_dir, config=None):
        self.state, self.config = state_dir, config or {}
        self.voice = Voice(state_dir, config)  # shares credential lookup and secrets.json
        self._voices, self._voices_at = None, 0.0

    def key(self):
        key = self.voice.credential("ELEVENLABS_API_KEY")
        if not key:
            raise ValueError("No ElevenLabs key yet. Run: jarvis voice setup")
        return key

    def agent_id(self):
        return (self.voice.credential("ELEVENLABS_AGENT_ID")
                or self.config.get("voice", {}).get("agent_id") or None)

    def voice_id(self):
        voice = self.voice.credential("ELEVENLABS_VOICE_ID")
        return voice if voice and ID.fullmatch(voice) else None

    def status(self):
        return {"configured": bool(self.voice.credential("ELEVENLABS_API_KEY")
                                   and self.agent_id()),
                "has_key": bool(self.voice.credential("ELEVENLABS_API_KEY")),
                "agent_id": self.agent_id(), "voice_id": self.voice_id(),
                "tts_engine": self.voice.engine}

    def session(self):
        """A signed WebSocket URL (valid ~15 min) plus the voice to ask for."""
        agent = self.agent_id()
        if not agent or not ID.fullmatch(agent):
            raise ValueError("No ElevenLabs agent yet. Run: jarvis voice agent-setup")
        data = _call("GET", "/convai/conversation/get-signed-url?"
                     + urllib.parse.urlencode({"agent_id": agent}), self.key())
        url = data.get("signed_url", "")
        if not url.startswith("wss://api.elevenlabs.io/"):  # the page's CSP allows only this
            raise RuntimeError("ElevenLabs returned an unexpected conversation URL.")
        return {"signed_url": url, "voice_id": self.voice_id()}

    def voices(self, refresh=False):
        """Voices this key can use, British/male first; cached for ten minutes."""
        if refresh or self._voices is None or time.time() - self._voices_at > 600:
            try:
                self._voices = list_voices(self.key())
            except urllib.error.HTTPError as exc:
                exc.close()
                raise RuntimeError("This ElevenLabs key can't list voices (it needs the "
                                   "Voices read permission).") from None
            self._voices_at = time.time()
        return {"current": self.voice_id(), "voices": self._voices}

    def set_voice(self, voice_id):
        """Pick the voice for both the live agent (next call) and read-aloud replies."""
        if not isinstance(voice_id, str) or not ID.fullmatch(voice_id):
            raise ValueError("That isn't an ElevenLabs voice ID.")
        known = self._voices or []
        if known and voice_id not in {v["voice_id"] for v in known}:
            raise ValueError("That voice isn't in this account's voice list.")
        save_secrets(self.state, ELEVENLABS_VOICE_ID=voice_id)
        name = next((v["name"] for v in known if v["voice_id"] == voice_id), voice_id)
        return {"current": voice_id, "name": name}

    def create(self, name="JARVIS"):
        """Create the agent with the ask_jarvis tool and voice override; save its id."""
        key = self.key()
        config = {
            "agent": {
                "first_message": "", "language": self.config.get("voice", {}).get("language", "en"),
                "prompt": {"prompt": PROMPT, "tools": [{
                    "type": "client", "name": TOOL, "expects_response": True,
                    "response_timeout_secs": 120,
                    "description": "Ask the real JARVIS: anything about David, his notes, "
                                   "tasks, calendar, files, devices, or any question or "
                                   "command. Returns the answer to speak.",
                    "parameters": {"type": "object", "required": ["request"], "properties": {
                        "request": {"type": "string",
                                    "description": "David's request, close to verbatim."}}},
                }]},
            },
            "asr": {"quality": "high", "user_input_audio_format": "pcm_16000",
                    "keywords": ["JARVIS", "Obsidian", "Zenbook", "Dell"]},
        }
        if self.voice_id():
            config["tts"] = {"voice_id": self.voice_id()}
        data = _call("POST", "/convai/agents/create", key, {
            "name": name, "conversation_config": config,
            "platform_settings": {"overrides": {"conversation_config_override": {
                "tts": {"voice_id": True}}}}}, timeout=60)
        agent = data.get("agent_id")
        if not agent or not ID.fullmatch(agent):
            raise RuntimeError("ElevenLabs didn't return an agent id.")
        save_secrets(self.state, ELEVENLABS_AGENT_ID=agent)
        return {"agent_id": agent, "voice_id": self.voice_id()}
