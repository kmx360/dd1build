#!/usr/bin/env python3
"""
Xbox 360 DD1 XeDK NAND image builder.

Builds a raw NAND image (with spare bytes) from either:
  - An extracted NAND directory (output of extract.py)
  - An xboxrom_update.bin plus keyvault and other components
"""

import argparse
import datetime
import os

from nandio import Nand, BLOCK_SIZE
from nand import (
    Bldr,
    BldrFlash,
    XeCryptSwizzle,
    encrypt_bl_chain,
    encrypt_keyvault,
    make_dos_timestamp,
    parse_update_binary,
    write_fs,
    write_mobiles,
    MOBILE_TYPES,
    SMC_CONFIG_BLOCKS,
    SMC_CONFIG_START,
    DEFAULT_BL_ENTRY,
    DEFAULT_KV_ADDR,
    DEFAULT_SMC_ADDR,
    DEFAULT_SYSUPDATE_ADDR,
    FS_START_BLOCK,
)

# Reverse map for CLI: name -> block type
_MOBILE_NAME_TO_TYPE = {name.lower(): t for t, name in MOBILE_TYPES.items()}


def build_image(
    smc,
    kv,
    bootloaders,
    fs_entries=None,
    smc_config=None,
    mobiles=None,
    bad_blocks=None,
    bl_entry=DEFAULT_BL_ENTRY,
    bl_region_size=None,
):
    """
    Build a complete NAND image from components.

    smc: decrypted SMC bytes
    kv: decrypted keyvault bytes
    bootloaders: list of decrypted bootloader bytearrays (S2, S3, ...)
    fs_entries: list of (name, data, dos_timestamp) tuples, or None
    smc_config: raw SMC config bytes, or None
    mobiles: list of (block_type, data) tuples, or None
    bad_blocks: list of block indices to mark as bad, or None
    """
    nand = Nand.empty(bad_blocks)

    if bl_region_size is None:
        bl_region_size = DEFAULT_SYSUPDATE_ADDR

    # Encrypt components
    smc = bytearray(smc)
    XeCryptSwizzle(smc, decrypt=False)
    kv = encrypt_keyvault(bytearray(kv))
    bl_chain = encrypt_bl_chain(bootloaders)

    # Build the entire boot region as one contiguous buffer
    header = BldrFlash()
    header.Bldr.Magic = 0x0F3F
    header.Bldr.Build = Bldr.from_buffer_copy(bootloaders[0][:0x10]).Build
    header.Bldr.Qfe = Bldr.from_buffer_copy(bootloaders[0][:0x10]).Qfe
    header.Bldr.Flags = 0
    header.Bldr.Entry = bl_entry
    header.Bldr.Size = bl_region_size
    copyright_str = b"\xa9 2004-2005 Microsoft Corporation. All rights reserved."
    for i, b in enumerate(copyright_str[:64]):
        header.Copyright[i] = b
    header.KeyVaultSize = 0
    header.SysUpdateAddr = DEFAULT_SYSUPDATE_ADDR
    header.SysUpdateCount = 2
    header.KeyVaultVersion = 1810
    header.KeyVaultAddr = DEFAULT_KV_ADDR
    header.PatchSlotSize = 0
    header.SmcConfigAddr = 0
    header.SmcSize = len(smc)
    header.SmcAddr = DEFAULT_SMC_ADDR

    boot_region = bytearray(DEFAULT_SYSUPDATE_ADDR)
    boot_region[0 : len(bytes(header))] = bytes(header)
    boot_region[DEFAULT_SMC_ADDR : DEFAULT_SMC_ADDR + len(smc)] = smc
    boot_region[DEFAULT_KV_ADDR : DEFAULT_KV_ADDR + len(kv)] = kv
    boot_region[bl_entry : bl_entry + len(bl_chain)] = bl_chain
    nand.write_flat(0, boot_region)

    # SMC config
    if smc_config:
        nand.write_flat(SMC_CONFIG_START * BLOCK_SIZE, smc_config)

    # Write mobile blocks
    fs_start = FS_START_BLOCK
    if mobiles:
        fs_start = write_mobiles(nand, mobiles)

    # Build filesystem
    fs_root_id = None
    if fs_entries:
        fs_root_id = write_fs(nand, fs_entries, fs_start_block=fs_start)

    return nand, fs_root_id


def load_fs_entries(fs_dir):
    """Load filesystem entries from a directory."""
    entries = []
    for name in sorted(os.listdir(fs_dir)):
        path = os.path.join(fs_dir, name)
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as f:
            data = f.read()
        ts = int(os.path.getmtime(path))
        dt = datetime.datetime.fromtimestamp(ts)
        entries.append((name, data, make_dos_timestamp(dt)))
    return entries


def load_mobiles(mobile_dir):
    """Load mobile .dat files from a directory."""
    name_to_type = {f"{name}.dat": t for t, name in MOBILE_TYPES.items()}
    mobiles = []
    for fname in sorted(os.listdir(mobile_dir)):
        if fname not in name_to_type:
            continue
        fpath = os.path.join(mobile_dir, fname)
        if os.path.isfile(fpath):
            with open(fpath, "rb") as f:
                mobiles.append((name_to_type[fname], f.read()))
    return mobiles


def read_file(path):
    with open(path, "rb") as f:
        return f.read()


def parse_block_list(s):
    """Parse comma-separated hex block indices."""
    blocks = []
    for part in s.split(","):
        blocks.append(int(part.strip(), 0))
    return blocks


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Build Xbox 360 DD1 XeDK NAND image")
    parser.add_argument("-o", "--output", required=True, help="Output NAND image path")

    src = parser.add_argument_group("input sources")
    src.add_argument(
        "--update", help="xboxrom_update.bin (provides bootloaders and optionally SMC)"
    )
    src.add_argument(
        "-i",
        "--input-dir",
        help="Directory with extracted components (smc.bin, kv.bin, s2-s5.bin, fs/, etc.)",
    )

    over = parser.add_argument_group("component overrides")
    over.add_argument("--smc", help="SMC binary")
    over.add_argument("--keyvault", help="Keyvault binary")
    over.add_argument("--smc-config", help="SMC config binary")
    over.add_argument("--s2", help="S2 bootloader")
    over.add_argument("--s3", help="S3 bootloader")
    over.add_argument("--s4", help="S4 bootloader")
    over.add_argument("--s5", help="S5 bootloader")
    over.add_argument("--fs", help="Filesystem directory")
    for name in sorted(MOBILE_TYPES.values()):
        over.add_argument(
            f"--{name.lower()}", metavar="FILE", help=f"{name}.dat override"
        )

    adv = parser.add_argument_group("advanced")
    adv.add_argument(
        "--bad-blocks",
        help="Comma-separated hex block indices to mark bad (e.g. 0x3b0,0x100)",
    )

    args = parser.parse_args()

    bootloaders = []
    bl_names = []
    smc = None
    kv = None
    smc_config = None
    fs_entries = None
    mobiles = []

    # Load from update binary
    if args.update:
        with open(args.update, "rb") as f:
            update_data = f.read()
        bl_list, update_smc = parse_update_binary(update_data)
        bootloaders = [bl for _, bl in bl_list]
        bl_names = [name for name, _ in bl_list]
        smc = update_smc
        for i, (name, bl) in enumerate(bl_list):
            bldr = Bldr.from_buffer_copy(bl[:0x10])
            print(f"  {name.upper()} (update)\tBuild {bldr.Build}")
        if smc:
            print(f"  SMC (update)\t{len(smc):#x} bytes")

    # Load from input directory
    d = args.input_dir
    if d:
        if not bootloaders:
            for s in ("s2", "s3", "s4", "s5"):
                p = os.path.join(d, f"{s}.bin")
                if os.path.exists(p):
                    bootloaders.append(read_file(p))
                    bl_names.append(s)
            for name in bl_names:
                print(f"  {name.upper()}\t\t{os.path.join(d, name + '.bin')}")

        if smc is None:
            p = os.path.join(d, "smc.bin")
            if os.path.exists(p):
                smc = read_file(p)
                print(f"  SMC\t\t{p}")

        if kv is None:
            p = os.path.join(d, "kv.bin")
            if os.path.exists(p):
                kv = read_file(p)
                print(f"  KeyVault\t{p}")

        if smc_config is None:
            p = os.path.join(d, "smc_config.bin")
            if os.path.exists(p):
                smc_config = read_file(p)
                print(f"  SMC Config\t{p}")

        mobiles = load_mobiles(d)

    # Bootloader overrides (replace individual stages)
    bl_overrides = {"s2": args.s2, "s3": args.s3, "s4": args.s4, "s5": args.s5}
    for name, path in bl_overrides.items():
        if path is None:
            continue
        data = read_file(path)
        if name in bl_names:
            idx = bl_names.index(name)
            bootloaders[idx] = data
        else:
            bootloaders.append(data)
            bl_names.append(name)
        bldr = Bldr.from_buffer_copy(data[:0x10])
        print(f"  {name.upper()} (override)\tBuild {bldr.Build}")

    # Other overrides
    if args.smc:
        smc = read_file(args.smc)
        print(f"  SMC\t\t{args.smc}")
    if args.keyvault:
        kv = read_file(args.keyvault)
        print(f"  KeyVault\t{args.keyvault}")
    if args.smc_config:
        smc_config = read_file(args.smc_config)
        print(f"  SMC Config\t{args.smc_config}")

    # Mobile overrides
    mobile_dict = {t: data for t, data in mobiles}
    for name in sorted(MOBILE_TYPES.values()):
        path = getattr(args, name.lower())
        if path:
            t = _MOBILE_NAME_TO_TYPE[name.lower()]
            mobile_dict[t] = read_file(path)
            print(f"  {name} (override)\t{path}")
    mobiles = [(t, data) for t, data in sorted(mobile_dict.items())]

    # Filesystem
    fs_dir = args.fs or (os.path.join(d, "fs") if d else None)
    if fs_dir and os.path.isdir(fs_dir):
        fs_entries = load_fs_entries(fs_dir)
        print(f"  FS\t\t{fs_dir} ({len(fs_entries)} files)")

    # Bad blocks
    bad_blocks = None
    if args.bad_blocks:
        bad_blocks = parse_block_list(args.bad_blocks)
        print(f"  Bad blocks\t{[f'{b:#x}' for b in bad_blocks]}")

    # Validate
    if not bootloaders:
        parser.error("No bootloaders provided (use --update or -i with s2-s5.bin)")
    if smc is None:
        parser.error("No SMC provided (use --smc, --update, or -i with smc.bin)")
    if kv is None:
        parser.error("No keyvault provided (use --keyvault or -i with kv.bin)")

    nand, fs_root_id = build_image(
        smc, kv, bootloaders, fs_entries, smc_config, mobiles or None, bad_blocks
    )

    if mobiles:
        print(f"  Mobiles\t{len(mobiles)} written")
    if fs_root_id is not None:
        print(f"  FS Root\t{fs_root_id:#x}")

    nand.save(args.output)
    print(f"  Output\t{args.output} ({len(nand.raw)} bytes)")


if __name__ == "__main__":
    main()
