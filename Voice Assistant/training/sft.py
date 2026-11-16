r"""
Fine-tunes a student on the generated data (one GPU).

    python sft.py --student Qwen/Qwen3-1.7B                  # full fine-tune (small models)
    python sft.py --student Qwen/Qwen3-4B-Instruct-2507 --lora

The model learns to answer the one-line COMPACT prompt with the right tool calls, so at run time it
needs no tool list in its prompt (the "prompt diet"). Loss is only on the answer, not the request.
Saves the best checkpoint (lowest validation loss) to $WORK_DIR/runs/<name>/final, merged if LoRA.
"""

import argparse

import torch
from datasets import Dataset
from transformers import AutoModelForCausalLM, AutoTokenizer
from trl import SFTConfig, SFTTrainer

from pathlib import Path

from common import (DATA, RUNS, WORK, build_converse_prompt, build_prompt, completion, converse_completion, model_path,
                    read_jsonl)


def dataset(path, tok):
    """Round-2 rows ({"text", "calls"}) or Triton-Mini v2 rows ({"messages", "reply"}, gen_converse.py)."""
    rows = read_jsonl(path)
    if rows and "messages" in rows[0]:
        return Dataset.from_list([{"prompt": build_converse_prompt(tok, r["messages"]),
                                   "completion": converse_completion(r["reply"]) + tok.eos_token} for r in rows])
    return Dataset.from_list([{"prompt": build_prompt(tok, r["text"]),
                               "completion": completion(r["calls"]) + tok.eos_token} for r in rows])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--student", required=True)
    ap.add_argument("--name", help="run name (default: student name, plus -lora)")
    ap.add_argument("--lora", action="store_true", help="LoRA instead of full fine-tuning (for 4B+)")
    ap.add_argument("--epochs", type=float, default=2)
    ap.add_argument("--lr", type=float, help="default 1e-5 full / 2e-4 LoRA")
    ap.add_argument("--batch", type=int, default=64, help="effective batch size")
    ap.add_argument("--data", default=str(DATA), help="folder with train.jsonl / val.jsonl (data_v2: Triton-Mini v2)")
    a = ap.parse_args()
    data = Path(a.data) if Path(a.data).is_absolute() else WORK / a.data

    name = a.name or a.student.split("/")[-1] + ("-lora" if a.lora else "")
    out = RUNS / name
    lr = a.lr or (2e-4 if a.lora else 1e-5)
    path = model_path(a.student)
    tok = AutoTokenizer.from_pretrained(path)
    train, val = dataset(data / "train.jsonl", tok), dataset(data / "val.jsonl", tok)
    print(f"{name}: {len(train)} train / {len(val)} val examples, lr {lr}, {a.epochs} epochs", flush=True)

    # Full fine-tuning keeps fp32 master weights (bf16 only for the maths): in pure bf16, updates this
    # small (lr 1e-5) round away to nothing. ~27 GB for 1.7B, fits a 40 GB A100. LoRA trains small fp32
    # adapters on top, so its frozen base can stay bf16.
    # V100 (sft_v100.slurm): no bf16, so fp16 maths with loss scaling; 32 GB, so 8-bit Adam states
    # (~3 GB instead of ~14 GB for 1.7B) and smaller steps (same effective batch).
    bf16 = torch.cuda.is_bf16_supported()
    small_gpu = torch.cuda.get_device_properties(0).total_memory < 38 * 2**30
    half = torch.bfloat16 if bf16 else torch.float16
    dtype = half if a.lora else torch.float32
    model = AutoModelForCausalLM.from_pretrained(path, torch_dtype=dtype, attn_implementation="sdpa")
    per_device = 8 if small_gpu else 16
    print(f"  {torch.cuda.get_device_name(0)}: {'bf16' if bf16 else 'fp16'}, {per_device} per step"
          f"{', 8-bit Adam' if small_gpu and not a.lora else ''}", flush=True)
    peft_config = None
    if a.lora:
        from peft import LoraConfig
        peft_config = LoraConfig(r=32, lora_alpha=64, lora_dropout=0.05, task_type="CAUSAL_LM",
                                 target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                                                 "gate_proj", "up_proj", "down_proj"])

    cfg = SFTConfig(
        output_dir=str(out),
        num_train_epochs=a.epochs,
        learning_rate=lr,
        lr_scheduler_type="cosine",
        # 3% warm-up, as steps: newer transformers removed warmup_ratio
        warmup_steps=max(1, int(0.03 * a.epochs * len(train) / a.batch)),
        per_device_train_batch_size=per_device,
        per_device_eval_batch_size=32,
        gradient_accumulation_steps=max(1, a.batch // per_device),
        gradient_checkpointing=True,
        bf16=bf16,
        fp16=not bf16,
        optim="adamw_bnb_8bit" if small_gpu and not a.lora else "adamw_torch",
        max_length=512,          # v2 rows carry a screen line and earlier turns
        completion_only_loss=True,
        eval_strategy="steps",
        eval_steps=200,
        save_strategy="steps",
        save_steps=200,
        save_total_limit=2,
        load_best_model_at_end=True,
        metric_for_best_model="eval_loss",
        logging_steps=20,
        report_to="none",
        dataloader_num_workers=2,
        seed=0,
    )
    trainer = SFTTrainer(model=model, args=cfg, train_dataset=train, eval_dataset=val,
                         processing_class=tok, peft_config=peft_config)
    trainer.train()

    final = out / "final"
    model = trainer.model
    if a.lora:
        model = model.merge_and_unload()
    model.save_pretrained(final, safe_serialization=True)
    tok.save_pretrained(final)
    print(f"Saved {final}  (best eval_loss {trainer.state.best_metric})")


if __name__ == "__main__":
    main()
