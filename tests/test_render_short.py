#!/usr/bin/env python3
"""Offline regression tests for the game-short-video project-file validation.

Run: python3 -m unittest tests.test_render_short
These tests cover the music bed keys only; they need no ffmpeg, no audio and no
network. Rendering itself is exercised by the skill's manual QA pass.
"""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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

    def test_music_volume_bounds_are_inclusive(self):
        for value in (-60, 0):
            with self.subTest(music_volume=value):
                path = self.write_project(music="bed.mp3", music_volume=value)
                self.assertEqual(render_short.load_project(str(path))["music_volume"], value)


if __name__ == "__main__":
    unittest.main()
