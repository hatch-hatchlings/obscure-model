#!/usr/bin/env python3
"""Download Qwen3-30B-A3B-Base from the Hugging Face Hub to scratch.

Mirrors how the model currently living at
/mnt/gs21/scratch/moham147/obscure-model/models/Qwen3-30B-A3B-Base was
fetched (a plain snapshot_download into that directory).

Usage:
    python training/download_qwen_model.py
    python training/download_qwen_model.py --model-id Qwen/Qwen3-30B-A3B-Base --out-dir /path/to/dir
"""
import argparse

from huggingface_hub import snapshot_download

DEFAULT_MODEL_ID = "Qwen/Qwen3-30B-A3B-Base"
DEFAULT_OUT_DIR = "/mnt/gs21/scratch/moham147/obscure-model/models/Qwen3-30B-A3B-Base"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    args = parser.parse_args()

    print(f"Downloading {args.model_id} to {args.out_dir} ...")
    snapshot_download(
        repo_id=args.model_id,
        local_dir=args.out_dir,
    )
    print("Done.")


if __name__ == "__main__":
    main()
