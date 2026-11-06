#!/usr/bin/env python3
"""Build the 15-second beauty-editorial film from brief.json with the Runway API.

Steps (each result is cached in output/, so a rerun only redoes what is missing):
  1. keyframe  - gen4_image turns the reference photo into the opening frame
                 (vanity scene, brief's styling, no text from the reference).
  2. clips     - one image-to-video clip per beat. Beat 1 starts on the keyframe,
                 every later beat starts on the last frame of the clip before it.
  3. audio     - voice line, room tone and timed sound effects.
  4. assemble  - ffmpeg joins the clips with hard cuts and mixes the audio.

Usage:
  python3 make_video.py --dry-run     # validate the brief, print requests, spend nothing
  python3 make_video.py               # generate everything that is missing
  python3 make_video.py --redo 03_storm   # regenerate one beat (and the beats after it)

Auth: the key is read from RUNWAYML_API_SECRET. In a Claude cloud session the
network proxy injects the saved key, so no variable is needed there.
"""
import argparse
import base64
import json
import mimetypes
import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

API = "https://api.dev.runwayml.com/v1"
API_VERSION = "2024-11-06"
ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output"
FPS = 24
PROMPT_LIMIT = 1000


# --------------------------------------------------------------------------- API

class Runway:
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        key = os.environ.get("RUNWAYML_API_SECRET", "key_proxy-injected")
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {key}",
            "X-Runway-Version": API_VERSION,
            "Content-Type": "application/json",
        })

    def _request(self, method, path, body=None):
        delay = 5
        for attempt in range(8):
            r = self.s.request(method, f"{API}{path}", json=body, timeout=60)
            if r.status_code == 429 or r.status_code >= 500:
                wait = delay + random.uniform(0, 2)
                print(f"    {r.status_code} from Runway, retrying in {wait:.0f}s")
                time.sleep(wait)
                delay = min(delay * 2, 60)
                continue
            if r.status_code >= 400:
                sys.exit(f"Runway rejected {method} {path} ({r.status_code}): {r.text[:800]}")
            return r.json()
        sys.exit(f"Runway kept failing on {method} {path}")

    def run(self, endpoint, body, label):
        """Create a task, poll it to completion, return its output URLs."""
        if self.dry_run:
            shown = json.loads(json.dumps(body))
            _shorten_data_uris(shown)
            print(f"  [dry-run] POST /v1/{endpoint}  ({label})\n" + _indent(json.dumps(shown, indent=2)))
            return None
        task = self._request("POST", f"/{endpoint}", body)
        tid = task["id"]
        print(f"  {label}: task {tid} submitted")
        while True:
            time.sleep(5 + random.uniform(0, 1.5))  # Runway asks for >=5s with jitter
            t = self._request("GET", f"/tasks/{tid}")
            status = t["status"]
            if status == "SUCCEEDED":
                print(f"  {label}: done ({t.get('cost', {}).get('credits', '?')} credits)")
                return t["output"]
            if status in ("FAILED", "CANCELLED"):
                sys.exit(f"{label} {status}: {t.get('failure')} [{t.get('failureCode')}]")
            prog = t.get("progress")
            print(f"    {label}: {status}" + (f" {prog:.0%}" if isinstance(prog, (int, float)) else ""))


def _shorten_data_uris(o):
    if isinstance(o, dict):
        for k, v in o.items():
            if isinstance(v, str) and v.startswith("data:"):
                o[k] = f"{v[:30]}...<{len(v)} chars>"
            else:
                _shorten_data_uris(v)
    elif isinstance(o, list):
        for v in o:
            _shorten_data_uris(v)


def _indent(s):
    return "\n".join("    " + line for line in s.splitlines())


def data_uri(path):
    mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    raw = Path(path).read_bytes()
    uri = f"data:{mime};base64,{base64.b64encode(raw).decode()}"
    if len(uri) > 5 * 1024 * 1024:
        sys.exit(f"{path} is over Runway's 5MB data-URI limit; save it as a smaller JPEG.")
    return uri


def download(url, dest):
    try:
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)
    except requests.exceptions.ProxyError:
        sys.exit(f"Download blocked by the network proxy. Allow host '{urlparse(url).hostname}' "
                 "in the cloud environment's network settings, then rerun (finished steps are cached).")
    return dest


# ------------------------------------------------------------------------ ffmpeg

def ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def last_frame(video, dest):
    ff("-sseof", "-0.1", "-i", str(video), "-frames:v", "1", "-q:v", "2", str(dest))
    return dest


def normalize(src, dest, seconds, w, h):
    ff("-i", str(src), "-t", str(seconds), "-an",
       "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},fps={FPS},setsar=1",
       "-c:v", "libx264", "-preset", "slow", "-crf", "16", "-pix_fmt", "yuv420p", str(dest))


# ------------------------------------------------------------------------- steps

def validate(brief):
    problems = []
    for b in brief["beats"]:
        n = len(f"{brief['style']} {b['prompt']}")
        if n > PROMPT_LIMIT:
            problems.append(f"beat {b['name']}: prompt is {n} chars (limit {PROMPT_LIMIT})")
        if not (2 <= b["duration"] <= 10) or int(b["duration"]) != b["duration"]:
            problems.append(f"beat {b['name']}: duration must be a whole number from 2 to 10")
    if len(brief["keyframe_prompt"]) > PROMPT_LIMIT:
        problems.append(f"keyframe_prompt is {len(brief['keyframe_prompt'])} chars (limit {PROMPT_LIMIT})")
    if "@muse" not in brief["keyframe_prompt"]:
        problems.append("keyframe_prompt must mention @muse (the reference image tag)")
    if not (ROOT / brief["reference_image"]).exists():
        problems.append(f"reference image {brief['reference_image']} not found")
    for p in problems:
        print("  PROBLEM:", p)
    if problems:
        sys.exit(1)
    total = sum(b["duration"] for b in brief["beats"])
    print(f"  brief OK: {len(brief['beats'])} beats, {total}s total")
    return total


def make_keyframe(rw, brief):
    dest = OUT / "00_keyframe.png"
    if dest.exists():
        print("  keyframe: cached")
        return dest
    body = {
        "model": brief["image_model"],
        "ratio": brief["image_ratio"],
        "promptText": brief["keyframe_prompt"],
        "referenceImages": [{"uri": data_uri(ROOT / brief["reference_image"]), "tag": "muse"}],
        "seed": brief["seed"],
    }
    urls = rw.run("text_to_image", body, "keyframe")
    return download(urls[0], dest) if urls else dest


def make_clips(rw, brief, keyframe, redo):
    w, h = map(int, brief["video_ratio"].split(":"))
    start = keyframe
    clips, invalidate = [], False
    for b in brief["beats"]:
        raw = OUT / f"{b['name']}_raw.mp4"
        clip = OUT / f"{b['name']}.mp4"
        if b["name"] == redo:
            invalidate = True
        if invalidate:
            raw.unlink(missing_ok=True)
            clip.unlink(missing_ok=True)
        if clip.exists():
            print(f"  {b['name']}: cached")
        else:
            body = {
                "model": brief["video_model"],
                "ratio": brief["video_ratio"],
                "duration": int(b["duration"]),
                "promptText": f"{brief['style']} {b['prompt']}",
                "promptImage": data_uri(start) if start.exists() else "data:image/jpeg;base64,<from previous step>",
                "seed": brief["seed"],
            }
            urls = rw.run("image_to_video", body, b["name"])
            if urls:
                download(urls[0], raw)
                normalize(raw, clip, b["duration"], w, h)
        clips.append(clip)
        if clip.exists():
            start = last_frame(clip, OUT / f"{b['name']}_last.jpg")
    return clips


def make_audio(rw, brief, total):
    a = brief["audio"]
    parts = []  # (file, start_seconds, gain)

    voice = OUT / "audio_voice.mp3"
    if not voice.exists():
        urls = rw.run("text_to_speech", {
            "model": "seed_audio", "promptText": a["voice"]["text"], "outputFormat": "mp3",
            "speechRate": a["voice"]["speech_rate"], "pitchRate": a["voice"]["pitch_rate"],
        }, "voice")
        if urls:
            download(urls[0], voice)
    parts.append((voice, a["voice"]["at"], 1.0))

    room = OUT / "audio_room.mp3"
    if not room.exists():
        urls = rw.run("sound_effect", {
            "model": "eleven_text_to_sound_v2", "promptText": a["room_tone"]["prompt"],
            "duration": float(total), "loop": True,
        }, "room tone")
        if urls:
            download(urls[0], room)
    parts.append((room, 0.0, a["room_tone"]["gain"]))

    for fx in a["effects"]:
        f = OUT / f"audio_{fx['name']}.mp3"
        if not f.exists():
            urls = rw.run("sound_effect", {
                "model": "eleven_text_to_sound_v2", "promptText": fx["prompt"],
                "duration": max(0.5, float(fx["duration"])),
            }, f"sfx {fx['name']}")
            if urls:
                download(urls[0], f)
        parts.append((f, fx["at"], fx["gain"]))
    return parts


def assemble(clips, audio_parts, total):
    if not all(c.exists() for c in clips) or not all(p[0].exists() for p in audio_parts):
        print("  assemble: skipped (some pieces are missing)")
        return None
    lst = OUT / "clips.txt"
    lst.write_text("".join(f"file '{c.name}'\n" for c in clips))
    picture = OUT / "picture.mp4"
    ff("-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(picture))

    inputs, chains = [], []
    for i, (f, at, gain) in enumerate(audio_parts):
        inputs += ["-i", str(f)]
        ms = int(at * 1000)
        chains.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo,"
                      f"volume={gain},adelay={ms}|{ms}[a{i}]")
    mix = "".join(f"[a{i}]" for i in range(len(audio_parts)))
    graph = ";".join(chains) + f";{mix}amix=inputs={len(audio_parts)}:normalize=0,alimiter=limit=0.9,atrim=0:{total}[aout]"
    sound = OUT / "sound.wav"
    ff(*inputs, "-filter_complex", graph, "-map", "[aout]", str(sound))

    final = OUT / "final.mp4"
    ff("-i", str(picture), "-i", str(sound), "-map", "0:v", "-map", "1:a",
       "-c:v", "copy", "-c:a", "aac", "-b:a", "256k", "-t", str(total), "-movflags", "+faststart", str(final))
    return final


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--brief", default=str(ROOT / "brief.json"))
    ap.add_argument("--dry-run", action="store_true", help="print the requests, call nothing")
    ap.add_argument("--redo", metavar="BEAT", help="regenerate this beat and every beat after it")
    ap.add_argument("--redo-keyframe", action="store_true", help="regenerate the opening frame (and all beats)")
    args = ap.parse_args()

    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg is required")
    brief = json.loads(Path(args.brief).read_text())
    OUT.mkdir(exist_ok=True)

    print("1/4 checking brief")
    total = validate(brief)
    rw = Runway(dry_run=args.dry_run)

    print("2/4 keyframe")
    redo = args.redo
    if args.redo_keyframe:
        (OUT / "00_keyframe.png").unlink(missing_ok=True)
        redo = brief["beats"][0]["name"]
    keyframe = make_keyframe(rw, brief)

    print("3/4 clips")
    clips = make_clips(rw, brief, keyframe, redo)

    print("    audio")
    audio = make_audio(rw, brief, total)

    print("4/4 assemble")
    final = assemble(clips, audio, total)
    if final:
        print(f"\nDone: {final}")
    elif args.dry_run:
        print("\nDry run finished: nothing was sent and no credits were spent.")


if __name__ == "__main__":
    main()
