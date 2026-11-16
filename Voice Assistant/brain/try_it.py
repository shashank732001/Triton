r"""
Try the brain by typing instead of talking: the same rules -> model -> safety -> tools path the
voice assistant uses, so you can test new tools without the mic.

    .venv\Scripts\python.exe -m brain.try_it                       # type commands, Enter to run
    .venv\Scripts\python.exe -m brain.try_it "open notepad and type hello"
        --dry-run      only show what would be done
        --no-model     rules only (today's behaviour)
        --speak        say the replies out loud too (speaker.py)

Commands run for real (they open apps, click and type), after a 2-second pause so you can switch
to the window you mean. "confirm" tools ask y/n here instead of by voice.
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from apps import Catalog  # noqa: E402
from brain.llm import LocalLLM  # noqa: E402
from brain.router import Router  # noqa: E402
from commands import Commands, Failed  # noqa: E402
from monitor import MonitorControl  # noqa: E402
from speaker import Speaker  # noqa: E402
from tools import safety  # noqa: E402
from tools.handlers import Tools  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text", nargs="*")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-model", action="store_true")
    ap.add_argument("--delay", type=float, default=2.0)
    ap.add_argument("--speak", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="  %(message)s")

    settings = json.loads((ROOT / "settings.json").read_text()) if (ROOT / "settings.json").exists() else {}
    catalog = Catalog(ROOT / "shortcuts.json")
    llm = None if a.no_model else LocalLLM.from_settings(settings.get("brain", {}), ROOT)
    router = Router(Commands(catalog, None), catalog, llm)
    tools = Tools(catalog, MonitorControl())
    speaker = Speaker.from_settings(settings.get("voice", {}), ROOT) if a.speak else None
    catalog.ready.wait(60)
    if speaker:
        speaker.ready.wait(60)
    if llm:
        print("Loading the model...", end="", flush=True)
        llm.ready.wait(120)
        print(" ready")

    def say(text):
        print(f"  {text}")
        if speaker:
            speaker.say(text, wait=True)

    def handle(text):
        t0 = time.monotonic()
        calls = router.rules(text)
        tier = "rules"
        if calls is None:
            calls, tier = router.model(text), "model"
        took = (time.monotonic() - t0) * 1000
        if not calls:
            print(f"  -> didn't understand ({took:.0f} ms)")
            if speaker:
                speaker.say("Didn't catch that", wait=True)
            return
        print(f"  -> {tier} ({took:.0f} ms): {json.dumps(calls)}")
        if a.dry_run:
            return
        time.sleep(a.delay)
        for call in calls:
            verdict, why = safety.check(call)
            if verdict == "refuse":
                print(f"  refused: {why}")
                return
            if verdict == "confirm" and input(f"  {why} [y/N] ").strip().lower() not in ("y", "yes"):
                print("  skipped")
                return
            try:
                say(tools.run(call))
            except Failed as e:
                say(f"failed: {e}")
                return
            time.sleep(0.8)          # let the app react before the next step

    if a.text:
        handle(" ".join(a.text))
    else:
        print("Type a command (empty line to quit).")
        while line := input("> ").strip():
            handle(line)
    if llm:
        llm.close()


if __name__ == "__main__":
    main()
