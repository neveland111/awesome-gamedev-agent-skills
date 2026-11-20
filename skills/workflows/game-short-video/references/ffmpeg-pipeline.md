# ffmpeg pipeline for a 9:16 captioned short

Everything here is what `scripts/render_short.py` does; use it when you need to change one stage
by hand or debug a render.

Requirements: ffmpeg 4.4+ with `libass` (`ffmpeg -filters | grep subtitles`). `drawtext` is not
required and is often missing from static builds.

## 1. Probe the narration

```bash
ffprobe -v error -show_entries format=duration -of csv=p=0 vo/01.mp3
```

Timeline for scene *i*: `duration_i = max(audio_i + gap, 3.4)`, last scene
`max(audio_last + 1.8, 4.2)`; `start_i = Σ duration_<i`.

## 2. One clip per scene

```bash
ffmpeg -y -loop 1 -framerate 30 -t 5.19 -i stills/01.jpg \
  -vf "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,\
zoompan=z='min(zoom+0.00064,1.10)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'\
:d=1:s=1080x1920:fps=30,format=yuv420p" \
  -r 30 -c:v libx264 -preset veryfast -crf 18 -pix_fmt yuv420p build/clip01.mp4
```

- `zoom step = (zoom_max − 1) / (duration × fps)`; `zoom_max` 1.08–1.12 for calm shorts.
- Alternate the anchor between scenes (centre, then a slow upward pan) so consecutive scenes do
  not share the same push:

```bash
# pan variant
y='ih/2-(ih/zoom/2)-(ih/zoom*0.035)'
```

- Keep `-framerate` equal to `fps` in `zoompan`. Mismatch is the single most common cause of a
  short final video and drifting captions.

## 3. Concatenate

```bash
printf "file '%s'\n" "$PWD"/build/clip*.mp4 > build/list.txt
ffmpeg -y -f concat -safe 0 -i build/list.txt -c copy build/body.mp4
```

## 4. Narration mix

```bash
ffmpeg -y \
  -i vo/01.mp3 -i vo/02.mp3 -i vo/03.mp3 \
  -filter_complex "\
[0:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,adelay=0|0[a0];\
[1:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,adelay=5190|5190[a1];\
[2:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,adelay=12480|12480[a2];\
[a0][a1][a2]amix=inputs=3:normalize=0:duration=longest,apad,atrim=0:18.2,\
loudnorm=I=-16:TP=-1.5:LRA=11[aout]" \
  -map "[aout]" -c:a aac -b:a 192k build/voice.m4a
```

`normalize=0` keeps per-line levels; without it every added line gets quieter.

### 4b. Music bed (optional)

Measure the bed once, then apply the gain that puts it at −30 LUFS plus `music_volume`:

```bash
ffmpeg -hide_banner -nostats -i audio/bed.mp3 -t 18.2 \
  -af "loudnorm=print_format=json" -f null - 2>&1 | grep -E 'input_(i|tp)'
# GAIN = min(-30 - input_i + music_volume, -2 - input_tp)      (music_volume defaults to 0)
# Only those two numbers are used: the encoded output is thrown away, so loudnorm's own
# I/TP/LRA targets would not change the measurement.
```

`-t` is the length of the finished video. The bed loops from its own start, so nothing past the
first `TOTAL` seconds ever reaches the mix and decoding the rest is pure waiting. It also changes
the *answer* for a track whose level moves: an intro at −45 LUFS with a loud drop at 1:30 is now
judged on the intro — the part the short actually plays — instead of being dragged down to the
drop's level and leaving the opening inaudible. Re-run the measurement when the cut changes
length, because a longer short hears more of the track and gets a different gain.

The true-peak term is the safety rail. Loudness normalisation only knows about averages, so a
quiet ambient bed with one percussive hit can measure −45 LUFS at −5 dBTP: the loudness gain
(+15 dB) would push that hit to +10 dBTP and clip the encoder. Capping the gain at
`-2 - input_tp` keeps the bed under −2 dBTP and, in that case, silently costs it some loudness
instead of clipping — check `music_gain_db` in the render summary when a bed lands quieter than
expected.

Loop the bed over the whole video, level it, fade the tail, duck it under the voice, then sum:

```bash
ffmpeg -y -i vo/01.mp3 -i vo/02.mp3 -i vo/03.mp3 -stream_loop -1 -i audio/bed.mp3 \
  -filter_complex "\
[0:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,adelay=0|0[a0];\
[1:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,adelay=5190|5190[a1];\
[2:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,adelay=12480|12480[a2];\
[a0][a1][a2]amix=inputs=3:normalize=0:duration=longest[voice];\
[voice]asplit=2[vmix][vkey0];\
[vkey0]apad=whole_dur=18.2[vkey];\
[3:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,volume=8.6dB,\
atrim=0:18.2,asetpts=PTS-STARTPTS,afade=t=out:st=16.7:d=1.5[music];\
[music][vkey]sidechaincompress=threshold=0.05:ratio=8:attack=10:release=400:makeup=1[bed];\
[vmix][bed]amix=inputs=2:normalize=0:duration=longest,apad,atrim=0:18.2,\
loudnorm=I=-16:TP=-1.5:LRA=11[aout]" \
  -map "[aout]" -c:a aac -b:a 192k build/mix.m4a
```

- `-stream_loop -1` must sit immediately before the music `-i`, and `atrim` ends the loop.
- `sidechaincompress` takes `[main][sidechain]`: the bed is compressed by the voice, so the music
  drops a few dB whenever someone speaks and comes back in the gaps.
- `volume=8.6dB` above is `-30 − (−38.6) + 0` for a bed that measures −38.6 LUFS: the same bed
  mastered at −14 LUFS would get `-16dB` instead and end up at the same place. That is the point of
  the measurement pass — `music_volume` is an offset from −30 LUFS, not a raw gain on the file.
  The gain is capped at `-2 - input_tp` first, so a transient-heavy quiet track is levelled by its
  peak rather than its average and cannot clip. `makeup=1` stops the compressor from also
  brightening the bed.
- `duration=longest` keeps the bed playing through the outro tail; `atrim` then ends the mix at the
  video length, and the 1.5 s `afade` lands inside the last scene. The side-chain key must be
  padded to the same length first — `sidechaincompress` ends at the shorter input, so an unpadded
  voice silently truncates the bed at the last word.
- Check the result with `volumedetect` at −19 dB mean and confirm the words are still clear before
  publishing; a bed that measures fine can still mask sibilants.

## 5. Captions (libass)

`build/caps.ass`:

```text
[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,Poppins,80,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,9,2,8,50,50,255,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:05.19,Main,,0,0,0,,{\fad(220,220)\an8}enemies learn.\Nyou don't
```

- `MarginV: 255` is the top inset for alignment 8 at 1920 tall (~13%). Raise it to push captions
  down; never let them pass 30% or they meet the subject.
- Timecodes are `H:MM:SS.cc` (centiseconds).
- `Fontname` is the family, resolved from `fontsdir`; pass the folder explicitly.

## 6. Final render

```bash
ffmpeg -y -i build/body.mp4 -i build/voice.m4a \
  -vf "subtitles=filename=build/caps.ass:fontsdir=assets/fonts" \
  -map 0:v -map 1:a -c:v libx264 -preset medium -crf 20 -pix_fmt yuv420p -r 30 \
  -c:a aac -b:a 192k -shortest -movflags +faststart build/short.mp4
```

## 7. QA

`scripts/render_short.py project.json --verify build/short.mp4` runs all of this in one pass and
exits `3` on a violation; these are the same checks by hand.

```bash
ffprobe -v error -show_entries format=duration,size:stream=codec_type,codec_name,width,height,r_frame_rate \
  -of default=nw=1 build/short.mp4

# one frame per scene, to check caption placement
ffmpeg -y -ss 2.0 -i build/short.mp4 -frames:v 1 build/frame-01.jpg

# loudness and true peak: -16 +/-1.5 LUFS, true peak at or below -1 dBTP
ffmpeg -hide_banner -nostats -i build/short.mp4 -af loudnorm=print_format=json -f null - 2>&1 \
  | grep -E '"input_(i|tp)"'

# silences, then compare each against the pause the timeline plans for it
ffmpeg -hide_banner -nostats -i build/short.mp4 -af "silencedetect=noise=-45dB:d=1.0" -f null -
```

A correctly built short has silence only in the gaps between scenes and in the outro pad; a
gap that runs 0.8 s past what the timeline allows means a scene has no narration file. With a
music bed there is nothing at -45 dB to detect, so run this on a voice-only render as well.
