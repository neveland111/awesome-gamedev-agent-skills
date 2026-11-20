---
name: game-short-video
description: >
  Produce a vertical 9:16 short (Shorts, TikTok, Reels) for a game from stills or captures plus
  a spoken script: beat sheet, one narration file per line, burned-in captions, Ken Burns motion,
  and one ffmpeg render without a video editor. Use for a devlog clip, feature reveal, launch
  teaser, wishlist or Steam-page promo short, or any request to turn screenshots into a captioned
  vertical video with narration.
---

# Game short video — 9:16, captions burned in

Deliver one 1080×1920 H.264/AAC MP4, 35–60 seconds, with spoken narration, captions rendered
into the frame, and slow motion on still images. No timeline editor is involved: a JSON beat
sheet plus `scripts/render_short.py` (which drives ffmpeg) is the whole build.

Baseline for new work: ffmpeg 7.x built with **libass**, Python 3.10+. Keep an existing project's
pinned ffmpeg if it already renders one of these; the filters used here exist from ffmpeg 4.4 on.

## When to use

- Use when the request is a vertical short for a game: devlog clip, feature reveal, patch/update
  announcement, launch or wishlist teaser, "turn these screenshots into a TikTok".
- Use when captions must be burned in (social platforms, muted autoplay), or when the script is
  narration-first and the visuals are stills rather than recorded footage.
- Triggers: `shorts`, `TikTok`, `Reels`, `9:16`, `1080x1920`, `captions burned in`, `vertical
  video`, `teaser`, `devlog video`, `promo clip`.

**When *not* to use:** editing real gameplay footage with cuts, transitions and audio ducking
inside a recorded track — that is an NLE or `ffmpeg` timeline task, not a stills beat sheet. For
the store-page copy and release mechanics, use `steam-publish` or `itch-publish`. For producing
the art itself, use `create-game-assets`.

## Workflow

1. **Write the beat sheet before any pixels.** One claim per scene, 8–12 scenes, 3–7 seconds
   each, 40–70 spoken words per 30 seconds. Read `references/short-form-scripting.md` for the
   hook, middle and loop rules — a short fails on structure long before it fails on art.
2. **Record narration per line, one file per scene.** Voice-over is the clock: the render derives
   every scene duration from its audio file, so never hand-time captions to a guess. TTS is
   acceptable; keep it consistent across lines. Export mono or stereo MP3/WAV.
3. **Prepare one still per scene.** 9:16 framing, at least 1080×1920 (2× if the game art allows).
   The render crops to fill, so keep the subject away from the outer 6% and leave the top fifth
   free of critical detail — captions sit there.
4. **Fill the project file** (schema below) and render:

   ```bash
   python scripts/render_short.py project.json --out build/short.mp4 \
       --ffmpeg /path/to/ffmpeg --fonts-dir assets/fonts
   ```

   Paths inside the project file are resolved relative to the project file. Run with `--dry-run`
   first to print the timeline without encoding, and `--json` for machine-readable output.
5. **QA the actual file, not the intent.** Probe duration/resolution/streams, extract one frame
   per scene and look at caption placement, and check the audio peak. Commands are in
   `references/ffmpeg-pipeline.md`.
6. **Hand off where it publishes.** The same file works on Shorts, TikTok and Reels; if the game
   is on Steam or itch.io, link it from the description rather than burning a URL into the frame.

## Project file

```json
{
  "width": 1080,
  "height": 1920,
  "fps": 30,
  "font_family": "Poppins",
  "font_size": 80,
  "caption_y": 0.14,
  "zoom": 1.1,
  "gap": 0.65,
  "scenes": [
    {
      "image": "stills/01-hook.jpg",
      "caption": "enemies learn.\nyou don't",
      "audio": "vo/01.mp3"
    },
    {
      "image": "stills/02-what-it-is.jpg",
      "caption": "every run they adapt\nto how you play",
      "audio": "vo/02.mp3"
    }
  ]
}
```

`image` and `audio` are required per scene; `caption` may be empty for a silent beat. Captions
wrap on explicit `\n` only — keep it to two lines of at most ~26 characters. `caption_y` is the
top edge as a fraction of height. `gap` is the pause after a line; the last scene adds extra.

`assets/project.example.json` is a working starting point.

## Patterns

### 1. Audio is the clock

```text
scene_duration = max(audio_duration + gap, 3.4)
last_scene     = max(audio_duration + 1.8, 4.2)
start_i        = sum(previous scene durations)
```

The last scene gets a longer tail so the closing line is never cut by the loop point. Captions
use the same start/end numbers, which is why they stay in sync for the whole video.

### 2. Ken Burns without frame loss

```bash
ffmpeg -loop 1 -framerate 30 -t 5.19 -i still.jpg \
  -vf "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,\
zoompan=z='min(zoom+0.00064,1.10)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=1080x1920:fps=30" \
  -r 30 -c:v libx264 -preset veryfast -crf 18 -pix_fmt yuv420p clip.mp4
```

`-framerate` on the still input must equal the output fps. Left at ffmpeg's default 25, a 30 fps
`zoompan` emits roughly 17% fewer frames than requested, every clip comes out short, and the
whole video drifts out of sync with the narration.

### 3. Captions through libass, not `drawtext`

Static ffmpeg builds frequently omit `drawtext` (no freetype) while including `libass`. Ship an
`.ass` script instead: `subtitles=filename=caps.ass:fontsdir=assets/fonts`. Alignment 8 with
`MarginV` at about 14% of height, size ~80 at 1080 wide, outline 9, shadow 2, `\fad(220,220)`.

```text
Style: Main,Poppins,80,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,9,2,8,50,50,255,1
Dialogue: 0,0:00:00.00,0:00:05.19,Main,,0,0,0,,{\fad(220,220)\an8}enemies learn.\Nyou don't
```

### 4. Narration mix

Delay each line to its scene start, mix without renormalizing, pad to the video length, then
normalize once:

```text
[0:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,adelay=5186|5186[a0];...
[a0][a1]...amix=inputs=N:normalize=0:duration=longest,apad,atrim=0:TOTAL,\
loudnorm=I=-16:TP=-1.5:LRA=11[aout]
```

## Pitfalls

- **`No such filter: 'drawtext'`** → that ffmpeg has no freetype. Use the libass path above;
  do not "fix" it by installing fonts alone.
- **Total length shorter than the plan, captions drifting later each scene** → still input
  framerate disagrees with `fps` in `zoompan`. Set `-framerate` to the project fps.
- **Captions cover the character or the gameplay** → they are drawn in the top band; re-crop the
  still or lower `caption_y` only if the top of the frame is busier than the subject.
- **Font silently ignored, captions fall back to a serif** → ASS matches the font *family*, not
  the filename. Pass `--fonts-dir` and set `font_family` to the family inside the file.
- **Lines arrive late or early** → a scene's `audio` is the wrong line, or two lines were merged
  into one file. Keep exactly one spoken sentence group per scene.
- **Video ends before the last word** → the final scene needs the longer tail; the script already
  applies it, so check the source audio really is the closing line.
- **Peaks at 0 dB or dialogue buried** → run `loudnorm` once at the end, not per line, and keep
  music below −20 dB relative to voice (the script mixes voice only).
- **Text too long to read in 4 seconds** → two lines maximum; move the detail into narration.
- **Captions unreadable on a bright sky** → outline 9 plus shadow, not a semi-transparent box;
  boxes hide the art and look like subtitles rather than design.

## Resources

- `references/short-form-scripting.md` — hook/middle/loop structure, caption length, beat-sheet
  examples for devlog, feature reveal and launch shorts. Read before writing the script.
- `references/ffmpeg-pipeline.md` — full filter recipes, ASS style block, QA and audio commands.
- `scripts/render_short.py --help` — the renderer; offline, deterministic, writes only `--out`
  and a temp folder.
- `assets/project.example.json` — copy as the starting project file.

## Related skills

- `create-game-assets` — produces the stills that become the scenes, with a locked visual target.
- `steam-publish` / `itch-publish` — the pages these shorts point at.
- `game-jam` / `prototype-fast` — the build being shown; keep capture honest to what ships.
