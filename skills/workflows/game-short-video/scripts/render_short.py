#!/usr/bin/env python3
"""Render a vertical 9:16 short from stills, narration files and captions.

Reads a project JSON (see the skill's assets/project.example.json), builds one
Ken Burns clip per scene, concatenates them, mixes the narration lines at their
scene starts, optionally adds a looped music bed ducked under the narration,
burns captions in with libass, and writes a single MP4.

Offline and deterministic: it never downloads anything and writes only to
--out plus a temporary work folder that is removed unless --keep-temp is given.
Requires an ffmpeg with the libass 'subtitles' filter; 'drawtext' is not used.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile

DEFAULT_GAP = 0.65
MIN_SCENE = 3.4
LAST_PAD = 1.8
MIN_LAST = 4.2
OUTLINE = 9
MUSIC_FADE = 1.5
# The bed is normalised to this integrated loudness first, so a quiet and a hot
# track land at the same place; 'music_volume' is then an offset on top of it.
MUSIC_TARGET_LUFS = -30.0
# Normalising on loudness alone can push a quiet track's peaks over 0 dBFS, so the gain
# is capped by whatever keeps the bed's true peak at or below this.
MUSIC_TRUE_PEAK = -2.0
DEFAULT_MUSIC_DB = 0.0

# --verify thresholds. The render normalises the whole mix to LUFS_TARGET once, so a
# finished short outside these bounds means the audio pass did not run as intended.
LUFS_TARGET = -16.0
LUFS_TOL = 1.5
TRUE_PEAK_MAX = -1.0
VERIFY_DURATION_TOL = 0.25
# Caption check: one greyscale frame per scene, searched in a band around caption_y.
FRAME_WIDTH = 270
CAPTION_BAND = (-0.05, 0.22)
CAPTION_INK_MIN = 40
CAPTION_ROW_INK = 4
CAPTION_EDGE = 0.02
CAPTION_SAFE_BOTTOM = 0.88
# Silence check: gaps the timeline does not already account for.
SILENCE_DB = -45.0
SILENCE_MIN = 1.0
SILENCE_TOL = 0.8

# Preflight limits, checked before anything is encoded.
MAX_SHORT_SECONDS = 60.0
MAX_CAPTION_LINES = 2
MAX_CAPTION_CHARS = 26
ASPECT_TOL = 0.02


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


def measure_loudness(ffmpeg: str, path: str,
                     limit: "float | None" = None) -> "tuple[float | None, float | None]":
    """Integrated loudness (LUFS) and true peak (dBTP); (None, None) if unmeasurable.

    'limit' caps how much is decoded, so a bed is judged on the span the short plays:
    a short never hears past its own length, because the loop restarts at the top.
    """
    # The encoded output is discarded, so loudnorm's own I/TP/LRA targets would change
    # nothing here; silence measures '-inf', which does not parse and reads as None.
    cmd = [ffmpeg, "-hide_banner", "-nostats", "-i", path]
    if limit is not None:
        cmd += ["-t", f"{limit:.3f}"]
    proc = subprocess.run(cmd + ["-af", "loudnorm=print_format=json", "-f", "null", "-"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return None, None

    def field(name: str) -> "float | None":
        match = re.search(rf'"{name}"\s*:\s*"(-?[0-9]+(?:\.[0-9]+)?)"', proc.stderr)
        return float(match.group(1)) if match else None

    return field("input_i"), field("input_tp")


def bed_gain_db(measured_lufs: "float | None", offset_db: float,
                true_peak: "float | None" = None) -> float:
    """Gain that puts the bed at MUSIC_TARGET_LUFS plus the offset, capped by its true peak."""
    if measured_lufs is None:
        return offset_db
    gain = MUSIC_TARGET_LUFS - measured_lufs + offset_db
    if true_peak is not None:
        gain = min(gain, MUSIC_TRUE_PEAK - true_peak)
    return gain


def planned_silence(plan: list[dict], total: float) -> "list[tuple[float, float]]":
    """Silent stretches the timeline asks for: after each line, plus the outro pad."""
    spans = [(item["start"] + item["audio"], item["start"] + item["duration"])
             for item in plan]
    spans.append((max(total - LAST_PAD, 0.0), total))
    return spans


def parse_silence(stderr: str) -> "list[tuple[float, float]]":
    """Intervals reported by ffmpeg's silencedetect filter."""
    spans: list[tuple[float, float]] = []
    start = None
    for line in stderr.splitlines():
        opened = re.search(r"silence_start:\s*(-?[0-9.]+)", line)
        if opened:
            start = max(float(opened.group(1)), 0.0)
            continue
        closed = re.search(r"silence_end:\s*(-?[0-9.]+)", line)
        if closed and start is not None:
            spans.append((start, float(closed.group(1))))
            start = None
    return spans


def find_silence(ffmpeg: str, path: str, threshold_db: float = SILENCE_DB,
                 min_duration: float = SILENCE_MIN) -> "list[tuple[float, float]]":
    """Silent stretches of an audio file, from ffmpeg's silencedetect filter."""
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostats", "-i", path, "-af",
         f"silencedetect=noise={threshold_db:.0f}dB:d={min_duration:.2f}",
         "-f", "null", "-"], capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-6:])
        raise RuntimeError(f"silencedetect failed:\n{tail}")
    return parse_silence(proc.stderr)


def unplanned_silence(gaps: "list[tuple[float, float]]",
                      planned: "list[tuple[float, float]]",
                      tolerance: float) -> "list[tuple[float, float, float]]":
    """Gaps that outlast the silence the timeline already explains."""
    out = []
    for start, end in gaps:
        covered = sum(max(0.0, min(end, p_end) - max(start, p_start))
                      for p_start, p_end in planned)
        surplus = (end - start) - covered
        if surplus >= tolerance:
            out.append((start, end, surplus))
    return out


def probe_image(ffprobe: str, path: str) -> "tuple[int, int] | None":
    """Pixel size of a still, or None when ffprobe cannot read it."""
    try:
        out = run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                   "stream=width,height", "-of", "json", path])
        stream = json.loads(out)["streams"][0]
        return int(stream["width"]), int(stream["height"])
    except (RuntimeError, KeyError, IndexError, ValueError, json.JSONDecodeError):
        return None


def _name_table_families(data: bytes, start: int) -> "set[str]":
    """Family names from one sfnt 'name' table."""
    out: set[str] = set()
    try:
        count, strings = struct.unpack(">HH", data[start + 2:start + 6])
    except struct.error:
        return out
    for index in range(count):
        record = start + 6 + 12 * index
        try:
            platform, _, _, name_id, length, offset = struct.unpack(">6H", data[record:record + 12])
        except struct.error:
            break
        if name_id not in (1, 16):
            continue
        raw = data[start + strings + offset:start + strings + offset + length]
        try:
            text = raw.decode("utf-16-be" if platform == 3 else "latin-1")
        except UnicodeDecodeError:
            continue
        text = text.replace("\x00", "").strip()
        if text:
            out.add(text)
    return out


def _sfnt_families(path: str) -> "set[str]":
    """Family names declared inside a TrueType/OpenType file; empty when unreadable."""
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return set()
    if data[:4] == b"ttcf" and len(data) >= 12:
        count = struct.unpack(">I", data[8:12])[0]
        offsets = list(struct.unpack(f">{count}I", data[12:12 + 4 * count]))
    elif data[:4] in (b"\x00\x01\x00\x00", b"OTTO", b"true", b"typ1"):
        offsets = [0]
    else:
        return set()
    names: set[str] = set()
    for offset in offsets:
        try:
            table_count = struct.unpack(">H", data[offset + 4:offset + 6])[0]
        except struct.error:
            continue
        for index in range(table_count):
            pointer = offset + 12 + 16 * index
            try:
                tag, _, table_offset, _ = struct.unpack(">4sIII", data[pointer:pointer + 16])
            except struct.error:
                break
            if tag == b"name":
                names |= _name_table_families(data, table_offset)
    return names


def _normalise_font_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.casefold())


def font_families(fonts_dir: str) -> "set[str]":
    """Normalised family names of the fonts in a folder, read from their name tables."""
    found: set[str] = set()
    try:
        entries = sorted(os.listdir(fonts_dir))
    except OSError:
        return found
    for name in entries:
        path = os.path.join(fonts_dir, name)
        if not os.path.isfile(path):
            continue
        families = _sfnt_families(path)
        if families:
            found.update(_normalise_font_name(family) for family in families)
        else:
            # A font we cannot parse (woff, bitmap) can still be matched by filename.
            found.add(_normalise_font_name(os.path.splitext(name)[0]))
    return found


def has_font(fonts_dir: str, family: str) -> bool:
    """Whether a folder ships a font whose family name matches."""
    return _normalise_font_name(family) in font_families(fonts_dir)


def preflight(ffprobe: str, plan: list[dict], total: float, width: int, height: int,
              font_family: str, fonts_dir: "str | None") -> list[str]:
    """Problems that would otherwise only show up in the finished video.

    Returns warnings; main() prints them and, under --strict, refuses to encode.
    """
    problems: list[str] = []
    target_aspect = width / height
    ratio = "9:16" if abs(target_aspect - 9 / 16) < 0.001 else f"{width}:{height}"
    for index, item in enumerate(plan, 1):
        scene = item["scene"]
        size = probe_image(ffprobe, scene["image"])
        if size is None:
            problems.append(f"scene {index}: cannot read the still {scene['image']}")
            continue
        still_w, still_h = size
        small = still_w < width or still_h < height
        cropped = abs(still_w / still_h - target_aspect) / target_aspect > ASPECT_TOL
        if small or cropped:
            what = f"scene {index}: still is {still_w}x{still_h}"
            if small and cropped:
                problems.append(f"{what}, below {width}x{height} and not {ratio}; "
                                "it will be upscaled and cropped")
            elif small:
                problems.append(f"{what}, below {width}x{height}; it will be upscaled")
            else:
                problems.append(f"{what}, not {ratio}; the render will crop it")
        caption = scene["caption"].strip()
        if not caption:
            continue
        lines = caption.split("\n")
        if len(lines) > MAX_CAPTION_LINES:
            problems.append(f"scene {index}: caption has {len(lines)} lines "
                            f"(max {MAX_CAPTION_LINES})")
        longest = max(len(line.strip()) for line in lines)
        if longest > MAX_CAPTION_CHARS:
            problems.append(f"scene {index}: longest caption line is {longest} characters "
                            f"(max {MAX_CAPTION_CHARS})")
    if total > MAX_SHORT_SECONDS:
        problems.append(f"total {total:.1f}s is over the {MAX_SHORT_SECONDS:.0f}s short limit")
    if fonts_dir and os.path.isdir(fonts_dir) and not has_font(fonts_dir, font_family):
        problems.append(f"font family '{font_family}' is not in {fonts_dir}; captions will "
                        "fall back to a system font")
    return problems


def grab_frame(ffmpeg: str, path: str, at: float, width: int, height: int) -> bytes:
    """One greyscale frame at 'at' seconds, scaled to width x height."""
    proc = subprocess.run(
        [ffmpeg, "-v", "error", "-ss", f"{at:.3f}", "-i", path, "-frames:v", "1",
         "-vf", f"scale={width}:{height}:flags=area,format=gray", "-f", "rawvideo", "-"],
        capture_output=True)
    if proc.returncode != 0 or len(proc.stdout) != width * height:
        raise RuntimeError(f"cannot grab a frame at {at:.2f}s: "
                           f"{proc.stderr.decode('utf-8', 'replace').strip()[-200:]}")
    return proc.stdout


def caption_block(gray: bytes, width: int, height: int, top: float,
                  bottom: float) -> dict:
    """White-stroked text in a row band: near-white pixels next to a dark outline.

    Returns fractional bounds plus the ink count; 'ink' is 0 when nothing was found.
    """
    y0 = max(int(top * height), 1)
    y1 = min(int(round(bottom * height)), height - 1)
    ink, rows = 0, []
    left, right = width - 1, 0
    for y in range(y0, y1):
        base = y * width
        count = 0
        for x in range(1, width - 1):
            i = base + x
            if gray[i] < 228:
                continue
            if min(gray[i - 1], gray[i + 1], gray[i - width], gray[i + width]) > 162:
                continue
            count += 1
            left, right = min(left, x), max(right, x)
        if count >= CAPTION_ROW_INK:
            ink += count
            rows.append(y)
    if not rows:
        return {"ink": 0}
    return {"ink": ink, "top": rows[0] / height, "bottom": (rows[-1] + 1) / height,
            "left": left / width, "right": (right + 1) / width}


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
    music = data.get("music")
    if music is not None:
        if not isinstance(music, str) or not music:
            raise ProjectError("'music' must be a path to an audio file")
        resolved = music if os.path.isabs(music) else os.path.join(base, music)
        if not os.path.isfile(resolved):
            raise ProjectError(f"'music' not found: {resolved}")
        data["music"] = resolved
    if "music_volume" in data:
        value = data["music_volume"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ProjectError("'music_volume' must be a number of dB")
        if not -60.0 <= value <= 0.0:
            raise ProjectError("'music_volume' must be between -60 and 0 dB")
    for key in ("width", "height", "fps"):
        if key in data and not isinstance(data[key], int):
            raise ProjectError(f"'{key}' must be an integer")
    return data


def verify_render(ffmpeg: str, ffprobe: str, target: str, project: dict,
                  plan: list[dict], total: float, width: int, height: int,
                  caption_y: float) -> dict:
    """Check a finished MP4 against the project's timeline.

    Four checks: the real duration against the timeline, one frame per captioned
    scene for the caption's position, the final integrated loudness and true peak,
    and silent stretches longer than the timeline already explains.
    """
    checks: list[dict] = []
    violations: list[str] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail})
        if not ok:
            violations.append(f"{name}: {detail}")

    try:
        actual = probe_duration(ffprobe, target)
    except (ProjectError, RuntimeError) as exc:
        check("duration", False, f"cannot read the file duration: {exc}")
    else:
        check("duration", abs(actual - total) <= max(VERIFY_DURATION_TOL, total * 0.002),
              f"{actual:.2f}s against a {total:.2f}s timeline")

    lufs, peak = measure_loudness(ffmpeg, target)
    if lufs is None or peak is None:
        check("loudness", False, "cannot measure the final loudness and true peak")
    else:
        check("loudness", LUFS_TARGET - LUFS_TOL <= lufs <= LUFS_TARGET + LUFS_TOL,
              f"{lufs:.1f} LUFS against {LUFS_TARGET:.0f} +/- {LUFS_TOL:.1f}")
        check("true peak", peak <= TRUE_PEAK_MAX,
              f"{peak:.1f} dBTP against a {TRUE_PEAK_MAX:.1f} dBTP ceiling")

    # One frame per captioned scene, taken mid-scene where the caption is fully faded in.
    frame_w = FRAME_WIDTH
    frame_h = max(int(round(frame_w * height / width)), 2)
    captioned = sum(1 for item in plan if item["scene"]["caption"].strip())
    drawn = 0
    for index, item in enumerate(plan, 1):
        if not item["scene"]["caption"].strip():
            continue
        at = item["start"] + item["duration"] / 2
        try:
            gray = grab_frame(ffmpeg, target, at, frame_w, frame_h)
        except RuntimeError as exc:
            violations.append(f"caption scene {index}: {exc}")
            continue
        block = caption_block(gray, frame_w, frame_h,
                              caption_y + CAPTION_BAND[0], caption_y + CAPTION_BAND[1])
        if block["ink"] < CAPTION_INK_MIN:
            violations.append(f"caption scene {index} at {at:.2f}s: no caption pixels around "
                              f"y={caption_y:.2f}")
        elif block["left"] < CAPTION_EDGE or block["right"] > 1.0 - CAPTION_EDGE:
            violations.append(f"caption scene {index} at {at:.2f}s: text runs off the frame edge")
        elif block["bottom"] > CAPTION_SAFE_BOTTOM:
            violations.append(f"caption scene {index} at {at:.2f}s: text reaches y="
                              f"{block['bottom']:.2f}, inside the bottom UI band")
        else:
            drawn += 1
    checks.append({"check": "captions", "ok": drawn == captioned,
                   "detail": f"{drawn}/{captioned} caption frames found where expected"})

    try:
        gaps = find_silence(ffmpeg, target)
    except RuntimeError as exc:
        check("silence", False, f"cannot scan the audio: {exc}")
    else:
        surplus = unplanned_silence(gaps, planned_silence(plan, total), SILENCE_TOL)
        for start, end, extra in surplus:
            violations.append(f"silence {start:.2f}-{end:.2f}s: {extra:.2f}s longer than "
                              "the timeline allows")
        detail = (f"{len(gaps)} silent stretch(es), none longer than the timeline allows"
                  if not surplus else
                  f"{len(surplus)} of {len(gaps)} silent stretch(es) outlast the timeline")
        if not gaps and project.get("music"):
            detail += "; a music bed would mask gaps, so verify a voice-only render too"
        checks.append({"check": "silence", "ok": not surplus, "detail": detail})

    return {"ok": not violations, "checks": checks, "violations": violations}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="render_short.py",
        description="Render a 9:16 captioned short from stills and narration files.",
        epilog="Exit codes: 0 rendered or verified clean, 1 render failed, "
               "2 invalid project or input, 3 verification found problems.",
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
    parser.add_argument("--strict", action="store_true",
                        help="treat preflight warnings as errors (exit 2)")
    parser.add_argument("--verify", metavar="MP4", nargs="?", const=True, default=None,
                        help="check the rendered MP4 against the project and exit instead of "
                             "rendering (without a path: the output this run would write)")
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
    music = project.get("music")
    music_db = float(project.get("music_volume", DEFAULT_MUSIC_DB))
    scenes = project["scenes"]

    out = os.path.abspath(args.out or os.path.splitext(os.path.abspath(args.project))[0]
                          + "-short.mp4")
    out_dir = os.path.dirname(out)
    if args.verify is None and out_dir and not os.path.isdir(out_dir):
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

    if args.verify is not None:
        target = os.path.abspath(args.verify if isinstance(args.verify, str) else out)
        if not os.path.isfile(target):
            die(f"rendered file not found: {target}", 2)
        report = verify_render(ffmpeg, ffprobe, target, project, plan, total,
                               width, height, caption_y)
        if args.json:
            print(json.dumps({"file": target, **report}, ensure_ascii=False, indent=2))
        else:
            print(f"verify {target}")
            for row in report["checks"]:
                print(f"  {row['check']:<9}  {'ok  ' if row['ok'] else 'FAIL'}  {row['detail']}")
            for problem in report["violations"]:
                print(f"  ! {problem}")
        return 0 if report["ok"] else 3

    if args.fonts_dir and not os.path.isdir(args.fonts_dir):
        die(f"fonts folder does not exist: {os.path.abspath(args.fonts_dir)}", 2)
    problems = preflight(ffprobe, plan, total, width, height, font_family, args.fonts_dir)
    if problems:
        for problem in problems:
            print(f"preflight: {problem}", file=sys.stderr)
        if args.strict:
            die(f"preflight found {len(problems)} problem(s); fix them or drop --strict", 2)

    if args.dry_run:
        summary = {"output": out, "total": total, "width": width, "height": height,
                   "fps": fps, "music": music, "music_volume": music_db if music else None,
                   "timeline": timeline, "rendered": False}
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            print(f"total {total:.2f}s  {width}x{height} @ {fps}fps")
            for row in timeline:
                print(f"  {row['scene']:2d}  {row['start']:6.2f}s +{row['duration']:5.2f}s  "
                      f"audio {row['audio']:5.2f}s  {row['caption']}")
        return 0

    # Normalise the bed to a fixed loudness before the offset, so the same
    # music_volume behaves the same for a quiet and for a hot track.
    bed_gain = music_db
    if music:
        measured, peak = measure_loudness(ffmpeg, music, total)
        if measured is None:
            print("warning: cannot measure the music bed; music_volume is used as a raw gain",
                  file=sys.stderr)
        bed_gain = bed_gain_db(measured, music_db, peak)

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
            f"amix=inputs={len(plan)}:normalize=0:duration=longest[voice]"
        )
        tail = f"atrim=0:{total},loudnorm=I=-16:TP=-1.5:LRA=11[aout]"
        if music:
            # Loop the bed to the full length, fade it out, then duck it under the voice. The
            # side-chain key is padded with whole_dur (not apad) so the graph still ends.
            graph += ";" + (
                "[voice]asplit=2[vmix][vkey0]"
                f";[vkey0]apad=whole_dur={total}[vkey]"
                f";[{len(plan)}:a]aresample=44100,"
                "aformat=sample_fmts=fltp:channel_layouts=stereo,"
                f"volume={bed_gain:.3f}dB,atrim=0:{total},asetpts=PTS-STARTPTS,"
                f"afade=t=out:st={max(total - MUSIC_FADE, 0.0):.3f}:d={MUSIC_FADE}[music]"
                ";[music][vkey]sidechaincompress=threshold=0.05:ratio=8:"
                "attack=10:release=400:makeup=1[bed]"
                ";[vmix][bed]amix=inputs=2:normalize=0:duration=longest,"
                + tail
            )
            inputs += ["-stream_loop", "-1", "-i", music]
        else:
            graph += f";[voice]apad,{tail}"
        voice = os.path.join(workdir, "voice.m4a")
        run([ffmpeg, "-y", *inputs, "-filter_complex", graph, "-map", "[aout]",
             "-t", f"{total}", "-c:a", "aac", "-b:a", "192k", voice])

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
               "height": height, "fps": fps, "music": music,
               "music_volume": music_db if music else None,
               "music_gain_db": round(bed_gain, 2) if music else None,
               "music_target_lufs": MUSIC_TARGET_LUFS if music else None,
               "timeline": timeline, "rendered": True}
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"wrote {out} ({size // 1024} KB, {total:.2f}s, {width}x{height} @ {fps}fps)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
