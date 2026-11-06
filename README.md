# Motion4 Mother

Builds a 15-second live-action beauty-editorial film from one reference photo using the Runway API.

## How it works

`make_video.py` reads `brief.json` and runs four steps. Each result is cached in `output/`, so a rerun only redoes what is missing.

1. **Keyframe.** `gen4_image` turns the reference photo (tagged `@muse`) into the opening frame: the vanity scene with the brief's styling, and none of the text or logos from the reference.
2. **Clips.** For each of the 4 beats (3s + 4s + 5s + 3s), `gen4.5` makes one image-to-video clip. Beat 1 starts on the keyframe, and each later beat starts on the last frame of the clip before it, so the cuts line up.
3. **Sound.** Runway text-to-speech says "Let's create a look." Sound effects provide the room tone, lipstick flick and catch, the low swell at the storm, and the products settling. Each sound is placed at a set time.
4. **Assemble.** ffmpeg joins the clips with hard cuts (720×1280, 24fps) and mixes the sound. The result is `output/final.mp4`.

## Usage

```bash
pip install -r requirements.txt          # ffmpeg must also be installed
cp /path/to/photo.jpg assets/reference.jpg   # JPEG, under ~3.5MB
python3 make_video.py --dry-run          # check the brief and print every request, without spending credits
python3 make_video.py                    # generate
python3 make_video.py --redo 03_storm    # redo one beat and every beat after it
python3 make_video.py --redo-keyframe    # redo the opening frame and all beats
```

To change the look, edit `brief.json`. Prompts can be up to 1000 characters; the script checks the length.

## API key

The script reads the key from `RUNWAYML_API_SECRET`. The key must start with `key_`; you create one in the Runway developer portal. In a Claude cloud session using the "Motion4-Mother-" environment, the key is saved as a network secret and the proxy adds it to each request, so no variable is needed.
