"""ElevenLabs speech output; credentials stay in the calling environment."""

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path


def speak(text, output):
    key = os.environ.get("ELEVENLABS_API_KEY")
    voice = os.environ.get("ELEVENLABS_VOICE_ID")
    if not key or not voice:
        raise ValueError("Set ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID in your shell.")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", voice):
        raise ValueError("Invalid ElevenLabs voice ID.")
    if not isinstance(text, str) or not text.strip() or len(text) > 5000:
        raise ValueError("Speech text must contain 1–5000 characters.")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Reserve the destination before spending API credits; never overwrite existing audio.
    with output.open("xb") as audio:
        try:
            request = urllib.request.Request(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=mp3_44100_128",
                data=json.dumps({"text": text, "model_id": os.environ.get(
                    "ELEVENLABS_MODEL_ID", "eleven_multilingual_v2")}).encode(),
                headers={"xi-api-key": key, "Content-Type": "application/json",
                         "Accept": "audio/mpeg"}, method="POST")
            with urllib.request.urlopen(request, timeout=60) as response:
                if "audio/" not in response.headers.get("Content-Type", ""):
                    raise RuntimeError("ElevenLabs returned a non-audio response.")
                size = 0
                while block := response.read(65536):
                    size += len(block)
                    if size > 25_000_000:
                        raise RuntimeError("Audio response exceeded 25 MB.")
                    audio.write(block)
                if size == 0:
                    raise RuntimeError("ElevenLabs returned empty audio.")
        except BaseException as exc:
            audio.close()
            output.unlink(missing_ok=True)
            if isinstance(exc, urllib.error.HTTPError):
                raise RuntimeError(f"ElevenLabs returned HTTP {exc.code}; check your key, "
                                   "voice access and credit balance.") from None
            raise
    return {"output": str(output.resolve()), "bytes": size, "provider": "elevenlabs"}
