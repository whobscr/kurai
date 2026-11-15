"""Listening to the music itself (the `kurai audio` container, Dockerfile.audio).

Two models, both on the CPU, at night:
- Essentia's Discogs-EffNet: a vector of how a track sounds ("sound"), plus readable tags from small heads on top of it
  (styles, moods, danceability, vocals) and BPM and key from Essentia's classic algorithms;
- CLAP (LAION's clap-htsat-unfused): a vector in a space shared by sound and words ("clap"), so «Спросить» can find
  tracks by a description of how they sound. (LAION's «larger_clap_music» on Hugging Face would fit music better, but
  its text side is broken: every description gets nearly the same vector.)

The main image never imports the heavy parts (Essentia, PyTorch): only these names, `tags` and `client`.
"""

EFFNET_MODEL = "discogs-effnet-bs64-1"
EFFNET_DIM = 1280
CLAP_MODEL = "laion/clap-htsat-unfused"
CLAP_REVISION = "8fa0f1c6d0433df6e97c127f64b2a1d6c0dcda8a"
CLAP_DIM = 512
