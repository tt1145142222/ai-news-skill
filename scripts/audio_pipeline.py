"""Free local narration, sample-timed caption units, and optional music.

Public interface: build_audio(scenes, root, work, config=None) -> dict.
The returned paths are absolute. All new files live below ``work`` except the
reusable narration cache at ``root/work/audio-cache`` (override cache_dir).
Schema 2 captions span actual synthesized utterance samples. Long utterances
can use explicitly supplied caption texts and time boundaries; this is not
word-level forced alignment. Legacy scenes retain visibly labelled estimates.
The caller must rewrite copy if duration_pass is false;
this module never stretches audio or pads an episode to hit its target length.

Requires the project's existing sherpa-onnx, soundfile, numpy and
imageio-ffmpeg installation. No network, API key, or paid service is used.
This function changes cwd temporarily for Kokoro's relative Chinese-path
resources; call it in one process rather than concurrent threads.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

import imageio_ffmpeg
import numpy as np
import sherpa_onnx as so
import soundfile as sf


PIPELINE_VERSION = "2.0.0"
DEFAULTS = {
    "speaker_id": 87,
    "speed": 1.0,
    "num_threads": 4,
    "paragraph_gap_seconds": 0.22,
    "unit_gap_seconds": 0.08,
    "model_dir": "models/kokoro-int8-multi-lang-v1_1",
    "min_seconds": 1.0,
    "max_seconds": 900.0,
    "voice_lufs": -16.0,
    "voice_true_peak_db": -2.0,
    "bgm_enabled": False,
    "bgm_lufs": -25.0,
    "bgm_bpm": 104.0,
    "duck_ratio": 2.0,
    "duck_threshold": 0.055,
    "engine": "kokoro",
    "transition_click_enabled": False,
    "transition_click_peak_dbfs": -20.0,
    "transition_click_asset_path": None,
    "transition_click_asset_sha256": None,
    "transition_click_gain_db": 0.0,
    "transition_click_start_offset_seconds": 0.0,
}


def _run(ffmpeg, args):
    result = subprocess.run(
        [ffmpeg, "-hide_banner", *map(str, args)], capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode:
        raise RuntimeError(f"FFmpeg failed ({result.returncode}): {result.stderr[-12000:]}")
    return result


def _sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


@contextmanager
def _working_directory(directory):
    previous = Path.cwd()
    os.chdir(directory)
    try:
        yield
    finally:
        os.chdir(previous)


def _model_identity(root, model):
    # Content hashes avoid reusing audio after a model/lexicon has been replaced.
    files = [model / name for name in [
        "model.int8.onnx", "voices.bin", "tokens.txt", "lexicon-us-en.txt",
        "lexicon-zh.txt",
    ]]
    data = model / "espeak-ng-data"
    if not data.is_dir():
        raise FileNotFoundError(f"Missing Kokoro resource directory: {data}")
    files += sorted(p for p in data.rglob("*") if p.is_file())
    manifests = {str(p.relative_to(root)).replace("\\", "/"): _sha256(p) for p in files}
    return hashlib.sha256(_canonical(manifests).encode("utf-8")).hexdigest()


def _create_tts(model_relative, config):
    m = Path(model_relative)
    settings = so.OfflineTtsConfig(model=so.OfflineTtsModelConfig(
        kokoro=so.OfflineTtsKokoroModelConfig(
            model=str(m / "model.int8.onnx"), voices=str(m / "voices.bin"),
            tokens=str(m / "tokens.txt"), data_dir=str(m / "espeak-ng-data"),
            lexicon=",".join(str(m / item) for item in ["lexicon-us-en.txt", "lexicon-zh.txt"]),
        ), num_threads=int(config["num_threads"]),
    ))
    return so.OfflineTts(settings)


def _estimated_captions(text, start, speech_duration):
    # Keep English words together when weighting Chinese/English copy. Exact
    # clause boundaries still require an alignment or listening review.
    phrases = [p.strip() for p in re.split(r"(?<=[，。！？；!?;])", text) if p.strip()]
    weights = [max(1, len(re.sub(r"[A-Za-z]+", "英英", p))) for p in phrases]
    total = sum(weights)
    elapsed = start
    captions = []
    for index, (phrase, weight) in enumerate(zip(phrases, weights)):
        end = start + speech_duration if index == len(phrases) - 1 else elapsed + speech_duration * weight / total
        captions.append({"start": elapsed, "end": end, "text": phrase, "alignment": "estimated"})
        elapsed = end
    return captions


def _morning_music(samples, rate, bpm, output):
    """Original local arrangement matching the approved 104 BPM morning mix."""
    duration = samples / rate
    music = np.zeros((samples, 2), dtype=np.float64)
    beat = 60.0 / bpm

    def add(note, start, length, level=.15, pan=0, kind="pluck"):
        n = int(length * rate)
        t = np.arange(n) / rate
        f = 440 * 2 ** ((note - 69) / 12)
        if kind == "pluck":
            a = sum((1 / h ** 1.7) * np.sin(2 * np.pi * f * h * t) * np.exp(-t * (2.2 + h * .9)) for h in range(1, 7))
            a *= 1 - np.exp(-t * 350)
        elif kind == "keys":
            a = (np.sin(2 * np.pi * f * t) + .23 * np.sin(2 * np.pi * 2 * f * t) * np.exp(-t * 4) + .08 * np.sin(2 * np.pi * 3 * f * t)) * np.exp(-t * 2.8) * (1 - np.exp(-t * 180))
        elif kind == "bass":
            a = (np.sin(2 * np.pi * f * t) + .12 * np.sin(2 * np.pi * 2 * f * t)) * np.exp(-t * 3) * (1 - np.exp(-t * 100))
        elif kind == "kick":
            a = np.sin(2 * np.pi * (52 * t + 4 * (1 - np.exp(-t * 30)))) * np.exp(-t * 28) * (1 - np.exp(-t * 900))
        else:
            a = (np.sin(2 * np.pi * 820 * t) + .35 * np.sin(2 * np.pi * 1330 * t)) * np.exp(-t * 110) * (1 - np.exp(-t * 1200))
        a *= np.minimum(1, np.maximum(0, (length - t) / .04))
        i = int(start * rate)
        j = min(i + n, len(music))
        if j <= i:
            return
        gain = np.array([np.sqrt((1 - pan) / 2), np.sqrt((1 + pan) / 2)])
        music[i:j] += a[:j-i, None] * level * gain

    chords = [
        ([60, 64, 67, 71], 36), ([57, 60, 64, 67], 33),
        ([53, 57, 60, 64], 29), ([55, 59, 62, 67], 31),
        ([60, 64, 67, 72], 36), ([52, 55, 59, 62], 28),
        ([53, 57, 60, 65], 29), ([55, 59, 62, 65], 31),
    ]
    melodies = [
        [76, 79, 81, 79], [76, 72, 74, 76], [77, 76, 72, 69], [74, 79, 77, 74],
        [76, 79, 84, 81], [79, 76, 74, 71], [72, 77, 76, 72], [74, 71, 67, 72],
    ]
    bars = int(duration / (4 * beat))
    for bar in range(bars):
        start = bar * 4 * beat
        chord, bass = chords[bar % 8]
        for k in [0, 1.5, 2.5, 3.5]:
            for ix, note in enumerate(chord):
                add(note, start + k * beat + ix * .012, 1.1, .11 if k == 0 else .075, -.35 if ix % 2 else .3, "pluck")
        for k, note in [(0, bass), (2, bass + 7)]:
            add(note, start + k * beat, beat * 1.55, .19, 0, "bass")
        for k in [0, 2]:
            add(48, start + k * beat, .25, .055, 0, "kick")
        for k in [1, 3]:
            add(48, start + k * beat, .12, .022, .15, "rim")
        if bar % 2 == 0:
            for k, note in zip([.5, 1.25, 2.5, 3.25], melodies[bar % 8]):
                add(note, start + k * beat, 1.0, .060, -.12, "keys")
    ending = bars * 4 * beat
    for ix, note in enumerate([60, 64, 67, 72]):
        add(note, ending + ix * .025, 2.2, .11, 0, "keys")
    dry = music.copy()
    for delay, gain in [(.11, .09), (.21, .06), (.34, .035)]:
        n = int(delay * rate)
        if n < samples:
            music[n:] += dry[:-n, ::-1] * gain
    positions = np.arange(samples)
    fade = np.minimum(1, positions / (rate * .8)) * np.minimum(1, (samples - positions) / (rate * 2.2))
    music *= fade[:, None]
    music *= .85 / max(float(np.max(np.abs(music))), 1e-9)
    sf.write(output, music, rate, subtype="PCM_24")


def _metrics(path):
    data, rate = sf.read(path)
    peak = float(np.max(np.abs(data)))
    if not np.isfinite(data).all() or peak >= .999:
        raise RuntimeError(f"Invalid or clipped audio: {path}")
    return {
        "samples": len(data), "sample_rate": rate,
        "channels": 1 if data.ndim == 1 else data.shape[1],
        "duration": len(data) / rate, "peak_dbfs": float(20 * np.log10(peak + 1e-12)),
        "rms_dbfs": float(20 * np.log10(np.sqrt(np.mean(data * data)) + 1e-12)),
        "clipped_samples": int(np.sum(np.abs(data) >= .999)), "finite": True,
    }


def _integrated_loudness(ffmpeg, path):
    result = _run(ffmpeg, ["-i", path, "-vn", "-af", "loudnorm=I=-16:TP=-2:LRA=8:print_format=json", "-f", "null", "-"])
    matches = re.findall(r'\{\s*"input_i".*?\}', result.stderr, flags=re.S)
    if not matches:
        raise RuntimeError(f"Could not measure loudness for {path}")
    reading = json.loads(matches[-1])
    return {"integrated_lufs": float(reading["input_i"]), "true_peak_dbtp": float(reading["input_tp"]),
            "loudness_range_lu": float(reading["input_lra"])}


def _config(config):
    supplied = config or {}
    voice, music, video = [supplied.get(name, {}) for name in ["voice", "music", "video"]]
    mapped = {
        "speaker_id": voice.get("speaker_id", DEFAULTS["speaker_id"]),
        "engine": voice.get("engine", DEFAULTS["engine"]),
        "model_dir": voice.get("model_dir", DEFAULTS["model_dir"]),
        "transition_click_enabled": supplied.get("transition_sound", {}).get("enabled", False),
        "transition_click_peak_dbfs": supplied.get("transition_sound", {}).get("peak_dbfs", -20.0),
        "transition_click_asset_path": supplied.get("transition_sound", {}).get("asset_path"),
        "transition_click_asset_sha256": supplied.get("transition_sound", {}).get("asset_sha256"),
        "transition_click_gain_db": supplied.get("transition_sound", {}).get("gain_db", 0.0),
        "transition_click_start_offset_seconds": supplied.get("transition_sound", {}).get("start_offset_seconds", 0.0),
        "speed": voice.get("speed", DEFAULTS["speed"]),
        "num_threads": voice.get("threads", DEFAULTS["num_threads"]),
        "paragraph_gap_seconds": voice.get("paragraph_gap_seconds", DEFAULTS["paragraph_gap_seconds"]),
        "unit_gap_seconds": voice.get("unit_gap_seconds", DEFAULTS["unit_gap_seconds"]),
        "voice_lufs": voice.get("target_lufs", DEFAULTS["voice_lufs"]),
        "voice_true_peak_db": voice.get("true_peak_db", DEFAULTS["voice_true_peak_db"]),
        "bgm_bpm": music.get("bpm", DEFAULTS["bgm_bpm"]),
        "bgm_enabled": music.get("enabled", DEFAULTS["bgm_enabled"]),
        "bgm_lufs": music.get("target_lufs", DEFAULTS["bgm_lufs"]),
        "duck_ratio": music.get("duck_ratio", DEFAULTS["duck_ratio"]),
        "duck_threshold": music.get("duck_threshold", DEFAULTS["duck_threshold"]),
        "min_seconds": video.get("min_seconds", DEFAULTS["min_seconds"]),
        "max_seconds": video.get("max_seconds", DEFAULTS["max_seconds"]),
    }
    return {**DEFAULTS, **mapped, **supplied}


def transcribe_audio(audio, root, work, timeline=None, config=None):
    """Transcribe final mixed audio per scene for review, not forced alignment.

    ``audio`` accepts a path to the final mix/video, or a build_audio result.
    Always review ASR mismatches against the actual source audio: proper nouns,
    English, dates and numbers can be recognized incorrectly by this model.
    """
    if isinstance(audio, dict):
        timeline = timeline or audio["timeline"]
        audio = audio["paths"]["mix"]
    if not timeline:
        raise ValueError("Provide the episode timeline for scene-by-scene ASR")
    root, work, audio = Path(root).resolve(), Path(work).resolve(), Path(audio).resolve()
    work.mkdir(parents=True, exist_ok=True)
    cfg = _config(config)
    model = Path("models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09")
    for name in ["model.int8.onnx", "tokens.txt"]:
        if not (root / model / name).is_file():
            raise FileNotFoundError(f"Missing local ASR model: {root / model / name}")
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    # Deliberately narrow: reuse only within this work for identical final bytes,
    # timeline, model, decoder and recognition implementation/configuration.
    # Cached computation never contains or inherits a human review conclusion.
    identity = {"schema": 1, "input_sha256": _sha256(audio),
                "timeline_sha256": hashlib.sha256(_canonical(timeline).encode("utf-8")).hexdigest(),
                "implementation_sha256": _sha256(__file__),
                "engine_version": importlib.metadata.version("sherpa-onnx"),
                "model_sha256": {name: _sha256(root / model / name)
                                 for name in ["model.int8.onnx", "tokens.txt"]},
                "decoder_sha256": _sha256(ffmpeg),
                "decoder": {"sample_rate": 16000, "channels": 1, "codec": "pcm_s16le"},
                "recognition": {"num_threads": int(cfg["num_threads"]),
                                "language": "", "use_itn": True}}
    cache = work / "transcription-cache.json"
    rows = None
    try:
        saved = json.loads(cache.read_text(encoding="utf-8"))
        candidate = saved["rows"]
        if (saved["identity"] == identity and isinstance(candidate, list)
                and len(candidate) == len(timeline)
                and saved["rows_sha256"] == hashlib.sha256(_canonical(candidate).encode("utf-8")).hexdigest()):
            rows = candidate
    except (OSError, ValueError, KeyError, TypeError):
        pass  # Missing, old or damaged cache is a computation miss, never a PASS.
    cache_hit = rows is not None
    if not cache_hit:
        mono = work / "final-mix-asr16k.wav"
        _run(ffmpeg, ["-y", "-v", "error", "-i", audio, "-ar", 16000, "-ac", 1, "-c:a", "pcm_s16le", mono])
        signal, rate = sf.read(mono, dtype="float32")
        rows = []
        with _working_directory(root):
            recognizer = so.OfflineRecognizer.from_sense_voice(
                model=str(model / "model.int8.onnx"), tokens=str(model / "tokens.txt"),
                num_threads=int(cfg["num_threads"]), language="", use_itn=True,
            )
            for scene in timeline:
                start, end = float(scene["start"]), float(scene["end"])
                section = signal[max(0, round(start * rate)):min(len(signal), round(end * rate))]
                if not section.size:
                    raise ValueError(f"Scene {scene['id']} falls outside final audio")
                stream = recognizer.create_stream()
                stream.accept_waveform(rate, section)
                recognizer.decode_stream(stream)
                rows.append({"scene_id": str(scene["id"]), "start": start, "end": end,
                             "expected": scene.get("spoken_text", scene.get("speech", "")),
                             "display": scene.get("display_text", scene.get("speech", "")),
                             "recognized": stream.result.text})
    if _sha256(audio) != identity["input_sha256"]:
        raise RuntimeError("Final audio changed during transcription; rerun for the current file")
    if not cache_hit:
        cached = {"identity": identity, "rows": rows,
                  "rows_sha256": hashlib.sha256(_canonical(rows).encode("utf-8")).hexdigest()}
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=work,
                                         suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(cached, handle, ensure_ascii=False, indent=2)
        try:
            temporary.replace(cache)
        finally:
            temporary.unlink(missing_ok=True)
    report = {
        "input": str(audio), "method": "SenseVoice per-scene transcription of final mix",
        "scene_count": len(rows), "scenes": rows, "listening_review": False,
        "computation_cache_hit": cache_hit, "computation_identity": identity,
        "alignment": "not forced alignment", "review_required": True,
        "limits": "ASR is an error-finding aid, not proof of correct pronunciation or a listening review; inspect numbers, names and missing phrases.",
    }
    (work / "transcription-review.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (work / "transcription-review.txt").write_text("\n\n".join(f"[{r['scene_id']}] {r['start']:.3f}-{r['end']:.3f}s\n预期：{r['expected']}\n识别：{r['recognized']}" for r in rows), encoding="utf-8")
    return report


def _caption_content(text):
    # Ignore editorial punctuation without turning 9.9 into 99 or 12:30 into
    # 1230. Numerical separators inside a number remain factual content.
    value = re.sub(r"\s+", "", str(text))
    value = re.sub(r"[。！？；、!?;]+", "", value)
    return re.sub(r"(?<!\d)[，,．.：:]|[，,．.：:](?!\d)", "", value)


def _speech_units(item, cfg):
    """Never infer TTS cuts from screen width, characters, or subtitle length."""
    modern = (item.get("layout") in {"overview", "news"}
              or int(cfg.get("schema_version", cfg.get("episode_schema", 1))) >= 2
              or "speech_units" in item)
    if not modern:
        return False, [{"text": str(item["speech"]).strip(), "display": str(item["speech"]).strip()}]
    units = item.get("speech_units")
    if not isinstance(units, list) or not units:
        raise ValueError(f"Schema 2 scene {item['id']} requires speech_units with complete utterances; do not split TTS by subtitle width")
    for index, unit in enumerate(units):
        if not isinstance(unit, dict) or not isinstance(unit.get("text"), str) or not unit["text"].strip():
            raise ValueError(f"Empty speech_units[{index}].text in scene {item['id']}")
        if not isinstance(unit.get("display"), str) or not unit["display"].strip():
            raise ValueError(f"speech_units[{index}].display must contain normalized visible text")
        boundaries, texts = unit.get("caption_boundaries"), unit.get("caption_texts")
        if (boundaries is None) != (texts is None):
            raise ValueError("Provide caption_boundaries and caption_texts together")
        if boundaries is not None:
            if not isinstance(texts, list) or not texts or any(not isinstance(t, str) or not t.strip() for t in texts):
                raise ValueError("caption_texts must contain nonempty normalized subtitle strings")
            if not isinstance(boundaries, list) or len(boundaries) != len(texts) + 1:
                raise ValueError("caption_boundaries must have len(caption_texts)+1 values")
            if _caption_content("".join(texts)) != _caption_content(unit["display"]):
                raise ValueError("caption_texts must reproduce the complete display text without dropping or adding words")
    return True, units


def _unit_captions(unit, start_sample, sample_count, rate):
    """One real synthesis interval, or editorial boundaries inside that interval."""
    duration = sample_count / rate
    texts = unit.get("caption_texts", [unit["display"]])
    boundaries = unit.get("caption_boundaries")
    method = "measured_synthesis_units"
    if boundaries is None:
        points = [0, sample_count]
    else:
        values = [float(v) for v in boundaries]
        if not all(np.isfinite(v) for v in values):
            raise ValueError("Caption boundaries must be finite")
        if abs(values[0]) > .002 or abs(values[-1]-duration) > .05:
            raise ValueError(f"Caption boundaries must span actual unit duration {duration:.6f}s (start 0, final within 0.05s)")
        if any(b <= a for a, b in zip(values, values[1:])):
            raise ValueError("Caption boundaries must increase")
        points = [round(v * rate) for v in values]
        points[0], points[-1] = 0, sample_count
        if any(b <= a for a, b in zip(points, points[1:])):
            raise ValueError("Caption boundaries collapse or leave the actual unit after sample rounding")
        method = "editor_supplied_within_measured_unit"
    captions = [{"start": (start_sample + a) / rate, "end": (start_sample + b) / rate,
                 "text": text.strip(), "spoken_text": unit["text"].strip(),
                 "alignment": method, "alignment_method": method,
                 "boundary_review_required": boundaries is not None}
                for text, a, b in zip(texts, points, points[1:])]
    return captions, method


def _legacy_captions(item, start, duration):
    speech = str(item["speech"]).strip()
    captions = _estimated_captions(speech, start, duration)
    boundaries = item.get("caption_boundaries")
    if boundaries is not None:
        values = [float(v) for v in boundaries]
        if (len(values) != len(captions) + 1 or not all(np.isfinite(v) for v in values)
                or abs(values[0]) > .002 or abs(values[-1]-duration) > .05
                or any(b <= a for a, b in zip(values, values[1:]))):
            raise ValueError("Legacy caption boundaries must increase and span the actual paragraph")
        for index, caption in enumerate(captions):
            caption["start"] = start + values[index]
            caption["end"] = start + (duration if index == len(captions)-1 else values[index+1])
            caption["alignment"] = "editor-supplied legacy boundaries; review required"
    overrides = item.get("subtitle_overrides", {})
    unknown = set(overrides) - {c["text"] for c in captions}
    if unknown:
        raise ValueError(f"Subtitle override does not match exact spoken clause: {unknown}")
    for caption in captions:
        if caption["text"] in overrides:
            caption["spoken_text"] = caption["text"]
            caption["text"] = str(overrides[caption["text"]])
        caption["alignment_method"] = "legacy_estimated" if boundaries is None else "legacy_editor_supplied"
    return captions


def _transition_asset(config, work, ffmpeg, mix_rate=48000):
    """Decode a pinned real click without changing its envelope or channels.

    Relative assets resolve from the installed Skill, never today's work/cwd.
    A supplied but invalid asset is an error, even when clicks are disabled;
    only a genuinely absent asset selects the historical synthesis branch.
    """
    supplied = config.get("transition_click_asset_path")
    if supplied is None:
        return None
    if not isinstance(supplied, str) or not supplied.strip():
        raise ValueError("Transition click asset_path must be a nonempty path")
    source = Path(supplied)
    if not source.is_absolute():
        source = Path(__file__).resolve().parent.parent / source
    source = source.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Missing transition click asset: {source}")
    expected = config.get("transition_click_asset_sha256")
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
        raise ValueError("Transition click asset requires an explicit SHA256")
    digest = _sha256(source)
    if digest != expected.lower():
        raise ValueError(f"Changed transition click asset (SHA256 mismatch): {source}")
    try:
        raw, source_rate = sf.read(source, dtype="float64", always_2d=True)
    except (RuntimeError, OSError) as exc:
        raise ValueError(f"Cannot decode transition click asset: {source}") from exc
    if not raw.size or raw.shape[1] not in (1, 2) or not np.isfinite(raw).all():
        raise ValueError("Transition click asset must be nonempty finite mono/stereo audio")
    source_peak = float(np.max(np.abs(raw)))
    if not 0 < source_peak < .999:
        raise ValueError("Transition click asset must be audible and unclipped")
    gain_db = float(config.get("transition_click_gain_db", 0.0))
    if not np.isfinite(gain_db):
        raise ValueError("Transition click gain_db must be finite")
    data = raw
    if source_rate != mix_rate:
        # FFmpeg's rate conversion preserves channel layout and duration. There
        # is deliberately no silence trim, fade, denoise, normalization or EQ.
        resampled = Path(work) / "transition-click-resampled.wav"
        if resampled.resolve() == source:
            raise ValueError("Transition click source cannot also be its resampling output")
        _run(ffmpeg, ["-y", "-v", "error", "-i", source, "-ar", mix_rate,
                      "-c:a", "pcm_f64le", resampled])
        data, decoded_rate = sf.read(resampled, dtype="float64", always_2d=True)
        if decoded_rate != mix_rate or data.shape[1] != raw.shape[1]:
            raise RuntimeError("Transition click resampling changed its channel layout")
        if abs(len(data) / mix_rate - len(raw) / source_rate) > 1 / mix_rate + 1e-9:
            raise RuntimeError("Transition click resampling changed its duration")
    with np.errstate(over="ignore", invalid="ignore"):
        data = data * np.power(10.0, gain_db / 20.0)
    if not np.isfinite(data).all() or not 0 < np.max(np.abs(data)) < .999:
        raise ValueError("Transition click gain creates silent, invalid or clipped audio")
    if _sha256(source) != digest:
        raise ValueError("Transition click asset changed while being decoded")
    if source == (Path(work) / "transition-click.wav").resolve():
        raise ValueError("Transition click source cannot also be its rendered evidence copy")
    details = {
        "kind": "pinned real audio sample", "asset_path": str(source), "asset_sha256": digest,
        "gain_db": gain_db, "source_sample_rate": source_rate, "source_channels": raw.shape[1],
        "source_samples": len(raw), "source_duration": len(raw) / source_rate,
        "source_peak_dbfs": float(20 * np.log10(source_peak)),
        "sample_rate": mix_rate, "channels": data.shape[1], "samples": len(data),
        "duration": len(data) / mix_rate, "sample_peak_dbfs": float(20 * np.log10(np.max(np.abs(data)))),
        "resampled": source_rate != mix_rate,
        "processing": "Necessary sample-rate conversion and uniform gain only; no trim, fade, normalization, EQ or synthesized replacement",
    }
    return data, details


def build_audio(scenes, root, work, config=None):
    """Build actual utterance timing, narration, optional music, and honest QA.

    Schema 2 accepts speech_units=[{text, display}], preferably full natural
    sentences or longer contextual utterances. A long unit may contain
    caption_texts plus caption_boundaries measured against its cached audio.
    Units are not automatically cut at 2-5 seconds; that is a caption-length
    advisory, and proper names/meaning need editorial review. Fixed opening
    utterances share the same content-addressed cache across episodes.
    Music is generated and mixed only when music.enabled is explicitly true.
    """
    started = time.perf_counter()
    cfg = _config(config)
    root = Path(root).resolve()
    work = Path(work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    if not scenes:
        raise ValueError("Provide at least one scene with id and speech")
    ids = [str(item["id"]) for item in scenes]
    if len(ids) != len(set(ids)):
        raise ValueError("Scene IDs must be unique")
    if not (0 < float(cfg["speed"]) <= 2):
        raise ValueError("speed must be in (0, 2]")
    if not (0 <= float(cfg["paragraph_gap_seconds"]) <= 1):
        raise ValueError("paragraph_gap_seconds must be between 0 and 1")
    if not (0 <= float(cfg["unit_gap_seconds"]) <= 1):
        raise ValueError("unit_gap_seconds must be between 0 and 1")
    click_offset = float(cfg["transition_click_start_offset_seconds"])
    if not np.isfinite(click_offset):
        raise ValueError("Transition click start_offset_seconds must be finite")
    if not isinstance(cfg["bgm_enabled"], bool):
        raise ValueError("music.enabled must be true or false")
    if not (-40 <= float(cfg["voice_lufs"]) <= -5):
        raise ValueError("voice target_lufs must be between -40 and -5")
    if not (-9 <= float(cfg["voice_true_peak_db"]) <= -1):
        raise ValueError("voice true_peak_db must be between -9 and -1")
    if not (0 < float(cfg["bgm_bpm"]) <= 240):
        raise ValueError("bgm_bpm must be in (0, 240]")
    if not (0 <= float(cfg["min_seconds"]) <= float(cfg["max_seconds"]) <= 3600):
        raise ValueError("Require 0 <= min_seconds <= max_seconds <= 3600")
    prepared = []
    for item in scenes:
        if not isinstance(item.get("speech"), str) or not item["speech"].strip():
            raise ValueError(f"Empty speech for scene {item['id']}")
        modern, units = _speech_units(item, cfg)
        prepared.append((item, modern, units))
    external = cfg["engine"] in {"prebuilt-local", "qwen3-tts"}
    if cfg["engine"] not in {"kokoro", "prebuilt-local", "qwen3-tts"}:
        raise ValueError("Unsupported voice engine")
    model = (root / cfg["model_dir"]).resolve()
    model_relative = model.relative_to(root)
    cache = Path(cfg.get("cache_dir") or root / "work/audio-cache").resolve()
    cache.mkdir(parents=True, exist_ok=True)
    model_id = "prebuilt-local-per-unit-sha256" if external else _model_identity(root, model)
    engine_version = "import-v1" if external else importlib.metadata.version("sherpa-onnx")
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    transition_asset = _transition_asset(cfg, work, ffmpeg)
    chunks, timeline, all_captions = [], [], []
    offset_samples = 0
    rate = None
    tts = None
    cache_hits = 0
    unit_count = 0
    warnings = []
    with _working_directory(root):
        for scene_index, (item, modern, specs) in enumerate(prepared):
            speech = str(item["speech"]).strip()
            scene_start_samples = offset_samples
            captions, units = [], []
            speech_samples = 0
            for unit_index, spec in enumerate(specs):
                text = spec["text"].strip()
                identity = {
                    "schema": 1, "speech": text, "speaker_id": int(cfg["speaker_id"]),
                    "speed": float(cfg["speed"]), "model_sha256": model_id,
                    "engine": "sherpa-onnx", "engine_version": engine_version,
                    "num_threads": int(cfg["num_threads"]),
                }
                key = hashlib.sha256(_canonical(identity).encode("utf-8")).hexdigest()
                cached = cache / f"{key}.wav"
                data, hit = None, False
                if external:
                    cached = Path(spec.get("audio_path", "")).resolve()
                    if not cached.is_file() or _sha256(cached) != spec.get("audio_sha256"):
                        raise ValueError(f"Missing/changed prebuilt utterance: {item['id']} unit {unit_index}")
                    if not spec.get("audio_provenance"):
                        raise ValueError("Prebuilt audio requires generation provenance")
                    data, scene_rate = sf.read(cached, dtype="float32")
                    if data.ndim != 1 or not data.size or not np.isfinite(data).all() or not .001 < np.max(np.abs(data)) < .999:
                        raise ValueError("Prebuilt utterance must be finite unclipped mono speech")
                    key, hit = spec['audio_sha256'], True
                    cache_hits += 1
                elif cached.exists():
                    try:
                        candidate, candidate_rate = sf.read(cached, dtype="float32")
                        if (candidate.ndim == 1 and candidate.size and np.isfinite(candidate).all()
                                and .001 < np.max(np.abs(candidate)) < .999):
                            data, scene_rate, hit = candidate, candidate_rate, True
                            cache_hits += 1
                    except (RuntimeError, OSError):
                        pass
                if data is None:
                    if tts is None:
                        tts = _create_tts(model_relative, cfg)
                    generated = tts.generate(text, sid=int(cfg["speaker_id"]), speed=float(cfg["speed"]))
                    data = np.asarray(generated.samples, dtype=np.float32)
                    scene_rate = generated.sample_rate
                    if (data.ndim != 1 or not data.size or not np.isfinite(data).all()
                            or not (.001 < np.max(np.abs(data)) < .999)):
                        raise RuntimeError(f"Empty, invalid, or clipping TTS output: {item['id']} unit {unit_index+1}")
                    temporary = cache / f"{key}.{os.getpid()}.tmp.wav"
                    sf.write(temporary, data, scene_rate, subtype="PCM_24")
                    temporary.replace(cached)
                    cached.with_suffix(".json").write_text(json.dumps(identity, ensure_ascii=False, indent=2), encoding="utf-8")
                    # Always use the quantized cache samples, including on the first
                    # render, so cache replay cannot subtly change the waveform.
                    data, scene_rate = sf.read(cached, dtype="float32")
                if rate is None:
                    rate = scene_rate
                if scene_rate != rate:
                    raise RuntimeError("Narration segments have inconsistent sample rates")
                if modern:
                    unit_captions, alignment = _unit_captions(spec, offset_samples, len(data), rate)
                else:
                    unit_captions = _legacy_captions(item, offset_samples / rate, len(data) / rate)
                    alignment = "legacy_estimated" if item.get("caption_boundaries") is None else "legacy_editor_supplied"
                for caption in unit_captions:
                    caption["scene_id"], caption["unit_index"] = str(item["id"]), unit_index
                    length = caption["end"] - caption["start"]
                    if modern and not 2 <= length <= 5:
                        warnings.append({"scene_id": str(item["id"]), "unit_index": unit_index,
                                         "caption": caption["text"], "duration": length,
                                         "code": "caption_duration_outside_2_to_5s_advisory",
                                         "action": "Review readability; preserve full names and natural utterance context. For a long utterance use actual editorial caption boundaries, not automatic TTS cuts."})
                units.append({"index": unit_index, "start": offset_samples / rate,
                              "end": (offset_samples + len(data)) / rate, "duration": len(data) / rate,
                              "text": text, "display": spec["display"].strip(), "samples": len(data),
                              "sample_rate": rate, "cache_key": key, "cache_path": str(cached),
                              "cache_hit": hit, "caption_alignment_method": alignment})
                captions.extend(unit_captions)
                chunks.append(data)
                offset_samples += len(data)
                speech_samples += len(data)
                unit_count += 1
                if unit_index < len(specs) - 1:
                    unit_gap = round(rate * float(cfg["unit_gap_seconds"]))
                    chunks.append(np.zeros(unit_gap, dtype=np.float32))
                    offset_samples += unit_gap
            speech_start, speech_end = scene_start_samples / rate, offset_samples / rate
            gap_samples = round(rate * float(cfg["paragraph_gap_seconds"])) if scene_index < len(scenes)-1 else 0
            chunks.append(np.zeros(gap_samples, dtype=np.float32))
            offset_samples += gap_samples
            end = offset_samples / rate
            methods = {u["caption_alignment_method"] for u in units}
            scene_method = ("measured_units_with_editor_boundaries" if "editor_supplied_within_measured_unit" in methods
                            else "measured_synthesis_units") if modern else "legacy_estimated_or_editor_supplied"
            timeline.append({
                "id": str(item["id"]), "start": speech_start, "end": end,
                "speech_start": speech_start, "speech_end": speech_end,
                "duration": end - speech_start, "speech_duration": speech_end - speech_start,
                "synthesized_audio_seconds": speech_samples / rate, "scene_gap_seconds": gap_samples / rate,
                "speech": speech, "spoken_text": "".join(u["text"] for u in units),
                "display_text": "".join(u["display"] for u in units), "units": units,
                "cache_key": units[0]["cache_key"] if len(units) == 1 else None,
                "cache_keys": [u["cache_key"] for u in units], "captions": captions,
                "layout": item.get("layout"), "caption_alignment": scene_method,
                "caption_alignment_method": scene_method, "schema_version": 2 if modern else 1,
            })
            all_captions.extend(captions)
    paths = {name: work / filename for name, filename in {
        "voice_raw": "voice-raw.wav", "voice": "voice-clean.wav",
        "mix": "morning-mix.wav",
    }.items()}
    sf.write(paths["voice_raw"], np.concatenate(chunks), rate, subtype="PCM_24")
    duration = offset_samples / rate
    voice_filter = (
        "highpass=f=70,adeclick=t=4,lowpass=f=9000,"
        "acompressor=threshold=0.125:ratio=2:attack=15:release=120:makeup=1,"
        f"loudnorm=I={float(cfg['voice_lufs'])}:TP={float(cfg['voice_true_peak_db'])}:LRA=7"
    )
    if external:
        # Preserve the imported generator's consonants and vocal dynamics.
        # Do not inherit the old model's de-click, low-pass and compression.
        voice_filter = f"highpass=f=60,loudnorm=I={float(cfg['voice_lufs'])}:TP={float(cfg['voice_true_peak_db'])}:LRA=9"
    _run(ffmpeg, ["-y", "-v", "error", "-i", paths["voice_raw"], "-af", voice_filter, "-ar", 48000, "-c:a", "pcm_s24le", paths["voice"]])
    voice_info = sf.info(paths["voice"])
    if abs(voice_info.duration - duration) > .002:
        raise RuntimeError("Narration processing changed duration; do not pad or speed it up")
    normalize_mix = f"loudnorm=I={float(cfg['voice_lufs'])}:TP={float(cfg['voice_true_peak_db'])}:LRA=7"
    if cfg["bgm_enabled"]:
        paths.update(bgm_original=work / "morning-original.wav", bgm=work / "morning-bgm.wav")
        _morning_music(voice_info.frames, voice_info.samplerate, float(cfg["bgm_bpm"]), paths["bgm_original"])
        _run(ffmpeg, ["-y", "-v", "error", "-i", paths["bgm_original"], "-af", f"highpass=f=45,lowpass=f=10000,loudnorm=I={float(cfg['bgm_lufs'])}:TP=-4:LRA=6", "-ar", 48000, "-c:a", "pcm_s24le", paths["bgm"]])
        mix_filter = (
            f"[0:a]asplit=2[v][sc];[1:a][sc]sidechaincompress=threshold={float(cfg['duck_threshold'])}:ratio={float(cfg['duck_ratio'])}:attack=45:release=350[bg];"
            "[v]pan=stereo|c0=c0|c1=c0[vs];[vs][bg]amix=inputs=2:duration=first:normalize=0,"
            f"{normalize_mix}[a]"
        )
        _run(ffmpeg, ["-y", "-v", "error", "-i", paths["voice"], "-i", paths["bgm"], "-filter_complex", mix_filter, "-map", "[a]", "-ar", 48000, "-c:a", "pcm_s24le", paths["mix"]])
    else:
        # No music synthesis, input, ducking, or implicit stale-file reuse.
        # Normalize after mono-to-stereo conversion to account for its +3 LU.
        mix_filter = f"pan=stereo|c0=c0|c1=c0,{normalize_mix}"
        _run(ffmpeg, ["-y", "-v", "error", "-i", paths["voice"], "-af", mix_filter,
                      "-ar", 48000, "-c:a", "pcm_s24le", paths["mix"]])
    click_events = []
    click_report = {"enabled": bool(cfg['transition_click_enabled']),
                    "kind": "original synthesized mouse click", "events": click_events,
                    "start_offset_seconds": click_offset,
                    "placement": "before_page" if click_offset < 0 else "after_page"}
    if transition_asset is not None:
        click_report.update(transition_asset[1])
    if cfg['transition_click_enabled']:
        mix_signal, mix_rate = sf.read(paths['mix'], dtype="float64", always_2d=True)
        if mix_rate != 48000 or mix_signal.shape[1] != 2:
            raise RuntimeError("Transition clicks require the existing 48 kHz stereo mix")
        if transition_asset is not None:
            click, _ = transition_asset
            peak_db = click_report['sample_peak_dbfs']
        else:
            peak_db = float(cfg['transition_click_peak_dbfs'])
            if not -36 <= peak_db <= -12:
                raise ValueError('Transition click peak must be between -36 and -12 dBFS')
            # Historical fallback for profiles without a pinned sample.
            rng = np.random.default_rng(20260911)
            t = np.arange(round(.092*mix_rate))/mix_rate
            click = np.zeros_like(t)
            for delay, level in [(0., 1.), (.038, .52)]:
                tau = np.maximum(t-delay, 0)
                noise = rng.standard_normal(len(t))
                bright = noise - np.r_[0., noise[:-1]]*.72
                pulse = (.65*bright + .22*np.sin(2*np.pi*1650*tau) + .13*np.sin(2*np.pi*2900*tau))
                click += (t>=delay)*level*pulse*np.exp(-tau/.006)*(1-np.exp(-tau/.00035))
            click *= 10**(peak_db/20)/max(np.max(np.abs(click)),1e-9)
            click = click[:, None]
            click_report.update(sample_rate=mix_rate, channels=1, samples=len(click),
                                duration=len(click)/mix_rate, sample_peak_dbfs=peak_db)
        stereo_click = np.repeat(click, 2, axis=1) if click.shape[1] == 1 else click
        for scene_index, (scene, segment) in enumerate(zip(scenes, timeline)):
            if scene.get('layout') != 'news': continue
            start = round((segment['start'] + click_offset)*mix_rate)
            end = start + len(click)
            page_start = round(segment['start']*mix_rate)
            scene_end = round(segment['end']*mix_rate)
            previous_speech_end = timeline[scene_index-1]['speech_end'] if scene_index else None
            if start < 0 or end > min(scene_end, len(mix_signal)):
                raise ValueError(f"Transition click does not fit without truncation at scene {scene['id']}")
            if click_offset < 0:
                if previous_speech_end is None:
                    raise ValueError(f"A before-page click needs a preceding speech gap: {scene['id']}")
                previous_end_sample = round(previous_speech_end*mix_rate)
                if start < previous_end_sample:
                    raise ValueError(f"Before-page click overlaps the preceding speech: {scene['id']}")
                if end >= page_start:
                    raise ValueError(f"Before-page click must finish strictly before the next scene: {scene['id']}")
            n = min(len(click), len(mix_signal)-start)
            mix_signal[start:start+n] += stereo_click[:n]
            event = {'scene_id':scene['id'], 'start':start/mix_rate,
                     'scene_start': segment['start'], 'start_offset_seconds': click_offset,
                     'actual_offset_seconds': start/mix_rate - segment['start'],
                     'end': end/mix_rate, 'gap_before_page_seconds': page_start/mix_rate - end/mix_rate,
                     'previous_speech_end': previous_speech_end,
                     'duration':n/mix_rate, 'sample_peak_dbfs':peak_db}
            if transition_asset is None:
                event['requested_peak_dbfs'] = peak_db
            else:
                event.update(asset_sha256=click_report['asset_sha256'], gain_db=click_report['gain_db'])
            click_events.append(event)
        sf.write(paths['mix'],mix_signal,mix_rate,subtype='PCM_24')
        click_path = work/'transition-click.wav'
        # A floating-point evidence copy avoids adding quantization to the
        # imported waveform; the established final mix remains PCM_24.
        sf.write(click_path,click,mix_rate,subtype='DOUBLE')
        click_report.update(rendered_sample_path=str(click_path), rendered_sample_sha256=_sha256(click_path))
    metrics = {name: _metrics(path) for name, path in paths.items()}
    for name in ["voice", "mix"] + (["bgm"] if cfg["bgm_enabled"] else []):
        metrics[name].update(_integrated_loudness(ffmpeg, paths[name]))
        _run(ffmpeg, ["-v", "error", "-i", paths[name], "-f", "null", "-"])
        if abs(metrics[name]["duration"] - duration) > .01:
            raise RuntimeError(f"Duration drift in {name}")
    duration_pass = float(cfg["min_seconds"]) <= duration <= float(cfg["max_seconds"])
    modern_only = all(modern for _, modern, _ in prepared)
    editor_boundaries = any(c["alignment_method"] == "editor_supplied_within_measured_unit" for c in all_captions)
    alignment_method = ("measured_units_with_editor_boundaries" if editor_boundaries else "measured_synthesis_units") if modern_only else "contains_legacy_estimated_or_editor_supplied"
    timing_valid = all(0 <= c["start"] < c["end"] <= duration + 1/rate for c in all_captions)
    timing_valid = timing_valid and all(a["end"] <= b["start"] + 1/rate for a,b in zip(all_captions,all_captions[1:]))
    loudness_pass = abs(metrics["mix"]["integrated_lufs"] - float(cfg["voice_lufs"])) <= 1.0
    true_peak_pass = metrics["mix"]["true_peak_dbtp"] <= float(cfg["voice_true_peak_db"]) + .2
    result = {
        "pipeline_version": PIPELINE_VERSION, "duration": duration, "timeline": timeline,
        "captions": all_captions, "caption_alignment": alignment_method,
        "caption_alignment_method": alignment_method,
        "paths": {name: str(path) for name, path in paths.items()},
        "quality": {
            "duration_pass": duration_pass, "min_seconds": float(cfg["min_seconds"]),
            "max_seconds": float(cfg["max_seconds"]), "audio_decode": "passed",
            "no_clipped_samples": True, "duration_preserved": True,
            "stereo_mix": metrics["mix"]["channels"] == 2,
            "needs_copy_revision": not duration_pass,
            "caption_alignment_pass": modern_only and timing_valid,
            "caption_alignment_method": alignment_method,
            "caption_timing_valid": timing_valid,
            "caption_boundary_review_required": editor_boundaries,
            "caption_semantic_review_required": True,
            "caption_duration_advisories": len(warnings),
            "loudness_pass": loudness_pass, "true_peak_pass": true_peak_pass,
            "music_enabled": cfg["bgm_enabled"],
            "listening_review": False,
            "limits": "Measured signal/decode and synthesis-unit sample boundaries only; unit windows include model leading/trailing silence. Editorial caption boundaries need review. No word-level forced alignment, pronunciation guarantee, human listening, or matching-reference-voice claim.",
        },
        "metrics": metrics, "config": {k: cfg[k] for k in DEFAULTS},
        "filters": {"voice": voice_filter, "mix": mix_filter},
        "cache": {"hits": cache_hits, "scenes": len(scenes), "units": unit_count, "model_sha256": model_id, "engine_version": engine_version},
        "narration_engine": cfg['engine'],
        "narration_provenance": ([{"scene_id":s['id'],"unit_index":i,"sha256":u['audio_sha256'],"provenance":u['audio_provenance']} for s in scenes for i,u in enumerate(s['speech_units'])] if external else []),
        "warnings": warnings,
        "music": {"enabled": cfg["bgm_enabled"], "generated": cfg["bgm_enabled"], "mixed": cfg["bgm_enabled"],
                  "reference_bgm_confirmed": False,
                  "decision": "Music is off by default for clear narration because reference BGM was not established; the existing local 104 BPM arrangement is an explicit optional choice."},
        "transition_sound": click_report,
        "composition": ("Optional original local major-key plucked strings, keys, bass and light percussion; no third-party song"
                        if cfg["bgm_enabled"] else "Narration only; no BGM generated or mixed"),
        "seconds_to_build": time.perf_counter() - started,
    }
    (work / "audio-report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenes", required=True, help="JSON list, or object with a scenes array")
    parser.add_argument("--root", required=True)
    parser.add_argument("--work", required=True)
    parser.add_argument("--config", help="Optional JSON config file")
    arguments = parser.parse_args()
    payload = json.loads(Path(arguments.scenes).read_text(encoding="utf-8-sig"))
    scenes = payload["scenes"] if isinstance(payload, dict) else payload
    # Existing episode snapshots did not include IDs; preserve their order.
    scenes = [dict(item, id=item.get("id", f"scene-{index+1:02}")) for index, item in enumerate(scenes)]
    config = json.loads(Path(arguments.config).read_text(encoding="utf-8-sig")) if arguments.config else None
    built = build_audio(scenes, arguments.root, arguments.work, config)
    print(json.dumps({"duration": built["duration"], "quality": built["quality"], "cache": built["cache"], "paths": built["paths"], "seconds_to_build": built["seconds_to_build"]}, ensure_ascii=False, indent=2))
