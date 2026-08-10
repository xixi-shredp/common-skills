#!/usr/bin/env python3
"""Calculate the OpenSBI FDT relocation address required by XiangShan's guide."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


MIB = 1024 * 1024
IMAGE_LIMIT = 32 * MIB
KERNEL_LOAD_ADDRESS = 0x80000000
RESERVED_GAP = 2 * MIB


def align_up(value: int, alignment: int) -> int:
    return (value + alignment - 1) // alignment * alignment


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Report whether XiangShan OpenSBI needs FW_PAYLOAD_FDT_ADDR for a Linux Image."
    )
    parser.add_argument("image", type=Path, help="path to arch/riscv/boot/Image")
    parser.add_argument(
        "--format",
        choices=("text", "value", "json"),
        default="text",
        help="text is human-readable; value prints only the address when required",
    )
    args = parser.parse_args()

    try:
        image_size = args.image.stat().st_size
    except OSError as exc:
        parser.error(f"cannot stat {args.image}: {exc}")

    needed = image_size > IMAGE_LIMIT
    address = align_up(KERNEL_LOAD_ADDRESS + image_size + RESERVED_GAP, MIB) if needed else None
    payload = {
        "image": str(args.image),
        "image_bytes": image_size,
        "threshold_bytes": IMAGE_LIMIT,
        "needs_fw_payload_fdt_addr": needed,
        "fw_payload_fdt_addr": f"0x{address:x}" if address is not None else None,
    }

    if args.format == "json":
        print(json.dumps(payload, sort_keys=True))
    elif args.format == "value":
        if address is not None:
            print(f"0x{address:x}")
    elif needed:
        print(f"Image is {image_size} bytes (> {IMAGE_LIMIT}); use FW_PAYLOAD_FDT_ADDR=0x{address:x}")
    else:
        print(f"Image is {image_size} bytes (<= {IMAGE_LIMIT}); FW_PAYLOAD_FDT_ADDR is not required.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
