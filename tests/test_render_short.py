#!/usr/bin/env python3
"""Offline regression tests for the game-short-video project-file validation.

Run: python3 -m unittest tests.test_render_short
These tests cover the music bed keys and the --verify checks; they need no ffmpeg,
no audio and no network. Rendering itself is exercised by the skill's manual QA pass.
"""
import importlib.util
import json
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parents[1] / "skills/workflows/game-short-video/scripts"
SCRIPT = HERE / "render_short.py"


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


render_short = load("render_short")


class RenderShortProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ("still.jpg", "vo.mp3", "bed.mp3"):
            (self.root / name).write_bytes(b"")
        self.scene = {"image": "still.jpg", "caption": "enemies learn", "audio": "vo.mp3"}

    def write_project(self, **extra):
        project = {"scenes": [dict(self.scene)]}
        project.update(extra)
        path = self.root / "project.json"
        path.write_text(json.dumps(project), encoding="utf-8")
        return path

    def run_tool(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                              capture_output=True, text=True)

    def assert_rejected(self, message, **extra):
        path = self.write_project(**extra)
        with self.assertRaises(render_short.ProjectError) as caught:
            render_short.load_project(str(path))
        self.assertIn(message, str(caught.exception))
        result = self.run_tool(path, "--dry-run")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(message, result.stderr)

    def test_missing_music_file_is_rejected(self):
        self.assert_rejected("'music' not found", music="absent.mp3")

    def test_music_must_be_a_path_string(self):
        self.assert_rejected("'music' must be a path to an audio file", music=123)
        self.assert_rejected("'music' must be a path to an audio file", music="")

    def test_music_volume_must_be_a_number(self):
        self.assert_rejected("'music_volume' must be a number of dB", music="bed.mp3",
                             music_volume="loud")
        self.assert_rejected("'music_volume' must be a number of dB", music="bed.mp3",
                             music_volume=True)

    def test_music_volume_is_limited_to_minus_60_through_0(self):
        for value in (-60.5, -70, 0.5, 6):
            with self.subTest(music_volume=value):
                self.assert_rejected("'music_volume' must be between -60 and 0 dB",
                                     music="bed.mp3", music_volume=value)

    def test_project_without_music_still_loads(self):
        path = self.write_project()
        project = render_short.load_project(str(path))
        self.assertNotIn("music", project)
        self.assertNotIn("music_volume", project)
        self.assertEqual(project["scenes"][0]["image"], str(self.root / "still.jpg"))

    def test_project_with_music_resolves_the_bed(self):
        path = self.write_project(music="bed.mp3", music_volume=-18)
        project = render_short.load_project(str(path))
        self.assertEqual(project["music"], str(self.root / "bed.mp3"))
        self.assertEqual(project["music_volume"], -18)

    def test_bed_gain_normalises_tracks_to_the_same_target(self):
        # A quiet and a hot master must reach the same place; the offset rides on top.
        self.assertAlmostEqual(render_short.bed_gain_db(-38.0, 0.0), 8.0)
        self.assertAlmostEqual(render_short.bed_gain_db(-18.0, 0.0), -12.0)
        self.assertAlmostEqual(render_short.bed_gain_db(-38.0, -6.0), 2.0)
        self.assertEqual(render_short.bed_gain_db(None, -6.0), -6.0)

    def test_bed_gain_is_capped_by_the_true_peak(self):
        # -38 LUFS with a -6 dBTP transient: +8 dB would land the peak at +2 dBTP.
        self.assertAlmostEqual(render_short.bed_gain_db(-38.0, 0.0, -6.0), 4.0)
        # A hot master whose peaks are already low needs no cap.
        self.assertAlmostEqual(render_short.bed_gain_db(-18.0, 0.0, -12.0), -12.0)
        # A peak above the ceiling attenuates even a bed that measures at target.
        self.assertAlmostEqual(render_short.bed_gain_db(-30.0, 0.0, 0.0), -2.0)
        # Asking for an offset does not get around the cap.
        self.assertAlmostEqual(render_short.bed_gain_db(-40.0, 0.0, -3.0), 1.0)
        # No usable measurement: the offset passes through untouched.
        self.assertEqual(render_short.bed_gain_db(None, -6.0, -6.0), -6.0)

    def test_music_volume_bounds_are_inclusive(self):
        for value in (-60, 0):
            with self.subTest(music_volume=value):
                path = self.write_project(music="bed.mp3", music_volume=value)
                self.assertEqual(render_short.load_project(str(path))["music_volume"], value)


class RenderShortVerifyTests(unittest.TestCase):
    """The --verify checks, with ffmpeg and ffprobe stubbed out."""

    PLAN = [{"start": 0.0, "duration": 5.0, "audio": 4.0,
             "scene": {"caption": "enemies learn", "image": "still.jpg", "audio": "vo.mp3"}}]
    TOTAL = 5.0

    @staticmethod
    def frame_with_caption(width, height, top, margin=None):
        """White strokes on a mid-grey field: how a caption looks at analysis size."""
        gray = bytearray([120] * (width * height))
        rows = max(int(0.03 * height), 4)
        left = width // 6 if margin is None else margin
        for y in range(int(top * height), int(top * height) + rows):
            for x in range(max(left, 1), min(width - left, width - 1), 3):
                gray[y * width + x - 1] = 0
                gray[y * width + x + 1] = 0
                gray[y * width + x] = 255
        return bytes(gray)

    def verify(self, *, duration=5.0, loudness=(-16.0, -1.6), caption_top=0.108,
               caption_margin=None, silence=((4.0, 5.0),), music=None):
        project = {"scenes": self.PLAN}
        if music:
            project["music"] = music
        with mock.patch.multiple(
            render_short,
            probe_duration=mock.DEFAULT,
            measure_loudness=mock.DEFAULT,
            grab_frame=mock.DEFAULT,
            find_silence=mock.DEFAULT,
        ) as patched:
            patched["probe_duration"].return_value = duration
            patched["measure_loudness"].return_value = loudness
            patched["grab_frame"].side_effect = (
                lambda ffmpeg, path, at, w, h: self.frame_with_caption(
                    w, h, caption_top, caption_margin))
            patched["find_silence"].return_value = list(silence)
            return render_short.verify_render("ffmpeg", "ffprobe", "short.mp4", project,
                                              self.PLAN, self.TOTAL, 1080, 1920, 0.10)

    def test_clean_render_passes(self):
        report = self.verify()
        self.assertTrue(report["ok"], report["violations"])
        self.assertEqual(report["violations"], [])
        self.assertEqual([row["check"] for row in report["checks"]],
                         ["duration", "loudness", "true peak", "captions", "silence"])

    def test_duration_drift_is_a_violation(self):
        report = self.verify(duration=6.0)
        self.assertFalse(report["ok"])
        self.assertTrue(any(v.startswith("duration:") for v in report["violations"]))

    def test_loudness_out_of_range_is_a_violation(self):
        report = self.verify(loudness=(-13.0, -1.6))
        self.assertFalse(report["ok"])
        self.assertTrue(any(v.startswith("loudness:") for v in report["violations"]))

    def test_true_peak_above_the_ceiling_is_a_violation(self):
        report = self.verify(loudness=(-16.0, -0.2))
        self.assertFalse(report["ok"])
        self.assertTrue(any(v.startswith("true peak:") for v in report["violations"]))

    def test_unmeasurable_audio_is_a_violation(self):
        report = self.verify(loudness=(None, None))
        self.assertFalse(report["ok"])
        self.assertTrue(any(v.startswith("loudness:") for v in report["violations"]))

    def test_caption_missing_from_its_band_is_a_violation(self):
        report = self.verify(caption_top=0.45)
        self.assertFalse(report["ok"])
        self.assertTrue(any("no caption pixels" in v for v in report["violations"]))

    def test_caption_touching_the_frame_edge_is_a_violation(self):
        report = self.verify(caption_margin=0)
        self.assertFalse(report["ok"])
        self.assertTrue(any("runs off the frame edge" in v for v in report["violations"]))

    def test_silence_longer_than_the_timeline_is_a_violation(self):
        report = self.verify(silence=((8.0, 12.0),))
        self.assertFalse(report["ok"])
        self.assertTrue(any(v.startswith("silence 8.00-12.00s") for v in report["violations"]))

    def test_planned_gaps_and_the_outro_pad_are_fine(self):
        report = self.verify(silence=((4.0, 5.0), (0.0, 0.0)))
        self.assertTrue(report["ok"], report["violations"])

    def test_music_note_is_only_added_when_nothing_was_heard(self):
        quiet = self.verify(silence=(), music="bed.mp3")
        self.assertIn("voice-only", quiet["checks"][-1]["detail"])
        loud = self.verify(music="bed.mp3")
        self.assertNotIn("voice-only", loud["checks"][-1]["detail"])


class RenderShortPreflightTests(unittest.TestCase):
    """Preflight warnings, with ffprobe stubbed and a synthetic font on disk."""

    PLAN = [{"start": 0.0, "duration": 5.0, "audio": 4.0,
             "scene": {"image": "s1.jpg", "audio": "vo.mp3", "caption": "enemies learn"}}]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fonts = self.root / "fonts"
        self.fonts.mkdir()

    @staticmethod
    def write_font(path, family):
        """A minimal sfnt holding one Windows family-name record."""
        name = family.encode("utf-16-be")
        table = (struct.pack(">HHH", 0, 1, 18)
                 + struct.pack(">6H", 3, 1, 0x409, 1, len(name), 0) + name)
        header = struct.pack(">IHHHH", 0x00010000, 1, 16, 0, 0)
        path.write_bytes(header + struct.pack(">4sIII", b"name", 0, 28, len(table)) + table)

    def preflight(self, *, size=(1080, 1920), caption=None, total=49.6,
                  fonts_dir=None, family="Poppins"):
        plan = [dict(self.PLAN[0])]
        plan[0]["scene"] = dict(self.PLAN[0]["scene"])
        if caption is not None:
            plan[0]["scene"]["caption"] = caption
        with mock.patch.object(render_short, "probe_image", return_value=size):
            return render_short.preflight("ffprobe", plan, total, 1080, 1920, family, fonts_dir)

    def test_clean_project_has_nothing_to_report(self):
        self.write_font(self.fonts / "Poppins-Regular.ttf", "Poppins")
        self.assertEqual(self.preflight(fonts_dir=str(self.fonts)), [])

    def test_small_still_is_flagged(self):
        problems = self.preflight(size=(720, 1280))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("below 1080x1920", problems[0])
        self.assertIn("upscaled", problems[0])

    def test_wrong_aspect_ratio_is_flagged(self):
        problems = self.preflight(size=(3413, 1920))  # big enough, but 16:9
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("not 9:16", problems[0])
        self.assertIn("crop", problems[0])

    def test_small_and_off_ratio_is_one_message(self):
        problems = self.preflight(size=(752, 1392))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("upscaled and cropped", problems[0])

    def test_unreadable_still_is_flagged(self):
        problems = self.preflight(size=None)
        self.assertTrue(any("cannot read the still" in p for p in problems))

    def test_three_caption_lines_are_flagged(self):
        problems = self.preflight(caption="one\ntwo\nthree")
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("3 lines", problems[0])

    def test_long_caption_line_is_flagged(self):
        problems = self.preflight(caption="this caption line is far too long to read")
        self.assertTrue(any("characters" in p for p in problems), problems)

    def test_two_lines_at_the_limit_pass(self):
        line = "x" * render_short.MAX_CAPTION_CHARS
        self.assertEqual(self.preflight(caption=f"{line}\n{line}"), [])

    def test_over_sixty_seconds_is_flagged(self):
        problems = self.preflight(total=61.0)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("60s short limit", problems[0])

    def test_missing_font_family_is_flagged(self):
        self.write_font(self.fonts / "Poppins-Regular.ttf", "Poppins")
        problems = self.preflight(fonts_dir=str(self.fonts), family="Inter")
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("'Inter' is not in", problems[0])

    def test_font_family_matches_case_and_spacing(self):
        self.write_font(self.fonts / "weird-name.otf", "Space Grotesk")
        self.assertEqual(self.preflight(fonts_dir=str(self.fonts), family="space  grotesk"), [])


class RenderShortMeasurementTests(unittest.TestCase):
    """measure_loudness, with ffmpeg's stderr faked."""

    JSON = '\t"input_i" : "-31.34",\n\t"input_tp" : "-18.13",\n'

    def run_measure(self, stderr=None, returncode=0, limit=None):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, returncode, "", stderr or self.JSON)

        with mock.patch.object(render_short.subprocess, "run", fake_run):
            return render_short.measure_loudness("ffmpeg", "bed.mp3", limit), captured["cmd"]

    def test_loudness_and_true_peak_are_read_from_one_pass(self):
        (lufs, peak), _ = self.run_measure()
        self.assertEqual((lufs, peak), (-31.34, -18.13))

    def test_the_decode_is_bounded_by_the_short_length(self):
        _, bounded = self.run_measure(limit=12.5)
        self.assertIn("-t", bounded)
        self.assertEqual(bounded[bounded.index("-t") + 1], "12.500")
        _, unbounded = self.run_measure()
        self.assertNotIn("-t", unbounded)

    def test_unreadable_and_silent_beds_fall_back(self):
        (lufs, peak), _ = self.run_measure(returncode=1, stderr="boom")
        self.assertIsNone(lufs)
        self.assertIsNone(peak)
        (lufs, peak), _ = self.run_measure(stderr='"input_i" : "-inf", "input_tp" : "-inf"')
        self.assertIsNone(lufs)
        self.assertIsNone(peak)


class RenderShortHelperTests(unittest.TestCase):
    def test_parse_silence_reads_silencedetect_output(self):
        stderr = (
            "[silencedetect @ 0x1] silence_start: 4.317\n"
            "[silencedetect @ 0x1] silence_end: 5.318 | silence_duration: 1.001\n"
            "[silencedetect @ 0x1] silence_start: 47.497\n"
            "[silencedetect @ 0x1] silence_end: 49.664 | silence_duration: 2.167\n"
        )
        self.assertEqual(render_short.parse_silence(stderr),
                         [(4.317, 5.318), (47.497, 49.664)])
        self.assertEqual(render_short.parse_silence("nothing here"), [])

    def test_unplanned_silence_ignores_what_the_timeline_explains(self):
        planned = [(4.0, 5.0), (7.0, 8.0)]
        self.assertEqual(render_short.unplanned_silence([(4.0, 5.1)], planned, 0.8), [])
        flagged = render_short.unplanned_silence([(8.0, 12.0)], planned, 0.8)
        self.assertEqual(len(flagged), 1)
        self.assertAlmostEqual(flagged[0][2], 4.0)

    def test_planned_silence_covers_each_line_and_the_outro(self):
        plan = [{"start": 0.0, "duration": 5.0, "audio": 4.0},
                {"start": 5.0, "duration": 4.2, "audio": 2.4}]
        self.assertEqual(render_short.planned_silence(plan, 9.2),
                         [(4.0, 5.0), (7.4, 9.2), (9.2 - render_short.LAST_PAD, 9.2)])

    def test_caption_block_finds_text_only_inside_the_band(self):
        frame = RenderShortVerifyTests.frame_with_caption(60, 40, 0.10)
        found = render_short.caption_block(frame, 60, 40, 0.05, 0.32)
        self.assertGreaterEqual(found["ink"], render_short.CAPTION_INK_MIN)
        self.assertAlmostEqual(found["top"], 0.10, places=2)
        self.assertLess(found["bottom"], 0.32)
        self.assertEqual(render_short.caption_block(frame, 60, 40, 0.40, 0.80), {"ink": 0})

    def test_caption_block_ignores_a_flat_frame(self):
        self.assertEqual(render_short.caption_block(bytes([120] * 2400), 60, 40, 0.0, 1.0),
                         {"ink": 0})


if __name__ == "__main__":
    unittest.main()
