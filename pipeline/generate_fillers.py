"""
One-time, offline script to pre-synthesize the fallback filler clips
described in implementation.md Section 7.

Run this once after installing Piper and downloading a voice model:

    python generate_fillers.py --voice ./assets/voices/en_US-lessac-medium.onnx

Produces:
    assets/fillers/tier1/filler_0.wav ... filler_2.wav
    assets/fillers/tier2/filler_0.wav ... filler_1.wav

Recommendation from the spec: 4-6 phrases per tier so repeated fillers
within a single session don't feel obviously scripted. The lists below
start with the 3 + 2 phrases given in the spec as a baseline -- add
more to each list before your first real session.
"""

import argparse
from pathlib import Path

TIER1_PHRASES = [
    "Let me think about that for a second.",
    "Good question, one moment.",
    "Mm, let's see.",
]

TIER2_PHRASES = [
    "Still working through that, one moment.",
    "Almost there, thanks for waiting.",
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--voice",
        required=True,
        help="Path to a Piper .onnx voice model, e.g. en_US-lessac-medium.onnx",
    )
    parser.add_argument(
        "--output-dir",
        default="./assets/fillers",
        help="Root directory to write tier1/ and tier2/ subfolders into",
    )
    args = parser.parse_args()

    # Imported here, not at module scope, so this script's --help works
    # even before `piper-tts` is installed.
    import wave

    from piper import PiperVoice

    voice = PiperVoice.load(args.voice)

    output_root = Path(args.output_dir)
    tier1_dir = output_root / "tier1"
    tier2_dir = output_root / "tier2"
    tier1_dir.mkdir(parents=True, exist_ok=True)
    tier2_dir.mkdir(parents=True, exist_ok=True)

    def _synthesize(phrase: str, out_path: Path):
        # Current piper-tts API: PiperVoice.synthesize_wav(text, wav_file)
        # takes an *open* wave.Wave_write object, not a path/output_path
        # kwarg -- that older-looking API has been removed in recent
        # releases. If you're on an install that predates this, check
        # `python -c "from piper import PiperVoice; help(PiperVoice)"`
        # for whichever method name/signature your version exposes.
        with wave.open(str(out_path), "wb") as wav_file:
            voice.synthesize_wav(phrase, wav_file)

    for i, phrase in enumerate(TIER1_PHRASES):
        out_path = tier1_dir / f"filler_{i}.wav"
        _synthesize(phrase, out_path)
        print(f"wrote {out_path}")

    for i, phrase in enumerate(TIER2_PHRASES):
        out_path = tier2_dir / f"filler_{i}.wav"
        _synthesize(phrase, out_path)
        print(f"wrote {out_path}")

    print("\nDone. Add more phrases to TIER1_PHRASES / TIER2_PHRASES and "
          "re-run any time you want to expand the rotation.")


if __name__ == "__main__":
    main()