"""Offline voice eval: speaker check, wake word and transcription, scored on recorded and
synthetic clips. Free and local: it never opens the mic, never writes the voiceprint and
never calls a model API. "Training" here means calibration on data, not fine-tuning.

    .venv/bin/python -m jarvis.voice_eval [--quick] [--download] [--no-whisper]

Data:
- positives: David's enrollment WAVs (.jarvis/voice/enroll), augmented with white/pink noise
  (15/9.4/3 dB SNR), synthetic room reverb (RT60 0.3-0.7 s), -12 dB gain and 1 s heads;
- negatives: Kokoro voices (local TTS), JARVIS's own cached TTS lines (.jarvis/audio/cache),
  and Mini LibriSpeech dev-clean-2 (OpenSLR 31, 126 MB, public, no login) under
  ~/.cache/jarvis-overhaul/datasets/librispeech (--download fetches it);
- wake word: Kokoro "Jarvis, ..." phrases (voices x speeds x noise), the enrollment wake
  clips, and confusable negatives ("Travis", "drivers", "I was talking to Jarvis").

Output: speaker EER, FRR at 1% FAR, the threshold calibrate() would choose and the one the
data suggests; wake recall / false-wake rate and p50/p95 latency for Parakeet alone,
Parakeet + variants, Parakeet + fuzzy -> Whisper, and Whisper alone; word error rates.
Written to ~/.cache/jarvis-overhaul/voice-eval.json.

Synthetic and read speech is kinder than David far from the mic: trust it for false
accepts and for ranking engines, not as proof that he will never be rejected live.
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

RATE = 16000
CACHE = Path.home() / ".cache" / "jarvis-overhaul"
LIBRI_URL = "https://www.openslr.org/resources/31/dev-clean-2.tar.gz"

WAKE_PHRASES = ("Jarvis, open Spotify.", "Hey Jarvis, what time is it?", "Jarvis, stop.",
                "Okay Jarvis, summarize my unread email.", "Jarvis, turn the volume down.",
                "Jarvis, start a new plan for the game.", "Hey Jarvis, remind me at five.",
                "What's the weather like tomorrow, Jarvis?")
CONFUSABLES = ("Travis, open Spotify.", "Drivers are late again today.", "I was talking to Jarvis.",
               "The service was slow this morning.", "Try this one, it's better.",
               "Jar this jam before it spoils.", "Java is a programming language.",
               "Do you think it will rain?", "Jarvis's voice sounds odd.", "Open the window please.")


# ---- pure helpers (unit-tested) -------------------------------------------------------
def _np():
    import numpy
    return numpy


def rms(audio):
    np = _np()
    return float(np.sqrt(np.mean(np.asarray(audio, dtype="float64") ** 2))) or 1e-9


def pink(n, rng):
    """1/f noise via spectral shaping, unit RMS."""
    np = _np()
    spec = np.fft.rfft(rng.normal(size=n))
    f = np.arange(len(spec), dtype="float64")
    f[0] = 1.0
    out = np.fft.irfft(spec / np.sqrt(f), n)
    return out / (out.std() or 1.0)


def mix_noise(audio, snr_db, rng, kind="white"):
    """Audio plus noise at `snr_db` (speech RMS over noise RMS)."""
    np = _np()
    audio = np.asarray(audio, dtype="float32")
    noise = pink(len(audio), rng) if kind == "pink" else rng.normal(size=len(audio))
    noise = noise / (np.std(noise) or 1.0) * rms(audio) / 10 ** (snr_db / 20)
    return (audio + noise).astype("float32")


def reverb(audio, rt60, rng):
    """A synthetic room: direct path plus an exponentially decaying noise tail (RT60 s)."""
    np = _np()
    n = int(rt60 * RATE)
    t = np.arange(n) / RATE
    ir = rng.normal(size=n) * np.exp(-6.9 * t / rt60) * 0.3
    ir[0] = 1.0
    wet = np.convolve(np.asarray(audio, dtype="float64"), ir)[:len(audio)]
    return (wet / (np.abs(wet).max() or 1.0) * (np.abs(audio).max() or 1.0)).astype("float32")


def rates(pos, neg, threshold):
    """(false accept rate, false reject rate) at `threshold` (score >= threshold accepts)."""
    far = sum(s >= threshold for s in neg) / len(neg) if neg else 0.0
    frr = sum(s < threshold for s in pos) / len(pos) if pos else 0.0
    return far, frr


def eer(pos, neg):
    """(equal error rate, threshold there), scanning every observed score."""
    best = (2.0, None, None)
    for t in sorted(set(pos) | set(neg)):
        far, frr = rates(pos, neg, t)
        gap = abs(far - frr)
        if gap < best[0]:
            best = (gap, (far + frr) / 2, t)
    return best[1], best[2]


def frr_at_far(pos, neg, far=0.01):
    """The lowest threshold whose false accept rate is <= `far`, and the FRR there."""
    for t in sorted(set(pos) | set(neg) | {1.01}):
        fa, fr = rates(pos, neg, t)
        if fa <= far:
            return fr, t
    return 1.0, None


def wer(ref, hyp):
    """Word error rate, case and punctuation ignored."""
    from .listen import _edits
    norm = lambda s: re.findall(r"[a-z0-9']+", (s or "").lower())
    r, h = norm(ref), norm(hyp)
    return _edits(r, h) / max(1, len(r))


def pct(values, q):
    np = _np()
    return round(float(np.percentile(values, q)), 3) if values else None


# ---- speaker check ------------------------------------------------------------------
def evaluate_speaker(enrolled, positives, negatives, embed, profile=None, pinned=0):
    """Scores with the production rule (listen.VoicePrint.score).
    enrolled: [embedding] of the enrollment clips; positives: [(label, source, audio)]
    scored leave-one-out against the enrollment without `source`; negatives:
    [(label, audio)] against all of it. `profile`/`pinned`: the live voiceprint, scored
    the same way (old plain match vs anchored) to show what learned clips do."""
    from .listen import VoicePrint
    pos, neg, by_label = [], [], {}
    pos_live, neg_live, neg_old = [], [], []
    for label, source, audio in positives:
        e = embed(audio)
        others = [o for j, o in enumerate(enrolled) if j != source]
        s = VoicePrint.score(e, others)
        pos.append(s)
        by_label.setdefault(label, []).append(s)
        if profile is not None:
            keep = [p for j, p in enumerate(profile) if j != source]
            pos_live.append(VoicePrint.score(e, keep, pinned - 1))
    neg_by = {}
    for label, audio in negatives:
        e = embed(audio)
        s = VoicePrint.score(e, enrolled)
        neg.append(s)
        neg_by.setdefault(label.split(":")[0], []).append(s)
        if profile is not None:
            neg_live.append(VoicePrint.score(e, profile, pinned))
            neg_old.append(VoicePrint._match(e, profile))
    report = {}
    neg_embs = [embed(a) for _, a in negatives] if negatives else None
    _, chosen, own = VoicePrint.calibrate(list(enrolled), neg_embs, report)
    e_rate, e_t = eer(pos, neg) if pos and neg else (None, None)
    frr1, t1 = frr_at_far(pos, neg, 0.01) if pos and neg else (None, None)
    # Suggested: the lowest threshold with <= 1% false accepts, inside [FLOOR, CEILING]
    # (live David scores lower than these clips, so never above CEILING).
    suggested = None if t1 is None else round(min(VoicePrint.CEILING, max(VoicePrint.FLOOR, t1)), 3)
    out = {
        "positives": len(pos), "negatives": len(neg),
        "positive_min_by_condition": {k: round(min(v), 3) for k, v in sorted(by_label.items())},
        "negative_max_by_source": {k: round(max(v), 3) for k, v in sorted(neg_by.items())},
        "eer": None if e_rate is None else round(e_rate, 4),
        "eer_threshold": None if e_t is None else round(e_t, 3),
        "frr_at_far_1pct": None if frr1 is None else round(frr1, 4),
        "threshold_at_far_1pct": None if t1 is None else round(t1, 3),
        "calibrate": report, "chosen_threshold": round(chosen, 3),
        "suggested_threshold": suggested,
    }
    for name, t in (("chosen", chosen), ("suggested", suggested)):
        if t is not None:
            far, frr = rates(pos, neg, t)
            out[f"at_{name}"] = {"threshold": round(t, 3), "far": round(far, 4), "frr": round(frr, 4)}
    if profile is not None and pos_live:
        t = chosen
        out["live_profile"] = {
            "entries": len(profile), "pinned": pinned,
            "trusted_learned": len(VoicePrint.trusted(profile, pinned)) - pinned,
            "far_anchored": round(rates(pos_live, neg_live, t)[0], 4),
            "frr_anchored": round(rates(pos_live, neg_live, t)[1], 4),
            "far_old_scoring": round(rates([], neg_old, t)[0], 4),
            "negative_max_anchored": round(max(neg_live), 3),
            "negative_max_old_scoring": round(max(neg_old), 3)}
    from .daemon import BORDERLINE
    out["negatives_in_near_miss_band"] = round(sum(s >= BORDERLINE for s in neg) / max(1, len(neg)), 4)
    return out


# ---- wake word and transcription ----------------------------------------------------
def evaluate_wake(items, fast, whisper=None):
    """items: [(label, audio, reference text)]; a clip is a wake clip when its reference
    opens or closes with "Jarvis" (assistant.wake_word). fast(audio) -> (text, logprob) or
    text; whisper(audio) -> text, or None to skip Whisper. Returns rates per mode."""
    from .assistant import wake_word
    from .listen import fuzzy_wake, wake
    modes = ("parakeet", "parakeet_variants", "parakeet_fuzzy_whisper", "owner_path", "whisper")
    tallies = {m: {"hit": 0, "wake": 0, "false": 0, "other": 0, "ms": []} for m in modes}
    errors = {"parakeet": [], "whisper": []}
    misses, false_wakes, n_hint = [], [], 0
    for label, audio, ref in items:
        is_wake = wake_word(ref) is not None
        t0 = time.perf_counter()
        out = fast(audio)
        t_fast = (time.perf_counter() - t0) * 1000
        text = out[0] if isinstance(out, tuple) else (out or "")
        hint = wake(text) is None and fuzzy_wake(text)
        n_hint += hint
        w_text, t_wh = None, 0.0
        need_whisper = whisper is not None
        if need_whisper:
            t0 = time.perf_counter()
            w_text = whisper(audio)
            t_wh = (time.perf_counter() - t0) * 1000
            errors["whisper"].append(wer(ref, w_text))
        errors["parakeet"].append(wer(ref, text))
        decided = {
            "parakeet": (wake_word(text) is not None, t_fast),
            "parakeet_variants": (wake(text) is not None, t_fast),
            # a follow-up window / push-to-talk: Whisper only on a fuzzy hint
            "parakeet_fuzzy_whisper": ((wake(text) is not None) or bool(
                hint and w_text is not None and wake(w_text) is not None),
                t_fast + (t_wh if hint else 0)),
            # the always-on owner path: Whisper whenever Parakeet lacks the wake word
            "owner_path": ((wake(text) is not None) or bool(w_text is not None and wake(w_text) is not None),
                           t_fast + (t_wh if wake(text) is None else 0)),
            "whisper": (w_text is not None and wake(w_text) is not None, t_wh),
        }
        for mode, (said, ms) in decided.items():
            if mode == "whisper" and w_text is None:
                continue
            tally = tallies[mode]
            tally["ms"].append(ms)
            if is_wake:
                tally["wake"] += 1
                tally["hit"] += said
            else:
                tally["other"] += 1
                tally["false"] += said
        if not is_wake and any(said for said, _ in decided.values()):
            false_wakes.append({"clip": label, "parakeet": text, "whisper": w_text,
                                "modes": [m for m, (said, _) in decided.items() if said]})
        if is_wake and wake(text) is None:
            misses.append({"clip": label, "parakeet": text, "fuzzy": hint,
                           "whisper": w_text})
    result = {}
    for mode, t in tallies.items():
        if not t["ms"]:
            continue
        result[mode] = {"recall": round(t["hit"] / max(1, t["wake"]), 4),
                        "false_wake": round(t["false"] / max(1, t["other"]), 4),
                        "wake_clips": t["wake"], "other_clips": t["other"],
                        "p50_ms": pct(t["ms"], 50), "p95_ms": pct(t["ms"], 95)}
    result["fuzzy_hints"] = n_hint
    result["wer"] = {k: round(sum(v) / len(v), 4) for k, v in errors.items() if v}
    result["parakeet_misses"] = misses[:40]
    result["false_wakes"] = false_wakes[:40]
    return result


# ---- data ---------------------------------------------------------------------------
def enrollment(state):
    from .listen import ENROLL_PHRASES, load_wav
    files = sorted((Path(state) / "voice" / "enroll").glob("*.wav"))
    return [(f.stem, load_wav(f), ENROLL_PHRASES[i] if i < len(ENROLL_PHRASES) else "")
            for i, f in enumerate(files)]


def augment_speaker(clips, rng, quick=False):
    """[(condition, source index, audio)] for each enrollment clip."""
    out = []
    for i, (_, a, _) in enumerate(clips):
        conds = {"clean": a, "white15": mix_noise(a, 15, rng), "white9": mix_noise(a, 9.4, rng),
                 "white3": mix_noise(a, 3, rng), "pink9": mix_noise(a, 9.4, rng, "pink"),
                 "pink3": mix_noise(a, 3, rng, "pink"), "gain-12": a * 0.25, "head1s": a[:RATE]}
        if not quick:
            for rt in (0.3, 0.5, 0.7):
                conds[f"reverb{rt}"] = mix_noise(reverb(a, rt, rng), 15, rng)
        out += [(k, i, v) for k, v in conds.items()]
    return out


def noisy(label, audio, ref, rng, quick=False):
    snrs = (9.4,) if quick else (15, 9.4, 3)
    yield f"{label}:clean", audio, ref
    for snr in snrs:
        yield f"{label}:snr{snr}", mix_noise(audio, snr, rng), ref


def kokoro_clips(state, rng, quick=False):
    """(wake items, confusable items, speaker negatives) from local Kokoro voices."""
    np = _np()
    from kokoro_onnx import Kokoro
    root = Path(state) / "voice" / "kokoro"
    k = Kokoro(str(root / "kokoro-v1.0.onnx"), str(root / "voices-v1.0.bin"))
    voices = [v for v in k.get_voices() if v[:2] in ("af", "am", "bf", "bm")]
    voices = voices[::3] if quick else voices
    speeds = (0.9, 1.0, 1.1, 1.2)
    wake_items, confusable, speakers = [], [], []

    def say(text, voice, speed):
        a, sr = k.create(text, voice=voice, speed=speed, lang="en-gb" if voice[0] == "b" else "en-us")
        a = np.interp(np.arange(0, len(a), sr / RATE), np.arange(len(a)), a).astype("float32")
        lead = np.zeros(int(0.25 * RATE), dtype="float32")  # a little room before speech
        return np.concatenate([lead, a / (np.abs(a).max() or 1.0) * 0.15, lead])
    per_voice = 1 if quick else 2
    for n, voice in enumerate(voices):
        for j in range(per_voice):
            text = WAKE_PHRASES[(n * per_voice + j) % len(WAKE_PHRASES)]
            audio = say(text, voice, speeds[(n + j) % len(speeds)])
            wake_items += list(noisy(f"kokoro:{voice}:{j}", audio, text, rng, quick))
            if j == 0:
                speakers.append((f"kokoro:{voice}", audio))
        text = CONFUSABLES[n % len(CONFUSABLES)]
        confusable += list(noisy(f"confusable:{voice}", say(text, voice, 1.0), text, rng, True))
    del k
    return wake_items, confusable, speakers


def own_voice(state, limit=12):
    """JARVIS's own cached spoken lines: it must never pass as David."""
    from .listen import load_wav
    files = sorted((Path(state) / "audio" / "cache").glob("*.wav"))[:limit]
    return [(f"jarvis_tts:{f.stem}", load_wav(f)) for f in files]


def libri(data_dir, per_speaker=2, speakers=None, seconds=6.0):
    """[(label, audio, transcript)] from Mini LibriSpeech, clipped to `seconds`."""
    import soundfile
    np = _np()
    root = Path(data_dir) / "librispeech" / "LibriSpeech" / "dev-clean-2"
    if not root.is_dir():
        return []
    out = []
    for spk in sorted(p for p in root.iterdir() if p.is_dir())[:speakers]:
        taken = 0
        for chapter in sorted(p for p in spk.iterdir() if p.is_dir()):
            trans = {}
            for line in next(chapter.glob("*.trans.txt")).read_text().splitlines():
                key, _, words = line.partition(" ")
                trans[key] = words
            for flac in sorted(chapter.glob("*.flac")):
                audio, sr = soundfile.read(str(flac), dtype="float32")
                if sr != RATE or len(audio) > seconds * RATE * 1.6:
                    continue  # long ones would need cutting mid-sentence: skip, keep refs exact
                out.append((f"libri:{spk.name}:{flac.stem}", np.asarray(audio, dtype="float32"),
                            trans.get(flac.stem, "")))
                taken += 1
                if taken >= per_speaker:
                    break
            if taken >= per_speaker:
                break
    return out


def download(data_dir):
    """Fetch Mini LibriSpeech dev-clean-2 (126 MB) into its own folder, then unpack it
    after checking no member escapes it."""
    import tarfile
    import urllib.request
    dest = Path(data_dir) / "librispeech"
    if (dest / "LibriSpeech" / "dev-clean-2").is_dir():
        return dest
    dest.mkdir(parents=True, exist_ok=True)
    archive = dest / "dev-clean-2.tar.gz"
    if not archive.is_file():
        urllib.request.urlretrieve(LIBRI_URL, archive)
    with tarfile.open(archive) as tar:
        for m in tar.getmembers():
            if m.name.startswith("/") or ".." in Path(m.name).parts or not (m.isfile() or m.isdir()):
                raise RuntimeError(f"Refusing archive member {m.name!r}")
        try:
            tar.extractall(dest, filter="data")
        except TypeError:  # Python without extraction filters: members were checked above
            tar.extractall(dest)
    return dest


# ---- engines ------------------------------------------------------------------------
def engines(with_whisper=True):
    from .listen import Parakeet, Transcriber
    p = Parakeet()
    p.ready()
    deadline = time.monotonic() + 600
    while p._model is None and p.error is None and time.monotonic() < deadline:
        time.sleep(0.3)
    if p._model is None:
        raise RuntimeError(f"Parakeet did not load: {p.error}")
    whisper = None
    if with_whisper:
        t = Transcriber({"voice": {"stt_engine": "whisper"}})
        whisper = t._whisper  # the production call: small.en int8 on CPU, hotword "Jarvis"
    return p, whisper


def run(state, out_path, data_dir, quick=False, with_whisper=True, seed=0, log=print):
    np = _np()
    from .listen import VoicePrint
    rng = np.random.default_rng(seed)
    started = time.time()
    clips = enrollment(state)
    if len(clips) < 3:
        raise RuntimeError("No enrollment clips in .jarvis/voice/enroll")
    ears = VoicePrint(state)
    embed = ears._embed
    enrolled = [embed(a) for _, a, _ in clips]
    try:
        live = json.loads(ears.path.read_text(encoding="utf-8"))  # read only
        profile, pinned = live.get("profile"), live.get("clips", 0)
        live_threshold = live.get("threshold")
    except (OSError, ValueError):
        profile, pinned, live_threshold = None, 0, None
    log("synthesising Kokoro clips ...")
    wake_items, confusable, kokoro_speakers = kokoro_clips(state, rng, quick)
    books = libri(data_dir, per_speaker=1 if quick else 2, speakers=10 if quick else None)
    negatives = kokoro_speakers + own_voice(state) + [(l, a) for l, a, _ in books]
    log(f"speaker check: {len(clips)} enrollment clips, {len(negatives)} negatives ...")
    speaker = evaluate_speaker(enrolled, augment_speaker(clips, rng, quick), negatives, embed,
                               profile, pinned)
    speaker["live_threshold"] = live_threshold
    p, whisper = engines(with_whisper)
    items = []
    for name, audio, ref in clips:
        items += list(noisy(f"enroll:{name}", audio, ref, rng, quick))
    items += wake_items + confusable
    items += [(l, a, r) for l, a, r in books[: 10 if quick else 30]]
    log(f"wake word and transcription: {len(items)} clips{' with Whisper' if whisper else ''} ...")
    wake_report = evaluate_wake(items, p, whisper)
    report = {"date": time.strftime("%Y-%m-%d %H:%M"), "quick": quick, "seed": seed,
              "seconds": round(time.time() - started, 1), "speaker": speaker, "wake": wake_report,
              "data": {"enrollment": len(clips), "kokoro_wake_clips": len(wake_items),
                       "confusable_clips": len(confusable), "librispeech": len(books),
                       "jarvis_tts": len(own_voice(state))},
              "note": ("Synthetic and read speech is kinder than David far from the mic: use it "
                       "for false accepts and engine ranking; confirm false rejects live.")}
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m jarvis.voice_eval", description=__doc__.split("\n")[0])
    ap.add_argument("--state", default=str(Path(__file__).resolve().parents[1] / ".jarvis"))
    ap.add_argument("--out", default=str(CACHE / "voice-eval.json"))
    ap.add_argument("--data", default=str(CACHE / "datasets"))
    ap.add_argument("--download", action="store_true", help="fetch Mini LibriSpeech (126 MB) first")
    ap.add_argument("--quick", action="store_true", help="fewer voices and conditions (~2 min)")
    ap.add_argument("--no-whisper", action="store_true", help="skip Whisper (much faster)")
    args = ap.parse_args(argv)
    if args.download:
        download(args.data)
    log = lambda m: print(m, file=sys.stderr, flush=True)
    report = run(args.state, args.out, args.data, args.quick, not args.no_whisper, log=log)
    s, w = report["speaker"], report["wake"]
    print(json.dumps({"out": args.out, "speaker": {k: s.get(k) for k in (
        "eer", "frr_at_far_1pct", "chosen_threshold", "suggested_threshold", "at_chosen")},
        "wake": {k: v for k, v in w.items() if isinstance(v, dict) and "recall" in v}}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
