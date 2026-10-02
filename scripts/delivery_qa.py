"""Inspect the encoded delivery and prepare evidence for a separate visual review.

Only technical signal/timing checks are automated. Frames, contact sheets and
their hashes are evidence to inspect, never proof of facts, beauty or speech.
The original video is read-only; all derived files stay below work/final-qa.
"""
import concurrent.futures
import hashlib
import json
import math
import subprocess
from collections import Counter
from pathlib import Path

import imageio_ffmpeg
from PIL import Image, ImageDraw

from audio_pipeline import _integrated_loudness, _metrics


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _decoded_video_measurements(video, media, video_sha256, fps):
    """Reuse only a complete decode of these exact bytes, never pending metrics."""
    measured = media.get("metrics", {})
    frames = measured.get("frame_count")
    duration = measured.get("video_frame_duration")
    if (measured.get("decode") == "passed"
            and measured.get("sha256") == video_sha256
            and type(frames) is int and frames > 0
            and type(duration) in (int, float) and math.isfinite(duration)
            and duration > 0 and math.isclose(duration, frames / fps, abs_tol=1e-6)
            and measured.get("fps") == fps):
        return frames, duration, "same-video verified complete decode"
    frames, duration = imageio_ffmpeg.count_frames_and_secs(str(video))
    return frames, duration, "complete decode during final QA"


def _check_clicks(episode, audio, media, profile):
    """Check declared mix events against both measured scene and frame timing.

    This is timing evidence, not acoustic recognition of a click in the AAC.
    Signal integrity of the encoded audio is measured separately.
    """
    enabled = bool(profile.get("transition_sound", {}).get("enabled", False))
    transition = audio.get("transition_sound", {})
    events = transition.get("events", [])
    result = {"enabled": enabled, "passed": True, "errors": [],
              "event_count": len(events), "events": [],
              "method": "mix event log compared with audio scene and video frame boundaries; not acoustic click recognition"}
    if not enabled:
        return result
    errors = result["errors"]
    scenes, timeline = episode.get("scenes", []), audio.get("timeline", [])
    fps = float(media.get("metrics", {}).get("fps", profile.get("video", {}).get("fps", 30)))
    if not math.isfinite(fps) or fps <= 0:
        result.update(passed=False, errors=["Invalid video fps for click timing"])
        return result
    tolerance = 1 / fps
    offset = float(profile.get("transition_sound", {}).get("start_offset_seconds", 0))
    if not math.isfinite(offset):
        result.update(passed=False, errors=["Invalid transition sample start offset"])
        return result
    expected = [str(scene["id"]) for scene in scenes if scene.get("layout") == "news"]
    result.update(expected_event_count=len(expected), tolerance_seconds=tolerance,
                  expected_start_offset_seconds=offset,
                  placement="before_page" if offset < 0 else "after_page")
    if not transition.get("enabled"):
        errors.append("Profile requires mouse clicks but the audio report has them disabled")
    actual = [str(event.get("scene_id", "")) for event in events]
    if Counter(actual) != Counter(expected):
        errors.append("Each news scene requires exactly one click and other scenes require none")
    boundaries = media.get("metrics", {}).get("scene_frame_boundaries", [])
    if len(boundaries) != len(scenes) + 1:
        errors.append("Video frame boundary count does not match the episode scenes")
    if [str(row.get("id")) for row in timeline] != [str(row.get("id")) for row in scenes]:
        errors.append("Audio scene order does not match the episode")
    by_id = {str(row.get("id")): row for row in timeline}
    indexes = {str(scene["id"]): i for i, scene in enumerate(scenes)}
    for event in events:
        sid = str(event.get("scene_id", ""))
        if sid not in expected or sid not in by_id or indexes[sid] >= len(boundaries):
            errors.append(f"Cannot bind click to both audio and video news scene: {sid}")
            continue
        try:
            start = float(event["start"])
            duration = float(event["duration"])
            end = start + duration
            audio_start = float(by_id[sid]["start"])
            video_start = float(boundaries[indexes[sid]]) / fps
            audio_delta = abs(start - (audio_start + offset))
            video_delta = abs(start - (video_start + offset))
            valid = duration > 0 and all(math.isfinite(v) for v in (start, duration, end, audio_delta, video_delta))
            valid = valid and max(audio_delta, video_delta) <= tolerance + 1e-9
            event_errors = []
            if not valid:
                event_errors.append("not within one frame of the configured boundary offset")
            declared_end = float(event.get("end", end))
            if not math.isfinite(declared_end) or abs(declared_end - end) > 1e-9:
                event_errors.append("declared event end differs from start plus full duration")
            previous_speech_end = None
            if offset < 0:
                index = indexes[sid]
                if index == 0 or index > len(timeline) - 1:
                    event_errors.append("no preceding scene with a speech gap")
                else:
                    previous_speech_end = float(timeline[index-1]["speech_end"])
                    # Ordering has no one-frame tolerance: even a fraction of
                    # a frame of overlap contradicts 'finish click, then page'.
                    if not math.isfinite(previous_speech_end) or start < previous_speech_end:
                        event_errors.append("click starts before the preceding speech has ended")
                    if not end < audio_start:
                        event_errors.append("click does not finish strictly before the next audio scene")
                    if not end < video_start:
                        event_errors.append("click does not finish strictly before the first video frame of the next scene")
            elif end > float(by_id[sid]["end"]):
                event_errors.append("click extends past its scene end")
            valid = valid and not event_errors
        except (KeyError, ValueError, TypeError):
            valid, audio_delta, video_delta = False, None, None
            end, video_start, previous_speech_end = None, None, None
            event_errors = ["invalid or missing click timing values"]
        result["events"].append({"scene_id": sid, "audio_error_seconds": audio_delta,
                                 "video_error_seconds": video_delta, "end": end,
                                 "video_page_start": video_start,
                                 "gap_before_video_page_seconds": video_start - end if end is not None else None,
                                 "previous_speech_end": previous_speech_end,
                                 "passed": valid, "errors": event_errors})
        if not valid:
            errors.append(f"Invalid click sequence for {sid}: {'; '.join(event_errors)}")
    result["passed"] = not errors
    return result


def _frame_points(episode, audio, media, profile, frame_count, fps):
    """Choose scene-bounded frames, marking when a before/after sample is impossible."""
    scenes, timeline = episode["scenes"], audio["timeline"]
    boundaries = media["metrics"]["scene_frame_boundaries"]
    if len(scenes) != len(timeline) or len(boundaries) != len(scenes) + 1:
        raise ValueError("Episode, audio and video scene boundaries do not agree")
    points = []
    media_fade = float(profile.get("visual", {}).get("media_fade_seconds", .3))
    page_fade = float(profile.get("visual", {}).get("page_fade_seconds", .25))

    def add(label, requested, scene_index, purpose, relation=None, boundary=None):
        first = int(boundaries[scene_index])
        last = min(frame_count - 1, int(boundaries[scene_index + 1]) - 1)
        if first < 0 or last < first:
            raise ValueError(f"Invalid or empty video scene interval: {scene_index}")
        frame = min(last, max(first, int(round(requested * fps))))
        actual = frame / fps
        row = {"label": label, "scene_id": str(scenes[scene_index]["id"]), "purpose": purpose,
               "requested_time": requested, "time": actual, "frame": frame,
               "scene_first_frame": first, "scene_last_frame": last,
               "clamped_to_scene": frame != int(round(requested * fps))}
        if relation:
            row["requested_relation"] = relation
            row["relation_available"] = actual < boundary if relation == "before" else actual >= boundary
            if not row["relation_available"]:
                row["note"] = "The requested before/after phase is outside this scene; review the bounded edge frame instead."
        points.append(row)
        return row

    for i, (scene, segment) in enumerate(zip(scenes, timeline)):
        if str(scene["id"]) != str(segment["id"]):
            raise ValueError("Episode and audio scene order do not agree")
        scene_start, scene_end = float(segment["start"]), float(segment["end"])
        duration = scene_end - scene_start
        if duration <= 0:
            raise ValueError(f"Nonpositive scene duration: {scene['id']}")
        for asset in scene.get("media", []):
            start, end = scene_start + float(asset["start"]), scene_start + float(asset["end"])
            if not scene_start <= start < end <= scene_end + 1e-6:
                raise ValueError(f"Media timing is outside its scene: {asset['id']}")
            prefix = f"{scene['id']}/{asset['id']}"
            add(prefix + "/before", start - .1, i, "media entry", "before", start)
            add(prefix + "/middle", (start + end) / 2, i, "media content")
            add(prefix + "/after", end + media_fade + 2 / fps, i, "media exit", "after", end)
            if asset.get("type") == "video":
                # Fractions also work for very short clips and never reverse early/late.
                add(prefix + "/motion-early", start + (end - start) * .2, i, "video motion early")
                add(prefix + "/motion-late", start + (end - start) * .8, i, "video motion late")
        if scene.get("layout") == "overview":
            for name, proportion in (("first", .08), ("middle", .5), ("last", .92)):
                add(f"{scene['id']}/{name}", scene_start + duration * proportion, i, "overview navigation and scrolling")
        if scene.get("layout") in ("closing", "outro") or i == len(scenes) - 1:
            add(f"{scene['id']}/ending", scene_end - min(.45, duration * .15), i, "ending and completed navigation")
        if i:
            add(f"{scene['id']}/after-transition", scene_start + min(page_fade + .25, duration * .5), i,
                "new scene, navigation and captions after page transition")
    if profile.get("transition_sound", {}).get("enabled") and float(profile.get("transition_sound", {}).get("start_offset_seconds", 0)) < 0:
        indexes = {str(scene['id']): i for i, scene in enumerate(scenes)}
        for event in audio.get("transition_sound", {}).get("events", []):
            sid = str(event['scene_id'])
            index = indexes[sid]
            if not index:
                raise ValueError(f"Cannot inspect a before-page click without a previous scene: {sid}")
            start, end = float(event['start']), float(event['start']) + float(event['duration'])
            page_start = int(boundaries[index]) / fps
            for label, low, high in (("during-click-previous-page", start, end),
                                     ("after-click-before-page", end, page_start)):
                first = max(int(boundaries[index-1]), math.ceil(low * fps))
                last = min(int(boundaries[index]) - 1, math.ceil(high * fps) - 1)
                requested = ((first + last) // 2) / fps if first <= last else (low + high) / 2
                row = add(f"{sid}/{label}", requested, index-1,
                          "previous page must remain visible until the entire click has finished")
                row.update(transition_scene_id=sid, phase_interval_start=low, phase_interval_end=high,
                           relation_available=low <= row['time'] < high)
                if not row['relation_available']:
                    row['note'] = "No distinct video-frame timestamp lies within this click phase; inspect the bounded edge and timing evidence."
            segment = timeline[index]
            duration = float(segment['end']) - float(segment['start'])
            row = add(f"{sid}/page-after-complete-click", page_start + min(page_fade + .05, duration * .5), index,
                      "new page appears only after the entire click and a visible preceding-page gap")
            row.update(transition_scene_id=sid, click_end=end, relation_available=row['time'] > end)
    return points


def inspect_delivery(episode, audio, media, work, profile):
    """Measure an encoded MP4 and return technical results plus review evidence.

    ``media`` is the render_video return value / video-report.json dictionary.
    Failures are recorded in ``errors`` and make ``technical_pass`` false. A
    successful result still requires a human or agent to inspect the visuals.
    """
    output = Path(work).resolve() / "final-qa"
    output.mkdir(parents=True, exist_ok=True)
    video = Path(media["video_path"]).resolve()
    if output == video.parent or output in video.parents:
        raise ValueError("The original video must not be inside the QA output directory")
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    config = profile.get("delivery_qa", {})
    voice = profile.get("voice", {})
    report = {"schema_version": 1, "technical_pass": False, "errors": [], "video_path": str(video),
              "video_sha256": _sha256(video), "audio_metrics": {}, "frames": [], "contact_sheets": [],
              "visual_review": "pending_human_or_agent_inspection", "listening_review": False,
              "limits": "Technical checks and extracted images do not verify facts, visual quality, pronunciation or naturalness. ASR and visual/listening review remain separate requirements."}
    errors = report["errors"]
    report["click_check"] = _check_clicks(episode, audio, media, profile)
    errors.extend(report["click_check"]["errors"])
    reader = imageio_ffmpeg.read_frames(str(video))
    try:
        metadata = next(reader)
    finally:
        reader.close()
    fps = float(metadata["fps"])
    if not math.isfinite(fps) or fps <= 0:
        raise ValueError("Cannot inspect delivery without a valid encoded video frame rate")
    frame_count, video_seconds, decode_source = _decoded_video_measurements(
        video, media, report["video_sha256"], fps)
    report["encoded_video"] = {"fps": fps, "frame_count": frame_count, "duration": video_seconds,
                               "decode_evidence": decode_source,
                               "source_size": metadata.get("source_size"),
                               "video_codec": metadata.get("codec"), "audio_codec": metadata.get("audio_codec")}
    if not frame_count:
        errors.append("Encoded video has no decodable frames")
    declared_fps = float(media.get("metrics", {}).get("fps", fps))
    if abs(declared_fps - fps) > 1e-6:
        errors.append("Encoded video fps does not match its timing report")
    if frame_count != int(media.get("metrics", {}).get("frame_count", frame_count)):
        errors.append("Encoded video frame count does not match its timing report")
    decoded = output / "final-aac-decoded.wav"
    try:
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
                        "-vn", "-c:a", "pcm_f32le", str(decoded)], check=True, capture_output=True)
        metrics = {**_metrics(decoded), **_integrated_loudness(ffmpeg, video)}
        metrics["duration_difference_seconds"] = abs(metrics["duration"] - float(audio["duration"]))
        metrics["video_duration_difference_seconds"] = abs(metrics["duration"] - frame_count / fps)
        report["audio_metrics"] = metrics
        target = float(voice.get("target_lufs", -16))
        lufs_tolerance = float(config.get("loudness_tolerance_lu", 1))
        peak_limit = float(voice.get("true_peak_db", -2)) + float(config.get("true_peak_tolerance_db", 1))
        duration_limit = float(config.get("duration_tolerance_seconds", max(.08, 1 / fps)))
        report["thresholds"] = {"target_lufs": target, "loudness_tolerance_lu": lufs_tolerance,
                                "true_peak_max_dbtp": peak_limit, "duration_tolerance_seconds": duration_limit}
        for name in ("integrated_lufs", "true_peak_dbtp", "duration"):
            if not math.isfinite(metrics[name]):
                errors.append(f"Encoded audio has nonfinite {name}")
        if abs(metrics["integrated_lufs"] - target) > lufs_tolerance:
            errors.append("Encoded AAC loudness is outside the configured tolerance")
        if metrics["true_peak_dbtp"] > peak_limit:
            errors.append("Encoded AAC true peak exceeds the configured ceiling")
        if max(metrics["duration_difference_seconds"], metrics["video_duration_difference_seconds"]) > duration_limit:
            errors.append("Encoded AAC duration does not match the measured audio/video timing")
    except (RuntimeError, subprocess.CalledProcessError, ValueError) as exc:
        errors.append(f"Encoded AAC signal check failed: {exc}")

    try:
        points = _frame_points(episode, audio, media, profile, frame_count, fps)

        def extract(pair):
            index, row = pair
            target = output / f"frame-{index:03}.jpg"
            subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", str(row["time"]),
                            "-i", str(video), "-frames:v", "1", "-q:v", "2", str(target)],
                           check=True, capture_output=True)
            row.update(path=str(target), sha256=_sha256(target))
            return row

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            report["frames"] = list(pool.map(extract, enumerate(points)))
        for offset in range(0, len(points), 12):
            page = report["frames"][offset:offset + 12]
            canvas = Image.new("RGB", (1920, math.ceil(len(page) / 3) * 404), "white")
            draw = ImageDraw.Draw(canvas)
            for index, row in enumerate(page):
                x, y = index % 3 * 640, index // 3 * 404
                with Image.open(row["path"]) as source:
                    source.thumbnail((640, 360))
                    canvas.paste(source, (x + (640 - source.width) // 2, y + (360 - source.height) // 2))
                draw.text((x + 5, y + 363), f"{offset + index:03} {row['time']:.3f}s  {row['label']}", fill="black")
                if row.get("relation_available") is False:
                    draw.text((x + 5, y + 380), "Requested phase unavailable within scene; bounded edge shown", fill="#9A4B00")
            target = output / f"contact-{offset // 12:02}.jpg"
            canvas.save(target, quality=93)
            report["contact_sheets"].append({"path": str(target), "sha256": _sha256(target)})
    except (RuntimeError, subprocess.CalledProcessError, ValueError, OSError) as exc:
        errors.append(f"Final-frame evidence extraction failed: {exc}")
    if _sha256(video) != report["video_sha256"]:
        errors.append("The original video changed during delivery inspection")
    report["technical_pass"] = not errors
    index_path = output / "index.json"
    index_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report.update(index_path=str(index_path), index_sha256=_sha256(index_path))
    return report
