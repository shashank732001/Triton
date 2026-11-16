"""
The six compatibility patches from the 2026 Colab notebook, with Colab's /content paths replaced
by WORK_DIR and the venv's own package locations. Idempotent: safe to run again.
"""

import importlib.util
import os
import re
import shutil
from pathlib import Path

WORK = Path(os.environ["WORK_DIR"])
PSG_GS = WORK / "piper-sample-generator" / "generate_samples.py"
OWW_PKG = WORK / "openwakeword" / "openwakeword"
VOICE = WORK / "piper-sample-generator" / "models" / "en_US-libritts_r-medium.pt"


def package_dir(name):
    return Path(importlib.util.find_spec(name).submodule_search_locations[0])


# A: torch_audiomentations calls torchaudio.set_audio_backend, removed in torchaudio 2.x
io_py = package_dir("torch_audiomentations") / "utils" / "io.py"
text = io_py.read_text()
if 'torchaudio.set_audio_backend("soundfile")' in text:
    io_py.write_text(text.replace('torchaudio.set_audio_backend("soundfile")', "pass  # patched"))
print(f"  A set_audio_backend no-op: {io_py}")

# E: generate_samples() requires a `model` argument that train.py doesn't pass -> default to our voice
text = PSG_GS.read_text()
text = re.sub(r"model: Union\[str, Path\](?: = \"[^\"]*\")?,", f'model: Union[str, Path] = "{VOICE}",', text, count=1)
PSG_GS.write_text(text)
print(f"  E generate_samples default voice: {VOICE.name}")

# B: train.py imports generate_samples from inside the openwakeword package
shutil.copy(PSG_GS, OWW_PKG / "generate_samples.py")
print(f"  B copied generate_samples.py into {OWW_PKG}")

# C (HF Hub timeouts) is done with environment variables in download_data.py

# D: torchaudio.info was removed in torchaudio 2.x; add a soundfile-based stand-in
init_py = package_dir("torchaudio") / "__init__.py"
marker = "# -- PATCH: info() shim for torchaudio 2.x --"
if marker not in init_py.read_text():
    with open(init_py, "a") as f:
        f.write(f"\n\n{marker}\n"
                "def info(file_path, *args, **kwargs):\n"
                "    import soundfile as _sf\n"
                "    si = _sf.info(str(file_path))\n"
                "    return type('_TorchaudioInfo', (), {\n"
                "        'num_frames': si.frames, 'sample_rate': si.samplerate,\n"
                "        'num_channels': si.channels, 'bits_per_sample': 16, 'encoding': 'PCM_S',\n"
                "    })()\n")
print(f"  D torchaudio.info shim: {init_py}")

# F: validation tensors need a float cast in train.py
train_py = OWW_PKG / "train.py"
text = train_py.read_text()
text = re.sub(r"val_predictions = self\.model\(x_val\)$", "val_predictions = self.model(x_val.float())", text, flags=re.M)
train_py.write_text(text)
print("  F train.py validation float cast")
