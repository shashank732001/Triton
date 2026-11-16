r"""
Downloads what the benchmark runs: llama.cpp (CPU build) and the baseline GGUF models.

    .venv\Scripts\python.exe bench\fetch_models.py            # everything below (~8.7 GB)
    .venv\Scripts\python.exe bench\fetch_models.py qwen3-0.6b # just the files whose name contains this

Everything goes to %USERPROFILE%\TritonModels (or $TRITON_MODELS), outside OneDrive. Already
downloaded files are skipped. The llama.cpp build is pinned so results stay comparable; change
LLAMA_BUILD to move on (llama.cpp also publishes Vulkan and OpenVINO builds for the Intel GPU).
"""

import io
import os
import sys
import zipfile
from pathlib import Path

import requests
from huggingface_hub import hf_hub_download

STORE = Path(os.environ.get("TRITON_MODELS", Path.home() / "TritonModels"))
LLAMA_BUILD = "b11963"
LLAMA_ZIP = f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_BUILD}/llama-{LLAMA_BUILD}-bin-win-cpu-x64.zip"

# (repo, file): the small candidates at Q4_K_M, two at Q8_0 to see what 4-bit costs,
# and a 4B as the laptop's "big brain" reference
MODELS = [
    ("unsloth/Qwen3-0.6B-GGUF", "Qwen3-0.6B-Q4_K_M.gguf"),
    ("unsloth/Qwen3-0.6B-GGUF", "Qwen3-0.6B-Q8_0.gguf"),
    ("unsloth/Qwen3-1.7B-GGUF", "Qwen3-1.7B-Q4_K_M.gguf"),
    ("unsloth/Qwen3-1.7B-GGUF", "Qwen3-1.7B-Q8_0.gguf"),
    ("unsloth/gemma-3-1b-it-GGUF", "gemma-3-1b-it-Q4_K_M.gguf"),
    ("unsloth/Llama-3.2-1B-Instruct-GGUF", "Llama-3.2-1B-Instruct-Q4_K_M.gguf"),
    ("bartowski/SmolLM2-1.7B-Instruct-GGUF", "SmolLM2-1.7B-Instruct-Q4_K_M.gguf"),
    ("unsloth/Qwen3-4B-Instruct-2507-GGUF", "Qwen3-4B-Instruct-2507-Q4_K_M.gguf"),
]


def fetch_llama():
    target = STORE / "llama.cpp"
    if (target / "llama-server.exe").exists() and (target / "BUILD").exists() \
            and (target / "BUILD").read_text().strip() == LLAMA_BUILD:
        print(f"llama.cpp {LLAMA_BUILD}: already there")
        return
    print(f"llama.cpp {LLAMA_BUILD}: downloading {LLAMA_ZIP}")
    r = requests.get(LLAMA_ZIP, timeout=600)
    r.raise_for_status()
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        for member in z.infolist():
            name = Path(member.filename).name          # flatten: the zip may have a top folder
            if name and not member.is_dir():
                (target / name).write_bytes(z.read(member))
    (target / "BUILD").write_text(LLAMA_BUILD)
    print(f"  -> {target / 'llama-server.exe'}")


def fetch_models(only=None):
    models = STORE / "models"
    models.mkdir(parents=True, exist_ok=True)
    for repo, file in MODELS:
        if only and only.lower() not in file.lower():
            continue
        if (models / file).exists():
            print(f"{file}: already there")
            continue
        print(f"{file}: downloading from {repo}")
        hf_hub_download(repo, file, local_dir=models)


if __name__ == "__main__":
    fetch_llama()
    fetch_models(sys.argv[1] if len(sys.argv) > 1 else None)
    print(f"\nModels in {STORE / 'models'}")
