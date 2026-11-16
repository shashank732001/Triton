r"""
Packs everything the N95 needs into one folder you can copy over (USB stick or network share):

    .venv\Scripts\python.exe make_n95_bundle.py
        --brain C:\Users\shash\TritonModels\trained\triton-Qwen3-0.6B-Q4_K_M.gguf   (default)
        --wake hey_triton            the N95's wake word (default; this PC uses hey_echo)
        --out <this folder>\dist\TritonVoice                                     (default)

The folder holds the code, settings.json for the N95, and every model at the path the assistant
looks for first, so nothing depends on %USERPROFILE%\TritonModels on the N95:
    models\triton-mini.gguf           the brain (fine-tuned, so it gets the one-line prompt)
    models\<wake>.onnx (+ _verifier)  wake word          models\vosk-model-small-en-us-0.15  instant ears
    models\stt\sherpa-onnx-whisper-base.en (int8 only)  better ears
    models\piper\<voice>.onnx(.json) or models\tts\<voice folder>   Triton's voice (settings.json "voice")
    llama.cpp\  llama-server and its DLLs
On the N95: run setup_n95.ps1 in that folder once (Python venv + packages), then start.vbs.
Re-run this after a better brain (round 2): only changed files are copied again.
"""

import argparse
import json
import shutil
import socket
from pathlib import Path

from speaker import find_voice

ROOT = Path(__file__).resolve().parent
STORE = Path.home() / "TritonModels"
CODE_DIRS = ["brain", "tools", "sounds"]
CODE_FILES = ["shortcuts.json", "requirements.txt", "start.vbs", "setup_n95.ps1", "Start Triton.bat", "Test Console.bat"]
SKIP_PY = {"make_n95_bundle.py", "analyze_clips.py", "brain_server.py"}      # laptop-only
WHISPER = "sherpa-onnx-whisper-base.en"
WHISPER_SMALL = "sherpa-onnx-whisper-small.en"
STREAMING_EARS = "moonshine-medium-streaming"     # TritonModels\stt\<this> (copied from moonshine-voice's cache)


def copy(src, dst):
    """Copy a file or folder, skipping files that are already there with the same size."""
    src, dst = Path(src), Path(dst)
    if src.is_dir():
        for f in src.rglob("*"):
            if f.is_file() and "__pycache__" not in f.parts:
                copy(f, dst / f.relative_to(src))
        return
    if dst.exists() and dst.stat().st_size == src.stat().st_size and dst.stat().st_mtime >= src.stat().st_mtime:
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    print(f"  {dst.relative_to(OUT)}  ({src.stat().st_size / 2**20:.0f} MB)" if src.stat().st_size > 2**20
          else f"  {dst.relative_to(OUT)}")


def primary_ip():
    """This PC's address on the network it uses for the internet (not a hotspot or virtual adapter).
    A UDP 'connect' only picks the route; nothing is sent."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]


def main():
    global OUT
    ap = argparse.ArgumentParser()
    ap.add_argument("--brain", type=Path, default=STORE / "trained" / "triton-Qwen3-0.6B-Q4_K_M.gguf")
    ap.add_argument("--wake", default="hey_triton")
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "dist" / "TritonVoice")
    # The N95's own voice: Piper medium says a short reply in ~0.25 s there; Kokoro took 1.5-2.5 s (too slow).
    # The laptop keeps whatever settings.json says.
    ap.add_argument("--voice", default="en_US-hfc_female-medium", help="Piper name or sherpa voice folder")
    ap.add_argument("--speaker", default="", help="speaker for Kokoro/Kitten voices")
    ap.add_argument("--brain-url", help="the laptop big brain's address (default: this PC's address, big_brain.json port)")
    ap.add_argument("--brain-mode", default="fallback", choices=["fallback", "first", "always"])
    a = ap.parse_args()
    OUT = a.out
    settings = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
    voice = a.voice

    needed = {
        a.brain: OUT / "models" / "triton-mini.gguf",
        ROOT / "models" / f"{a.wake}.onnx": OUT / "models" / f"{a.wake}.onnx",
        ROOT / "models" / "vosk-model-small-en-us-0.15": OUT / "models" / "vosk-model-small-en-us-0.15",
        STORE / "llama.cpp": OUT / "llama.cpp",
    }
    found = find_voice(voice, ROOT)                 # a Piper voice, or a sherpa-onnx voice folder (Kokoro, Kitten...)
    if found and found[0] == "sherpa":
        needed[found[1]] = OUT / "models" / "tts" / voice
    else:
        needed[ROOT / "models" / "piper" / f"{voice}.onnx"] = OUT / "models" / "piper" / f"{voice}.onnx"
        needed[ROOT / "models" / "piper" / f"{voice}.onnx.json"] = OUT / "models" / "piper" / f"{voice}.onnx.json"
    missing = [str(p) for p in needed if not p.exists()]
    if missing:
        raise SystemExit("Missing, so nothing was copied:\n  " + "\n  ".join(missing))

    print(f"Bundling into {OUT}")
    for py in ROOT.glob("*.py"):
        if py.name not in SKIP_PY:
            copy(py, OUT / py.name)
    for name in CODE_DIRS + CODE_FILES:
        copy(ROOT / name, OUT / name)
    for src, dst in needed.items():
        copy(src, dst)
    verifier = ROOT / "models" / f"{a.wake}_verifier.pkl"
    if verifier.exists():
        copy(verifier, OUT / "models" / verifier.name)
    # openWakeWord's own model files (feature extractor, VAD, the built-in fallback word) aren't in its pip
    # package; setup_n95.ps1 copies these into the venv, so the N95 needn't download them
    import openwakeword
    for f in (Path(openwakeword.__file__).parent / "resources" / "models").glob("*.onnx"):
        copy(f, OUT / "openwakeword_models" / f.name)
    for name in (WHISPER, WHISPER_SMALL):        # small hears every command; base stays as a lighter fallback
        whisper = STORE / "stt" / name
        for f in [*whisper.glob("*.int8.onnx"), *whisper.glob("*tokens.txt")]:   # int8 is what transcriber loads
            copy(f, OUT / "models" / "stt" / name / f.name)
    # Streaming ears (Moonshine medium, hears while you talk; 2026-11-05): whisper-small above is now only the
    # second listen when nothing in a command was understood
    for f in (STORE / "stt" / STREAMING_EARS).glob("*"):
        copy(f, OUT / "models" / "stt" / STREAMING_EARS / f.name)

    # The N95's own settings: its wake word, the bundled brain (default path), same voice and ears
    # wake_threshold 0.5 for hey_triton (round 2 + voice check, Rapoo mic): your 32 test takes all pass (lowest
    # 0.84) and 0/500 other voices do; at 0.2, 1/500 did (README section 6)
    n95 = {"wake_word": a.wake, "wake_threshold": 0.5 if a.wake == "hey_triton" else 0.2,
           "voice": {"name": voice, "speaker": a.speaker, "speed": 1.15, "enabled": True},
           # whisper-small hears every command, alone (2026-11-04: base's quick pass was re-heard by small on nearly
           # every real sentence anyway; small alone is both quicker overall and right more often)
           # 2026-11-05: Moonshine streaming hears every command (text ready ~0.03 s after you stop, vs ~2 s laptop /
           # ~4 s N95 for whisper-small); whisper-small listens again only when nothing was understood
           "ears": {**settings.get("ears", {"threads": 4, "enabled": True}), "model": STREAMING_EARS,
                    "second_opinion": "whisper-small"},
           # Triton-Mini v2 ("converse" in its file name) is copied as triton-mini.gguf too: say which prompt it takes
           "brain": {"threads": 4, **({"prompt": "converse-mini"} if "converse" in a.brain.name else {})}}
    # The laptop's big brain (brain_server.py), paired once here: its address, key and certificate fingerprint
    # (HTTPS with that certificate pinned). load_config() makes the certificate if the server never ran yet.
    import brain_server
    if (ROOT / "big_brain.json").exists() or a.brain_url:
        cfg = brain_server.load_config()
        # "fallback": the N95's own model answers first (~1.5 s); the laptop gets questions and what the N95 can't
        # place. "first" sent most commands to the laptop's 9B (6-17 s each), which felt slow.
        n95["big_brain"] = {"url": a.brain_url or f"https://{primary_ip()}:{cfg['port']}",
                            "urls": [f"https://{socket.gethostname()}:{cfg['port']}"],   # if the address changes
                            "key": cfg["key"], "cert_sha256": cfg["cert_sha256"], "mode": a.brain_mode, "timeout": 25}
        print(f"  big brain: {n95['big_brain']['url']} (certificate {cfg['cert_sha256'][:16]}...)")
    # Voice control of Triton Screen Share: on the TV the laptop's screen comes here
    n95["screen_share"] = {"role": "show"}
    (OUT / "settings.json").write_text(json.dumps(n95, indent=2), encoding="utf-8")
    (OUT / "BRAIN.txt").write_text(f"models\\triton-mini.gguf = {a.brain.name}\n", encoding="utf-8")
    size = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file()) / 2**20
    print(f"\nDone: {size:.0f} MB. Copy {OUT} to the N95, then run setup_n95.ps1 there once.")


if __name__ == "__main__":
    main()
