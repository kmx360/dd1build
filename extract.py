#!/usr/bin/env python3
"""
Xbox 360 DD1 XeDK NAND image extractor.

Extracts SMC, keyvault, SMC config, bootloaders (S2-S5), mobile blocks,
and SFCX filesystem contents from a raw NAND dump with spare bytes.
"""

import argparse
import os

from nandio import Nand, RESERVE_START
from nand import (
    Bldr,
    BldrFlash,
    XeCryptSwizzle,
    decrypt_bl_chain,
    decrypt_keyvault,
    read_fs,
    read_mobiles,
    scan_typed_blocks,
    KV_SIZE,
    SMC_CONFIG_START,
    SMC_CONFIG_BLOCKS,
)


def extract_image(input_path, output_dir):
    nand = Nand.from_file(input_path)

    # Bad block report
    bad = nand.bad_blocks
    remap = nand.remap_table
    if bad:
        print(f"  Bad blocks\t{len(bad)} found:")
        for block_idx, marker in bad:
            area = "reserve" if block_idx >= RESERVE_START else "data"
            repl = remap.get(block_idx)
            suffix = f" -> {repl:#06x}" if repl is not None else " (no remap!)"
            print(f"    block {block_idx:#06x} (marker={marker:#04x}, {area}){suffix}")
    else:
        print("  Bad blocks\tnone")

    # Parse header
    header_data = nand.read_flat(0, 0x200)
    header = BldrFlash.from_buffer_copy(header_data)
    print(f"  Flash\t\t{header}")

    # Scan all typed blocks (FS root, mobiles) in one pass
    typed_blocks = scan_typed_blocks(nand)

    os.makedirs(output_dir, exist_ok=True)

    # SMC
    if header.SmcSize > 0:
        smc = bytearray(nand.read_flat(header.SmcAddr, header.SmcSize))
        XeCryptSwizzle(smc, decrypt=True)
        path = os.path.join(output_dir, "smc.bin")
        with open(path, "wb") as f:
            f.write(smc)
        print(f"  SMC\t\t{header.SmcSize:#x} bytes -> {path}")

    # KeyVault
    if header.KeyVaultAddr > 0:
        kv_size = header.KeyVaultSize if header.KeyVaultSize > 0 else KV_SIZE
        kv = decrypt_keyvault(nand.read_flat(header.KeyVaultAddr, kv_size))
        path = os.path.join(output_dir, "kv.bin")
        with open(path, "wb") as f:
            f.write(kv)
        print(
            f"  KeyVault\t{kv_size:#x} bytes (version {header.KeyVaultVersion}) -> {path}"
        )

    # SMC Config (4 blocks at 0xF7C-0xF7F)
    smc_config = bytearray()
    for b in range(SMC_CONFIG_START, SMC_CONFIG_START + SMC_CONFIG_BLOCKS):
        smc_config += nand.read_block(b)
    # Check if non-empty
    if any(b != 0xFF for b in smc_config):
        path = os.path.join(output_dir, "smc_config.bin")
        with open(path, "wb") as f:
            f.write(smc_config)
        print(
            f"  SMC Config\t{len(smc_config):#x} bytes ({SMC_CONFIG_BLOCKS} blocks) -> {path}"
        )
    else:
        print("  SMC Config\tempty")

    # Bootloaders
    bootloaders = decrypt_bl_chain(nand, header)
    for name, bl in bootloaders:
        bldr = Bldr.from_buffer_copy(bl[:0x10])
        path = os.path.join(output_dir, f"{name}.bin")
        with open(path, "wb") as f:
            f.write(bl)
        print(f"  {name.upper()}\t\t{bldr}")

    # Mobile blocks
    mobiles = read_mobiles(nand, typed_blocks)
    if mobiles:
        for name, data in mobiles:
            path = os.path.join(output_dir, name)
            with open(path, "wb") as f:
                f.write(data)
            print(f"  Mobile\t{name} ({len(data):#x} bytes) -> {path}")
    else:
        print("  Mobiles\tnone")

    # Filesystem
    fs_root_id, files = read_fs(nand, typed_blocks)
    if files:
        print(f"  FS Root\t{fs_root_id:#x}")
        fs_dir = os.path.join(output_dir, "fs")
        os.makedirs(fs_dir, exist_ok=True)
        for name, file_data in files:
            path = os.path.join(fs_dir, name)
            with open(path, "wb") as f:
                f.write(file_data)
        print(f"  FS\t\t{len(files)} files -> {fs_dir}/")
    else:
        print("  FS Root\tnot found")


def main():
    parser = argparse.ArgumentParser(description="Extract Xbox 360 DD1 XeDK NAND image")
    parser.add_argument("input", help="Raw NAND image with spare bytes")
    parser.add_argument(
        "-o", "--output", default=".", help="Output directory (default: .)"
    )
    args = parser.parse_args()
    extract_image(args.input, args.output)


if __name__ == "__main__":
    main()
