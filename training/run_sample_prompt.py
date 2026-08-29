#!/usr/bin/env python3
"""Run Qwen3-30B-A3B-Base on a single completion prompt.

This is a base (non-instruct) model, so it does text completion, not chat -
pass raw prompt text to complete rather than a chat-formatted message.

Usage:
    python training/run_sample_prompt.py
    python training/run_sample_prompt.py --prompt "..." --max-new-tokens 128
"""
import argparse

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

DEFAULT_MODEL_DIR = "/mnt/gs21/scratch/moham147/obscure-model/models/Qwen3-30B-A3B-Base"

DEFAULT_PROMPT = (
    "// STM32 firmware: configure I2C1 in master mode at 400kHz and read a "
    "single byte from a sensor at address 0x68, register 0x00.\n"
    "void i2c1_read_sensor_byte(uint8_t *out) {\n"
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", default=DEFAULT_MODEL_DIR)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--max-new-tokens", type=int, default=128)
    parser.add_argument("--temperature", type=float, default=0.7)
    args = parser.parse_args()

    print(f"Loading tokenizer + model from {args.model_dir} ...")
    tokenizer = AutoTokenizer.from_pretrained(args.model_dir)
    model = AutoModelForCausalLM.from_pretrained(
        args.model_dir,
        dtype=torch.bfloat16,
        device_map="auto",
    )
    model.eval()

    inputs = tokenizer(args.prompt, return_tensors="pt").to(model.device)

    print("Prompt:")
    print(args.prompt)
    print("-" * 40)

    with torch.no_grad():
        output_ids = model.generate(
            **inputs,
            max_new_tokens=args.max_new_tokens,
            do_sample=args.temperature > 0,
            temperature=args.temperature if args.temperature > 0 else None,
            pad_token_id=tokenizer.eos_token_id,
        )

    completion = tokenizer.decode(
        output_ids[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True
    )
    print("Completion:")
    print(completion)


if __name__ == "__main__":
    main()
