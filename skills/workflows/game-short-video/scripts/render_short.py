#!/usr/bin/env python3
"""Render a vertical 9:16 short from stills, narration files and captions.

Reads a project JSON (see the skill's assets/project.example.json), builds one
Ken Burns clip per scene, concatenates them, mixes the narration lines at their
scene starts, burns captions in with libass, and writes a single MP4.

Offline and deterministic: it never downloads anything and writes only to
--out plus a temporary work folder that is removed unless --keep-temp is given.
Requires an ffmpeg with the libass 'subtitles' filter; 'drawtext' is not used.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

DEFAULT_GAP = 0.65
MIN_SCENE = 3.4
LAST_PAD = 1.8
MIN_LAST = 4.2
OUTLINE = 9


class ProjectError(Exception):
    """Invalid project file or missing input."""


def die(msg: str, code: int = 2) -> "None":
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(code)


def run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-12:])
        raise RuntimeError(f"command failed: {cmd[0]} {cmd[1]}\n{tail}")
    return proc.stdout


def probe_duration(ffprobe: str, path: str) -> float:
    out = run([ffprobe, "-v", "error", "-show_entries", "format=duration",
               "-of", "json", path])
    try:
        value = float(json.loads(out)["format"]["duration"])
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        raise ProjectError(f"cannot read a duration from {path}: {exc}") from exc
    if value <= 0:
        raise ProjectError(f"{path} has no playable duration")
    return value


def ass_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int(seconds % 3600 // 60)
    s = seconds % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def ass_text(text: str) -> str:
    text = text.replace("\\", "\\\\")
    for ch in "{}":
        text = text.replace(ch, "\\" + ch)
    return text.replace("\r\n", "\n").replace("\n", r"\N")


def load_project(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise ProjectError(f"cannot read project file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ProjectError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ProjectError("project file must contain a JSON object")
    scenes = data.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise ProjectError("'scenes' must be a non-empty list")
    base = os.path.dirname(os.path.abspath(path))
    for index, scene in enumerate(scenes, 1):
        if not isinstance(scene, dict):
            raise ProjectError(f"scene {index} must be an object")
        for key in ("image", "audio"):
            value = scene.get(key)
            if not isinstance(value, str) or not value:
                raise ProjectError(f"scene {index} needs a '{key}' path")
            resolved = value if os.path.isabs(value) else os.path.join(base, value)
            if not os.path.isfile(resolved):
                raise ProjectError(f"scene {index}: '{key}' not found: {resolved}")
            scene[key] = resolved
        caption = scene.get("caption", "")
        if caption is None:
            caption = ""
        if not isinstance(caption, str):
            raise ProjectError(f"scene {index}: 'caption' must be a string")
        scene["caption"] = caption
    for key in ("width", "height", "fps"):
        if key in data and not isinstance(data[key], int):
            raise ProjectError(f"'{key}' must be an integer")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="render_short.py",
        description="Render a 9:16 captioned short from stills and narration files.",
        epilog="Exit codes: 0 rendered, 1 render failed, 2 invalid project or input.",
    )
    parser.add_argument("project", help="project JSON file")
    parser.add_argument("-o", "--out", help="output MP4 (default: <project>-short.mp4)")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg binary (default: ffmpeg)")
    parser.add_argument("--ffprobe", default=None, help="ffprobe binary (default: ffmpeg's sibling)")
    parser.add_argument("--fonts-dir", default=None, help="folder holding the caption font family")
    parser.add_argument("--font-family", default=None, help="override the caption font family")
    parser.add_argument("--workdir", default=None, help="folder for intermediate files")
    parser.add_argument("--crf", type=int, default=20, help="x264 CRF for the final render (default: 20)")
    parser.add_argument("--preset", default="medium", help="x264 preset for the final render")
    parser.add_argument("--dry-run", action="store_true", help="print the timeline, encode nothing")
    parser.add_argument("--keep-temp", action="store_true", help="keep intermediate files")
    parser.add_argument("--json", action="store_true", help="print one JSON summary object")
    args = parser.parse_args(argv)

    try:
        project = load_project(args.project)
    except ProjectError as exc:
        die(str(exc), 2)

    ffmpeg = args.ffmpeg if os.path.isabs(args.ffmpeg) else (shutil.which(args.ffmpeg) or "")
    if not args.dry_run and not (ffmpeg and os.path.isfile(ffmpeg)):
        die(f"ffmpeg not found: {args.ffmpeg}", 2)
    ffprobe = args.ffprobe
    if ffprobe is None:
        sibling = os.path.join(os.path.dirname(ffmpeg), "ffprobe") if ffmpeg else ""
        ffprobe = (sibling if sibling and os.path.isfile(sibling)
                   else (shutil.which("ffprobe") or "ffprobe"))

    width = int(project.get("width", 1080)) // 2 * 2
    height = int(project.get("height", 1920)) // 2 * 2
    fps = int(project.get("fps", 30))
    if not 1 <= fps <= 120:
        die("'fps' must be between 1 and 120", 2)
    gap = float(project.get("gap", DEFAULT_GAP))
    zoom_max = float(project.get("zoom", 1.10))
    if not 1.0 < zoom_max <= 1.6:
        die("'zoom' must be greater than 1.0 and at most 1.6", 2)
    font_family = args.font_family or project.get("font_family") or "Poppins"
    font_size = int(project.get("font_size", 80))
    caption_y = min(max(float(project.get("caption_y", 0.14)), 0.02), 0.6)
    scenes = project["scenes"]

    out = os.path.abspath(args.out or os.path.splitext(os.path.abspath(args.project))[0]
                          + "-short.mp4")
    out_dir = os.path.dirname(out)
    if out_dir and not os.path.isdir(out_dir):
        die(f"output folder does not exist: {out_dir}", 2)

    # Timeline: the narration is the clock.
    plan: list[dict] = []
    start = 0.0
    for index, scene in enumerate(scenes):
        try:
            audio_len = probe_duration(ffprobe, scene["audio"])
        except (ProjectError, RuntimeError) as exc:
            die(f"scene {index + 1}: {exc}", 2)
        if index == len(scenes) - 1:
            duration = max(audio_len + LAST_PAD, MIN_LAST)
        else:
            duration = max(audio_len + gap, MIN_SCENE)
        plan.append({"start": start, "duration": round(duration, 3),
                     "audio": audio_len, "scene": scene})
        start += duration
    total = round(start, 3)

    timeline = [
        {"scene": i + 1, "start": round(p["start"], 3), "duration": p["duration"],
         "caption": p["scene"]["caption"].replace("\n", " / "),
         "audio": round(p["audio"], 3)}
        for i, p in enumerate(plan)
    ]

    if args.dry_run:
        summary = {"output": out, "total": total, "width": width, "height": height,
                   "fps": fps, "timeline": timeline, "rendered": False}
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            print(f"total {total:.2f}s  {width}x{height} @ {fps}fps")
            for row in timeline:
                print(f"  {row['scene']:2d}  {row['start']:6.2f}s +{row['duration']:5.2f}s  "
                      f"audio {row['audio']:5.2f}s  {row['caption']}")
        return 0

    workdir = args.workdir or tempfile.mkdtemp(prefix="game-short-")
    os.makedirs(workdir, exist_ok=True)
    remove_workdir = not args.keep_temp and not args.workdir

    try:
        clips = []
        for index, item in enumerate(plan, 1):
            scene, duration = item["scene"], item["duration"]
            clip = os.path.join(workdir, f"clip{index:02d}.mp4")
            step = (zoom_max - 1.0) / max(duration * fps, 1.0)
            anchor_y = "ih/2-(ih/zoom/2)"
            if index % 2 == 0:
                anchor_y += "-(ih/zoom*0.035)"
            vf = (
                f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},"
                f"zoompan=z='min(zoom+{step:.8f},{zoom_max})'"
                f":x='iw/2-(iw/zoom/2)':y='{anchor_y}'"
                f":d=1:s={width}x{height}:fps={fps},format=yuv420p"
            )
            run([ffmpeg, "-y", "-loop", "1", "-framerate", str(fps), "-t", f"{duration}",
                 "-i", scene["image"], "-vf", vf, "-r", str(fps), "-c:v", "libx264",
                 "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", clip])
            clips.append(clip)

        listing = os.path.join(workdir, "clips.txt")
        with open(listing, "w", encoding="utf-8") as handle:
            for clip in clips:
                handle.write(f"file '{clip}'\n")
        body = os.path.join(workdir, "body.mp4")
        run([ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", listing, "-c", "copy", body])

        # Captions.
        style = (
            f"Style: Main,{font_family},{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,"
            f"&H00000000,-1,0,0,0,100,100,0,0,1,{OUTLINE},2,8,50,50,"
            f"{int(round(caption_y * height))},1"
        )
        events = []
        for item in plan:
            caption = item["scene"]["caption"].strip()
            if not caption:
                continue
            begin, end = item["start"], item["start"] + item["duration"]
            events.append(
                f"Dialogue: 0,{ass_time(begin)},{ass_time(end)},Main,,0,0,0,,"
                f"{{\\fad(220,220)\\an8}}{ass_text(caption)}"
            )
        caps = os.path.join(workdir, "caps.ass")
        with open(caps, "w", encoding="utf-8") as handle:
            handle.write(
                "[Script Info]\nScriptType: v4.00+\n"
                f"PlayResX: {width}\nPlayResY: {height}\n"
                "WrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
                "[V4+ Styles]\n"
                "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
                "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
                "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
                "MarginL, MarginR, MarginV, Encoding\n"
                f"{style}\n\n"
                "[Events]\n"
                "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
                "Effect, Text\n"
                + "\n".join(events) + "\n"
            )

        # Narration mix.
        inputs: list[str] = []
        filters: list[str] = []
        labels: list[str] = []
        for index, item in enumerate(plan):
            inputs += ["-i", item["scene"]["audio"]]
            delay = int(round(item["start"] * 1000))
            filters.append(
                f"[{index}:a]aresample=44100,"
                "aformat=sample_fmts=fltp:channel_layouts=stereo,"
                f"adelay={delay}|{delay}[a{index}]"
            )
            labels.append(f"[a{index}]")
        graph = ";".join(filters) + ";" + "".join(labels) + (
            f"amix=inputs={len(plan)}:normalize=0:duration=longest,"
            f"apad,atrim=0:{total},loudnorm=I=-16:TP=-1.5:LRA=11[aout]"
        )
        voice = os.path.join(workdir, "voice.m4a")
        run([ffmpeg, "-y", *inputs, "-filter_complex", graph, "-map", "[aout]",
             "-c:a", "aac", "-b:a", "192k", voice])

        subtitles = f"subtitles=filename={caps}"
        if args.fonts_dir:
            fonts_dir = os.path.abspath(args.fonts_dir)
            if not os.path.isdir(fonts_dir):
                die(f"fonts folder does not exist: {fonts_dir}", 2)
            subtitles += f":fontsdir={fonts_dir}"

        run([ffmpeg, "-y", "-i", body, "-i", voice, "-vf", subtitles,
             "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", args.preset,
             "-crf", str(args.crf), "-pix_fmt", "yuv420p", "-r", str(fps),
             "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", out])
    except RuntimeError as exc:
        die(str(exc), 1)
    finally:
        if remove_workdir:
            shutil.rmtree(workdir, ignore_errors=True)

    if not os.path.isfile(out):
        die("ffmpeg did not produce an output file", 1)
    size = os.path.getsize(out)
    summary = {"output": out, "bytes": size, "total": total, "width": width,
               "height": height, "fps": fps, "timeline": timeline, "rendered": True}
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"wrote {out} ({size // 1024} KB, {total:.2f}s, {width}x{height} @ {fps}fps)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
