"""Data-driven renderer for the approved AI morning-news visual profile.

This module does not fetch news, synthesize speech, or declare editorial approval.
All generated media stays in the caller's work directory.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
import hashlib
import json
import math
import re
import shutil
import subprocess
import time

import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFont


class LayoutError(ValueError):
    """The copy cannot be shown legibly inside the selected layout."""


def _run(ff, args, work):
    result = subprocess.run(
        [str(ff), "-hide_banner", "-loglevel", "error", *map(str, args)],
        cwd=str(work), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode:
        raise RuntimeError(result.stderr[-12000:])
    return result


def _resolve(root, name):
    path = Path(name)
    return path if path.is_absolute() else Path(root) / path


def _text_lines(value):
    if value is None:
        return []
    if isinstance(value, str):
        return value.splitlines()
    return [str(x) for x in value]


class Page:
    def __init__(self, runtime_root, profile):
        self.profile = profile
        self.v = profile.get("visual", {})
        self.bg = self.v.get("background", "#F4F1E9")
        self.ink = self.v.get("ink", "#202321")
        self.accent = self.v.get("accent", "#AE372C")
        self.muted = self.v.get("muted", "#676962")
        self.font_paths = {
            "sans": _resolve(runtime_root, profile["font_sans"]),
            "serif": _resolve(runtime_root, profile["font_serif"]),
        }
        self.image = Image.new("RGB", (1080, 1920), self.bg)
        self.draw = ImageDraw.Draw(self.image)
        self.font_cache = {}
        self.boxes = []

    def font(self, size, style="sans"):
        key = (int(size), style)
        if key not in self.font_cache:
            self.font_cache[key] = ImageFont.truetype(str(self.font_paths[style]), int(size))
        return self.font_cache[key]

    def measured(self, text, size, style="sans"):
        return self.font(size, style).getbbox(str(text), anchor="lt")

    def text(self, box, value, size=44, min_size=None, color=None, style="sans",
             max_lines=1, wrap=False, line_gap=20, tag="text"):
        """Fit within a real rectangle, refusing unreadable fallback sizes."""
        x, y, right, bottom = box
        if not (0 <= x < right <= 1080 and 0 <= y < bottom <= 1920):
            raise LayoutError(f"Invalid {tag} box: {box}")
        given = _text_lines(value)
        if not given:
            return
        minimum = int(size if min_size is None else min_size)
        chosen = None
        for current in range(int(size), minimum - 1, -1):
            lines = []
            for original in given:
                if not wrap:
                    lines.append(original)
                    continue
                line = ""
                for char in original:
                    trial = line + char
                    bb = self.measured(trial, current, style)
                    if bb[2] - bb[0] > right - x and line:
                        lines.append(line.rstrip())
                        line = char.lstrip()
                    else:
                        line = trial
                if line:
                    lines.append(line.rstrip())
            if len(lines) > max_lines:
                continue
            sizes = [self.measured(line, current, style) for line in lines]
            heights = [bb[3] - bb[1] for bb in sizes]
            if (all(bb[2] - bb[0] <= right - x for bb in sizes)
                    and sum(heights) + max(0, len(lines) - 1) * line_gap <= bottom - y):
                chosen = (current, lines, sizes)
                break
        if chosen is None:
            raise LayoutError(
                f"{tag} does not fit at readable size >= {minimum}px: {value!r}. "
                "Shorten the text or choose a different layout."
            )
        current, lines, sizes = chosen
        for line, bb in zip(lines, sizes):
            self.draw.text((x - bb[0], y - bb[1]), line, font=self.font(current, style),
                           fill=color or self.ink, anchor="lt")
            actual = (x, y, x + bb[2] - bb[0], y + bb[3] - bb[1])
            self.boxes.append({"tag": tag, "text": line, "font_size": current, "bbox": actual})
            y += bb[3] - bb[1] + line_gap

    def line(self, coordinates, color=None, width=2):
        self.draw.line(coordinates, fill=color or self.ink, width=width)


def _render_page(scene, index, episode, runtime_root, profile):
    page = Page(runtime_root, profile)
    p, v = page, scene.get("visual", {})
    episode_date = date.fromisoformat(episode["date"])
    total = len(episode["scenes"])
    p.draw.rectangle((64, 80, 72, 132), fill=p.accent)
    p.text((95, 80, 640, 144), "AI 早报", 46, style="serif", tag="masthead")
    p.text((778, 94, 1016, 141), episode_date.strftime("%m / %d"), 32, tag="date")
    p.line((64, 169, 1016, 169), width=3)
    p.text((65, 226, 825, 284), scene.get("section", "本日选读"), 32, tag="section")
    p.text((905, 226, 1016, 284), f"{index + 1:02}", 32, color=p.muted, tag="page")
    p.text((62, 342, 1016, 670), scene["head"], 106, min_size=80,
           style="serif", max_lines=2, line_gap=44, tag="headline")
    layout = scene["layout"]
    if layout == "cover":
        p.text((60, 725, 490, 1057), f"{episode_date.month:02}", 275, tag="cover-month")
        p.text((475, 872, 1000, 957), f"月 / {episode_date.day:02}日", 48,
               color=p.accent, tag="cover-day")
        p.line((67, 1086, 1003, 1086))
        items = v.get("items", [])
        if not 1 <= len(items) <= 3:
            raise LayoutError("cover.visual.items requires one to three items")
        step = 952 / len(items)
        for j, item in enumerate(items):
            x = round(68 + j * step)
            right = round(68 + (j + 1) * step - 25)
            p.text((x, 1140, right, 1205), item["label"], 38, min_size=34, tag="cover-label")
            p.text((x, 1220, right, 1317), item.get("detail", ""), 32, min_size=30,
                   wrap=True, max_lines=2, line_gap=10, color=p.muted, tag="cover-detail")
    elif layout in ("big_text", "stat"):
        preferred = 260 if layout == "stat" else 190
        minimum = 150 if layout == "stat" else 95
        p.text((65, 748, 1005, 1050), v["value"], preferred, min_size=minimum,
               color=p.accent, tag=f"{layout}-value")
        p.text((74, 1070, 1005, 1150), v.get("label", ""), 49, min_size=42, tag="value-label")
        p.line((74, 1190, 1005, 1190))
        p.text((74, 1220, 1005, 1320), v.get("lines", []), 36, min_size=34,
               max_lines=2, wrap=True, line_gap=15, color=p.muted, tag="value-detail")
    elif layout == "date_event":
        event = date.fromisoformat(v["date"])
        p.draw.rectangle((65, 735, 465, 1195), fill=p.accent)
        p.text((100, 765, 438, 837), f"{event.month}月", 44, color=p.bg, tag="event-month")
        p.text((96, 851, 448, 1137), f"{event.day:02}", 246, min_size=225,
               color=p.bg, tag="event-day")
        p.text((525, 803, 1005, 906), v.get("time", ""), 72, min_size=54, tag="event-time")
        p.text((525, 953, 1005, 1295), v.get("lines", []), 45, min_size=40,
               wrap=True, max_lines=4, line_gap=27, tag="event-detail")
    elif layout == "flow":
        items = v.get("items", [])
        if not 1 <= len(items) <= 3:
            raise LayoutError("flow.visual.items requires one to three items")
        step = 950 / len(items)
        for j, item in enumerate(items):
            label = item.get("label", "") if isinstance(item, dict) else str(item)
            x = round(70 + j * step)
            width = round(step - 65)
            p.draw.rectangle((x, 820, x + width, 1158), outline=p.ink, width=2)
            p.text((x + 24, 852, x + width - 20, 984), label, 40, min_size=35,
                   wrap=True, max_lines=2, tag="flow-label")
            for k in range(4):
                p.line((x + 29, 1020 + k * 26, x + width - 29 - (k % 2) * 32,
                        1020 + k * 26), color=p.muted)
            if j < len(items) - 1:
                p.text((x + width + 9, 971, x + round(step) - 5, 1031), "→", 38,
                       color=p.accent, tag="flow-arrow")
        p.text((70, 1240, 1005, 1320), v.get("label", ""), 38, min_size=34,
               color=p.muted, tag="flow-detail")
    elif layout == "conditions":
        p.text((65, 790, 520, 1280), v["value"], 170, min_size=110, color=p.accent,
               style="serif", max_lines=2, wrap=True, line_gap=42, tag="conditions-value")
        p.line((550, 789, 550, 1290))
        p.text((585, 814, 1005, 1305), v.get("lines", []), 43, min_size=38,
               max_lines=5, wrap=True, line_gap=48, tag="conditions-detail")
    elif layout == "closing":
        p.text((66, 762, 1005, 1110), v.get("value", "你最想试哪一个？"),
               122, min_size=95, style="serif", color=p.accent,
               max_lines=2, wrap=True, line_gap=45, tag="closing-value")
        p.line((70, 1183, 1000, 1183))
        p.text((70, 1230, 1005, 1320), v.get("lines", []), 42, min_size=36,
               max_lines=2, wrap=True, line_gap=15, tag="closing-detail")
    else:
        raise LayoutError(f"Unsupported layout: {layout!r}")
    p.line((65, 1350, 1015, 1350), color="#B9B7AF", width=1)
    p.text((67, 1380, 1015, 1464), scene.get("source_label", ""), 27,
           color=p.muted, wrap=True, max_lines=2, line_gap=12, tag="source-label")
    p.draw.rectangle((0, 1490, 1080, 1760), fill=p.v.get("subtitle_background", "#EAE6DD"))
    p.text((65, 1822, 820, 1877), "AI生成配音与画面", 25, color=p.muted, tag="disclosure")
    p.text((892, 1819, 1015, 1872), f"{index + 1} / {total}", 27, color=p.muted, tag="page-count")
    return p


def _subtitle_text(text, font):
    # User-controlled copy must never become an ASS override command.
    text = str(text).replace("\\", "＼").replace("{", "｛").replace("}", "｝")
    text = text.replace("\r", "").strip().rstrip('，。；')
    # Balanced two-line breaks avoid one-character orphan lines. Explicit
    # semantic line breaks provided by the editor always take precedence.
    if '\n' not in text and font.getlength(text)>900:
        options=[]
        for i in range(1,len(text)):
            left,right=text[:i].strip(),text[i:].strip()
            if len(left)<3 or len(right)<3:continue
            if text[i-1].isascii() and text[i].isascii() and text[i-1].isalnum() and text[i].isalnum():continue
            a,b=font.getlength(left),font.getlength(right)
            if a<=900 and b<=900:options.append((abs(a-b),left,right))
        if options:
            _,left,right=min(options)
            return left+'\n'+right
    lines = []
    for paragraph in text.split("\n"):
        line = ""
        for char in paragraph:
            trial = line + char
            bbox = font.getbbox(trial)
            if bbox[2] - bbox[0] > 900 and line:
                lines.append(line.rstrip())
                line = char.lstrip()
            else:
                line = trial
        if line:
            lines.append(line.rstrip())
    if not 1 <= len(lines) <= 2:
        raise LayoutError(f"Subtitle needs more than two readable lines: {text!r}")
    return "\n".join(lines)


def _stamp(seconds, ass=False):
    scale = 100 if ass else 1000
    ticks = round(seconds * scale)
    hours, ticks = divmod(ticks, 3600 * scale)
    minutes, ticks = divmod(ticks, 60 * scale)
    secs, ticks = divmod(ticks, scale)
    return (f"{hours}:{minutes:02}:{secs:02}.{ticks:02}" if ass
            else f"{hours:02}:{minutes:02}:{secs:02},{ticks:03}")


def _ass_color(value):
    value = value.lstrip("#")
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", value):
        raise ValueError("Colors must use #RRGGBB")
    return "&H00" + value[4:6] + value[2:4] + value[:2]


def _captions(audio, runtime_root, work, profile):
    visual = profile.get("visual", {})
    size = int(visual.get("subtitle_size", 57))
    if size < 57:
        raise LayoutError("Subtitle font must be at least the 57px baseline")
    font_path = _resolve(runtime_root, profile["font_sans"])
    font = ImageFont.truetype(str(font_path), size)
    fonts = work / "fonts"
    fonts.mkdir(parents=True, exist_ok=True)
    staged_font = fonts / font_path.name
    if not staged_font.exists() or staged_font.stat().st_size != font_path.stat().st_size:
        shutil.copy2(font_path, staged_font)
    name = font.getname()[0]
    ink = _ass_color(visual.get("ink", "#202321"))
    sub_bg = _ass_color(visual.get("subtitle_background", "#EAE6DD"))
    margin = int(visual.get("subtitle_margin_v", 235))
    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 2\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, "
        "Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{name},{size},{ink},{ink},{sub_bg},{sub_bg},0,0,0,0,100,100,0,0,1,0,0,2,80,80,{margin},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    entries, srt, rendered = [], [], []
    previous_end = -1.0
    for i, caption in enumerate(audio["captions"], 1):
        start, end = float(caption["start"]), float(caption["end"])
        if not (0 <= start < end <= float(audio["duration"]) + .002):
            raise ValueError(f"Invalid caption {i} interval {start}, {end}")
        if start < previous_end - .011:
            raise ValueError(f"Overlapping caption {i}")
        previous_end = end
        text = _subtitle_text(caption["text"], font)
        ass_text = text.replace("\n", r"\N")
        entries.append(f"Dialogue: 0,{_stamp(start, True)},{_stamp(end, True)},Default,,0,0,0,,{ass_text}")
        srt.append(f"{i}\n{_stamp(start)} --> {_stamp(end)}\n{text}")
        rendered.append({"start": start, "end": end, "text": text})
    if not rendered:
        raise ValueError("No captions supplied")
    ass_path, srt_path = work / "captions.ass", work / "字幕.srt"
    ass_path.write_text(header + "\n".join(entries) + "\n", encoding="utf-8")
    srt_path.write_text("\n\n".join(srt) + "\n", encoding="utf-8")
    return ass_path, srt_path, rendered


def render_video(episode, audio, runtime_root, work, profile):
    """Render a draft and technical QA artifacts; editorial release is the caller's job."""
    started = time.perf_counter()
    runtime_root, work = Path(runtime_root), Path(work)
    work.mkdir(parents=True, exist_ok=True)
    video = profile.get("video", {})
    if (int(video.get("width", 1080)), int(video.get("height", 1920))) != (1080, 1920):
        raise ValueError("This approved layout requires 1080x1920")
    fps = int(video.get("fps", 30))
    if fps != 30:
        raise ValueError("This baseline requires 30 fps")
    duration = float(audio["duration"])
    if not float(video.get("min_seconds", 50)) <= duration <= float(video.get("max_seconds", 70)):
        raise ValueError(f"Narration duration {duration:.2f}s is outside profile limits; revise the script")
    scenes, timeline = episode["scenes"], audio["timeline"]
    if not scenes or len(scenes) != len(timeline):
        raise ValueError("Scene and audio timeline counts differ")
    frame_boundaries = [0]
    previous_end = 0.0
    for scene, segment in zip(scenes, timeline):
        if scene["id"] != segment["id"]:
            raise ValueError("Scene IDs are not in audio timeline order")
        if abs(float(segment["start"]) - previous_end) > .002:
            raise ValueError("Audio timeline has gaps or overlaps")
        previous_end = float(segment["end"])
        frame_boundaries.append(round(previous_end * fps))
    if abs(previous_end - duration) > .002:
        raise ValueError("Audio timeline does not end at final mixed-audio duration")
    if any(b <= a for a, b in zip(frame_boundaries, frame_boundaries[1:])):
        raise ValueError("Every scene must occupy at least one frame")
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    ass, srt, captions = _captions(audio, runtime_root, work, profile)
    pages, layout_boxes = [], []
    for i, scene in enumerate(scenes):
        page = _render_page(scene, i, episode, runtime_root, profile)
        path = work / f"page-{i:02}.png"
        page.image.save(path)
        pages.append(path)
        layout_boxes.append({"id": scene["id"], "boxes": page.boxes})

    def encode_page(i):
        part = work / f"part-{i:02}.mp4"
        count = frame_boundaries[i + 1] - frame_boundaries[i]
        bg = profile.get("visual", {}).get("background", "#F4F1E9").lstrip("#")
        _run(ff, ["-y", "-loop", "1", "-framerate", fps, "-i", pages[i].name,
                  "-frames:v", count, "-vf", f"fade=t=in:st=0:d=0.18:color=0x{bg}",
                  "-an", "-c:v", "libx264", "-threads", "2", "-preset", "fast",
                  "-crf", video.get("crf", 19), "-pix_fmt", "yuv420p", part.name], work)
        return part

    with ThreadPoolExecutor(max_workers=2) as pool:
        parts = list(pool.map(encode_page, range(len(scenes))))
    (work / "concat.txt").write_text("\n".join(f"file '{p.name}'" for p in parts), encoding="ascii")
    final = work / f"AI早报_{episode['date']}_清晨轻快版.mp4"
    _run(ff, ["-y", "-f", "concat", "-safe", "0", "-i", "concat.txt", "-i", audio["paths"]["mix"],
              "-vf", f"ass={ass.name}:fontsdir=fonts", "-map", "0:v:0", "-map", "1:a:0",
              "-frames:v", frame_boundaries[-1], "-t", f"{duration:.9f}", "-c:v", "libx264",
              "-preset", "fast", "-crf", video.get("crf", 19), "-c:a", "aac", "-b:a", "256k",
              "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-metadata",
              "comment=AI generated narration and editorial graphics; original local background music", final.name], work)
    decoded = _run(ff, ["-i", final.name, "-progress", "pipe:1", "-f", "null", "-"], work)
    decoded_counts = re.findall(r"(?m)^frame=(\d+)$", decoded.stdout)
    if not decoded_counts or int(decoded_counts[-1]) != frame_boundaries[-1]:
        raise ValueError("Decoded frame count differs from the planned cumulative timeline")
    frames = []
    for i, segment in enumerate(timeline):
        start, end = float(segment["start"]), float(segment["end"])
        eligible = [c for c in captions if c["start"] >= start - .002 and c["end"] <= end + .002]
        if not eligible:
            raise ValueError(f"Scene {scenes[i]['id']} has no caption to inspect")
        # Inspect the longest caption, where clipping is most likely.
        caption = max(eligible, key=lambda c: len(c["text"]))
        stamp = (caption["start"] + caption["end"]) / 2
        frame = work / f"qa-{i:02}.png"
        _run(ff, ["-y", "-ss", f"{stamp:.6f}", "-i", final.name, "-frames:v", "1", frame.name], work)
        with Image.open(frame) as im:
            if im.size != (1080, 1920):
                raise ValueError("Decoded frame has unexpected dimensions")
        frames.append(str(frame))
    columns, rows = min(4, len(frames)), math.ceil(len(frames) / 4)
    contact = Image.new("RGB", (columns * 270, rows * 480), profile.get("visual", {}).get("background", "#F4F1E9"))
    for i, path in enumerate(frames):
        with Image.open(path) as im:
            contact.paste(im.resize((270, 480)), ((i % 4) * 270, (i // 4) * 480))
    contact_path = work / "contact.jpg"
    contact.save(contact_path, quality=92)
    cover = work / "封面.png"
    shutil.copy2(pages[0], cover)
    metrics = {
        "duration": duration, "video_frame_duration": frame_boundaries[-1] / fps,
        "duration_difference_seconds": abs(frame_boundaries[-1] / fps - duration),
        "width": 1080, "height": 1920, "fps": fps, "frame_count": int(decoded_counts[-1]),
        "scene_frame_boundaries": frame_boundaries, "scene_count": len(scenes),
        "caption_count": len(captions), "caption_alignment": audio.get("caption_alignment", "within-paragraph estimates; not forced aligned"),
        "layout_bounds": "passed", "decode": "passed", "visual_review": "pending_agent_inspection",
        "sha256": hashlib.sha256(final.read_bytes()).hexdigest(),
        "seconds_to_render": time.perf_counter() - started,
    }
    (work / "layout-boxes.json").write_text(json.dumps(layout_boxes, ensure_ascii=False, indent=2), encoding="utf-8")
    (work / "video-qa.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"video_path": str(final), "cover_path": str(cover), "srt_path": str(srt),
            "contact_path": str(contact_path), "frames": frames, "metrics": metrics}
