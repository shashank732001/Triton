# Train wake words on the university cluster

Trains the two custom wake words on an A100: **"Hey Echo"** (this PC) and **"Hey Triton"** (the PC with
Nova Launcher). Based on the 2026 openWakeWord Colab notebook
(github.com/alfiedennen/openwakeword-colab-2026), split for a Slurm cluster whose GPU nodes have no
internet, plus your own recordings so it learns your voice.

Everything (Python environment, code, ~25 GB of data, caches, temp files, models) goes into
`$WORK/hey_nova`, so your small home quota stays untouched (`echo $WORK`: `/home/woody/iwem/iwem119h`).

## 1. Record your voice (laptop, ~3 min per round)

```
cd "C:\Users\shash\TritonBundle\Voice Assistant"
.venv\Scripts\python.exe record_samples.py --word hey_echo
.venv\Scripts\python.exe record_samples.py --word hey_echo --more
.venv\Scripts\python.exe record_samples.py --word hey_triton
.venv\Scripts\python.exe record_samples.py --word hey_triton --more
```

## 2. Upload (laptop, PowerShell)

```
scp -r "C:\Users\shash\TritonBundle\Voice Assistant\cluster_training" iwem119h@tinyx:/home/woody/iwem/iwem119h/
```

If `$WORK/hey_nova` doesn't exist any more, run setup on the LOGIN node first (~30-60 min):
`cd $WORK/cluster_training && tmux new -s train`, then `bash setup.sh` until "Pre-flight PASSED".

Then your recordings:

```
ssh iwem119h@tinyx "mkdir -p /home/woody/iwem/iwem119h/hey_nova/my_voice"
scp -r "C:\Users\shash\TritonBundle\Voice Assistant\voice_samples\hey_echo" "C:\Users\shash\TritonBundle\Voice Assistant\voice_samples\hey_triton" iwem119h@tinyx:/home/woody/iwem/iwem119h/hey_nova/my_voice/
```

## 3. Submit both jobs (cluster)

Triton waits for Echo to finish (both share the prepared data, so they shouldn't build it at once):

```
cd $WORK/cluster_training
jid=$(sbatch --parsable train.slurm)                                   # "hey echo"  (~1.5-2.5 h)
WAKE_PHRASE="hey triton" sbatch --dependency=afterany:$jid train.slurm  # "hey triton" (~1.5-2.5 h)
squeue --me                                                            # PD = waiting, R = running
tail -f wakeword_*.log
```

Training prints lines like `recall=0.82 your_voice=0.75 fp/hr=0.31`:
- **recall**: how often it catches the phrase in computer voices it never trained on
- **your_voice**: how often it catches your held-back takes (the number that matters most)
- **fp/hr**: false alarms per hour of ordinary speech (lower is better)

## 4. Bring it home (laptop, PowerShell)

For each of `hey_echo` and `hey_triton`:

```
scp iwem119h@tinyx:/home/woody/iwem/iwem119h/hey_nova/hey_echo.onnx "C:\Users\shash\TritonBundle\Voice Assistant\models\"
scp -r iwem119h@tinyx:/home/woody/iwem/iwem119h/hey_nova/hey_echo_output/hey_echo/positive_test "C:\Users\shash\TritonBundle\Voice Assistant\models\hey_echo_many_voices"
```

Put the logs (`wakeword_*.log`) next to the models, then train the voice checks on the laptop
(`train_voice_check.py --word hey_echo` / `--word hey_triton`).

## Cleaning up

Everything lives in `$WORK/hey_nova`. Delete it when you're happy: `rm -rf $WORK/hey_nova`.
