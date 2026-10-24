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
VOICE_TOOL = "change_voice"
SPEED, STABILITY = (0.7, 1.2), (0.0, 1.0)  # ElevenLabs' accepted override ranges
PROMPT = """You are JARVIS, David's personal assistant, speaking out loud. You are only the
voice: the real JARVIS (with David's memory, notes, tasks, files and tools) answers
through the ask_jarvis tool.

- For any request, question or command beyond a greeting or small talk, call ask_jarvis
  with David's words as close to verbatim as you can. Never make up facts about him.
- While waiting, say at most a few words ("One moment.").
- Speak the tool's answer naturally and briefly. Skip code, links and markdown; say
  it's in the console instead.
- When he asks you to sound different (another voice, a woman's voice, faster, slower,
  calmer, more expressive, back to normal), call change_voice and say nothing more: the
  call reconnects in the new voice by itself.
- Calm, dry, British understatement. Address him as David, or sir sparingly."""

TOOLS = [{
    "type": "client", "name": TOOL, "expects_response": True, "response_timeout_secs": 120,
    "description": "Ask the real JARVIS: anything about David, his notes, tasks, calendar, "
                   "files, devices, or any question or command. Returns the answer to speak.",
    "parameters": {"type": "object", "required": ["request"], "properties": {
        "request": {"type": "string", "description": "David's request, close to verbatim."}}},
}, {
    "type": "client", "name": VOICE_TOOL, "expects_response": True, "response_timeout_secs": 20,
    "description": "Change how you sound: the voice, speaking speed and/or steadiness. "
                   "Give only what David asked to change.",
    "parameters": {"type": "object", "properties": {
        "voice": {"type": "string", "description": "A voice name (e.g. Lily, Daniel) or a "
                  "description (e.g. 'british female', 'deep male'). 'default' for George."},
        "speed": {"type": "number", "description": "0.7 (slow) to 1.2 (fast); 1 is normal."},
        "stability": {"type": "number", "description": "0 (expressive) to 1 (steady, calm); "
                      "0.5 is normal."}}},
}]
OVERRIDES = {"conversation_config_override": {
    "tts": {"voice_id": True, "speed": True, "stability": True},
    "agent": {"first_message": True}}}
DEFAULT_VOICE = "JBFqnCBsd6RMkjVDRZzb"  # George, the voice JARVIS shipped with


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
                "tts_engine": self.voice.engine, "tuning": self.tuning(),
                "ranges": {"speed": SPEED, "stability": STABILITY}}

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
        return {"signed_url": url, "voice_id": self.voice_id(), **self.tuning()}

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

    def tuning(self):
        """Saved speed/stability, or None for the agent's own setting."""
        out = {}
        for key, name, (lo, hi) in (("speed", "ELEVENLABS_VOICE_SPEED", SPEED),
                                    ("stability", "ELEVENLABS_VOICE_STABILITY", STABILITY)):
            try:
                value = float(self.voice.credential(name))
            except (TypeError, ValueError):
                continue
            if lo <= value <= hi:
                out[key] = value
        return out

    def find_voice(self, query):
        """A voice by ID, name ('lily') or description ('british female')."""
        voices = self.voices()["voices"]
        query = str(query or "").strip()
        if query.lower() in ("default", "normal", "jarvis", "original", "usual"):
            query = DEFAULT_VOICE
        for v in voices:
            if query == v["voice_id"]:
                return v
        words = re.findall(r"[a-z]+", query.lower())
        if not words:
            raise ValueError("Say which voice.")
        for v in voices:
            if v["name"].split(" - ")[0].lower() in words:
                return v

        def score(v):
            text = set(re.findall(r"[a-z]+", (v["name"] + " " + v["about"]).lower()))
            if "female" in words and "female" not in text or "male" in words and "female" in text:
                return 0  # 'male' is a substring of 'female'; never cross genders
            return sum(w in text for w in words)
        best = max(voices, key=score, default=None)
        if not best or not score(best):
            names = ", ".join(v["name"].split(" - ")[0] for v in voices[:12])
            raise ValueError(f"No voice like '{query}'. Try one of: {names}.")
        return best

    def set_voice(self, voice_id=None, voice=None, speed=None, stability=None):
        """Pick the voice and its tuning for the live agent (next connect) and read-aloud."""
        changes = {}
        if voice is not None:
            chosen = self.find_voice(voice)
            voice_id, name = chosen["voice_id"], chosen["name"]
        elif voice_id is not None or (speed is None and stability is None):
            if not isinstance(voice_id, str) or not ID.fullmatch(voice_id):
                raise ValueError("That isn't an ElevenLabs voice ID.")
            known = self._voices or []
            if known and voice_id not in {v["voice_id"] for v in known}:
                raise ValueError("That voice isn't in this account's voice list.")
            name = next((v["name"] for v in known if v["voice_id"] == voice_id), voice_id)
        else:
            voice_id, name = self.voice_id(), None
        if voice_id:
            changes["ELEVENLABS_VOICE_ID"] = voice_id
        for value, key, (lo, hi) in ((speed, "ELEVENLABS_VOICE_SPEED", SPEED),
                                     (stability, "ELEVENLABS_VOICE_STABILITY", STABILITY)):
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("Speed and stability must be numbers.")
            changes[key] = round(min(hi, max(lo, float(value))), 2)
        save_secrets(self.state, **changes)
        result = {"current": voice_id, "name": name or next(
            (v["name"] for v in self._voices or [] if v["voice_id"] == voice_id), voice_id)}
        return {**result, **self.tuning()}

    def sync(self):
        """Bring an existing agent up to date: prompt, both tools and the overrides."""
        agent = self.agent_id()
        if not agent or not ID.fullmatch(agent):
            raise ValueError("No ElevenLabs agent yet. Run: jarvis voice agent-setup")
        _call("PATCH", f"/convai/agents/{agent}", self.key(), {
            "conversation_config": {"agent": {"prompt": {"prompt": PROMPT, "tools": TOOLS}}},
            "platform_settings": {"overrides": OVERRIDES}}, timeout=60)
        return {**self.status(), "synced": True}

    def create(self, name="JARVIS"):
        """Create the agent with its client tools and voice overrides; save its id."""
        key = self.key()
        config = {
            "agent": {
                "first_message": "", "language": self.config.get("voice", {}).get("language", "en"),
                "prompt": {"prompt": PROMPT, "tools": TOOLS},
            },
            "asr": {"quality": "high", "user_input_audio_format": "pcm_16000",
                    "keywords": ["JARVIS", "Obsidian", "Zenbook", "Dell"]},
        }
        if self.voice_id():
            config["tts"] = {"voice_id": self.voice_id()}
        data = _call("POST", "/convai/agents/create", key, {
            "name": name, "conversation_config": config,
            "platform_settings": {"overrides": OVERRIDES}}, timeout=60)
        agent = data.get("agent_id")
        if not agent or not ID.fullmatch(agent):
            raise RuntimeError("ElevenLabs didn't return an agent id.")
        save_secrets(self.state, ELEVENLABS_AGENT_ID=agent)
        return {"agent_id": agent, "voice_id": self.voice_id()}
