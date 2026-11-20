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
   first to print the timeline without encoding, and `--json` for machine-readable output. Add
   `--strict` to refuse the render when preflight finds a problem (see below).
5. **QA the actual file, not the intent.** `--verify` does the mechanical pass — duration against
   the timeline, one frame per scene for caption placement, final loudness and true peak, and
   silences longer than the timeline allows. Then read a few of those frames yourself: it checks
   where the text landed, not whether it reads well.
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
  "music": "audio/bed.mp3",
  "music_volume": 0,
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
`music` is optional: one audio file, looped to the full length, faded out over the last 1.5 s and
ducked automatically under the voice. The bed is first measured and normalised to **−30 LUFS**, so
a quiet and a loud master land in the same place; `music_volume` is then an offset in dB on top of
that (default `0`, allowed `-60` to `0`). The gain is capped so the bed's true peak stays at or
below −2 dBTP, which can leave a very peaky track a little quieter than −30 LUFS. Only the first
`TOTAL` seconds of the bed are measured, so a track that changes level midway gets a gain that
suits its opening; if you re-cut the short longer, the gain moves with it. At `0` the bed sits about 14 dB under a −16 LUFS voice,
which is the right starting point — lower it in 2–3 dB steps if the words get buried. Omit both
keys for a voice-only short.

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
[a0][a1]...amix=inputs=N:normalize=0:duration=longest[voice]
```

With a `music` file, the bed's integrated loudness and true peak are measured once over its first
`TOTAL` seconds — the loop never reaches past the end of the short, and measuring only that span
keeps the level tied to what is heard (`loudnorm … print_format=json`, read `input_i` and
`input_tp`) — and
`GAIN = min(-30 - measured + music_volume, -2 - input_tp)` — the second term keeps a quiet track
with one loud transient from clipping; the bed is then looped
(`-stream_loop -1`), levelled by that gain, faded and side-chain compressed by the voice before the
two are summed:

```text
[N:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,volume=GAINdB,\
atrim=0:TOTAL,asetpts=PTS-STARTPTS,afade=t=out:st=TOTAL-1.5:d=1.5[music];
[voice]asplit=2[vmix][vkey0];
[vkey0]apad=whole_dur=TOTAL[vkey];
[music][vkey]sidechaincompress=threshold=0.05:ratio=8:attack=10:release=400:makeup=1[bed];
[vmix][bed]amix=inputs=2:normalize=0:duration=longest,atrim=0:TOTAL[vout]
```

The key is padded to the full length first: `sidechaincompress` stops at the shorter of its two
inputs, so an unpadded voice cuts the bed off at the last word instead of playing the outro out.
Use `apad=whole_dur`, not `apad,atrim` — an unbounded `apad` upstream of `atrim` never signals
end-of-stream and the render hangs.

## Preflight

Every render checks the inputs before the first encode and prints what it finds to stderr:

```text
preflight: scene 3: still is 752x1392, below 1080x1920 and not 9:16; it will be upscaled and cropped
preflight: scene 5: longest caption line is 34 characters (max 26)
preflight: font family 'Inter' is not in assets/fonts; captions will fall back to a system font
preflight: total 87.1s is over the 60s short limit
```

Per scene it compares the still's pixel size against the project's `width`x`height` (default
1080x1920) and its aspect ratio against 9:16 (2 % tolerance), then the caption's line count
(max 2) and longest line (max 26 characters). Once for the whole short: the total length against
60 s, and `font_family` against the families actually declared inside the files in `--fonts-dir`
— read from each font's name table, so a renamed file still matches, and only checked when
`--fonts-dir` is passed. A `--fonts-dir` that does not exist is always a hard error, before any
encoding.

These are warnings by default: a slightly soft still still renders, and the choice stays yours.
Pass `--strict` to turn them into exit `2` before anything is encoded — use it in CI and batch
runs. `--dry-run` runs the same preflight, so checking costs nothing while you iterate.

## Verify a render

`--verify` checks a finished MP4 against the project instead of encoding anything:

```bash
python scripts/render_short.py project.json --verify build/short.mp4 \
    --ffmpeg /path/to/ffmpeg
```

Exit `0` when everything passes, `3` when a check fails, `2` when the project or the file cannot
be read. `--json` prints the same report as an object for CI. Four checks:

- **duration** — the container's real duration against the timeline, within 0.25 s or 0.2 %.
  Catches a render that ended early, a stale file from an older cut, or a project edited after
  rendering.
- **captions** — one frame per captioned scene at its midpoint, searched in a band from
  `caption_y - 0.05` to `caption_y + 0.22`. A caption counts as drawn when near-white pixels sit
  against a dark outline, which is what white text with a black stroke looks like at 270 px wide.
  It fails when nothing is found where the caption should be (missing font, caption pulled
  off-frame, a project that no longer matches the file), when the text touches the outer 2 % and
  is clipped, or when it reaches the bottom 12 % where the Shorts UI sits.
- **loudness and true peak** — the final mix must measure −16 ±1.5 LUFS with a true peak at or
  below −1 dBTP, i.e. the single `loudnorm` pass really ran.
- **silence** — stretches at −45 dB or quieter lasting at least 1 s that outlast what the timeline
  already explains (the pause after each line plus the outro pad) by 0.8 s or more. This is what
  catches a narration file that failed to load or is silent. A music bed fills those gaps, so
  verify a voice-only render when the mix has music.

The caption check is a pixel heuristic, not OCR: it proves text was drawn near the right place,
not that it reads correctly.

## Pitfalls

- **`No such filter: 'drawtext'`** → that ffmpeg has no freetype. Use the libass path above;
  do not "fix" it by installing fonts alone.
- **Total length shorter than the plan, captions drifting later each scene** → still input
  framerate disagrees with `fps` in `zoompan`. Set `-framerate` to the project fps.
- **Captions cover the character or the gameplay** → they are drawn in the top band; re-crop the
  still or lower `caption_y` only if the top of the frame is busier than the subject.
- **Font silently ignored, captions fall back to a serif** → ASS matches the font *family*, not
  the filename. Pass `--fonts-dir` and set `font_family` to the family inside the file; preflight
  warns when that family is not among the fonts you shipped.
- **Lines arrive late or early** → a scene's `audio` is the wrong line, or two lines were merged
  into one file. Keep exactly one spoken sentence group per scene.
- **Video ends before the last word** → the final scene needs the longer tail; the script already
  applies it, so check the source audio really is the closing line.
- **Peaks at 0 dB or dialogue buried** → run `loudnorm` once at the end, not per line; a bed
  above about −18 dB under the voice fights it. The bed is normalised to −30 LUFS, so this is now
  a per-video judgement of `music_volume` (default `0`), not a property of the track you picked.
- **Music stops early or ends abruptly** → the bed is looped and trimmed to the video length by
  the script; a gap means the file is shorter than expected and has a silent tail — trim the
  source loop to its real content.
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
