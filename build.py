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


def build_image(
    smc,
    kv,
    bootloaders,
    fs_entries=None,
    smc_config=None,
    mobiles=None,
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
    """
    nand = Nand.empty()

    if bl_region_size is None:
        bl_region_size = DEFAULT_SYSUPDATE_ADDR - bl_entry

    # Encrypt and place SMC
    smc = bytearray(smc)
    XeCryptSwizzle(smc, decrypt=False)
    nand.write_flat(DEFAULT_SMC_ADDR, smc)

    # Encrypt and place keyvault
    kv = encrypt_keyvault(bytearray(kv))
    nand.write_flat(DEFAULT_KV_ADDR, kv)

    # SMC config -- 4 blocks at 0xF7C-0xF7F
    if smc_config:
        smc_config = smc_config.ljust(SMC_CONFIG_BLOCKS * BLOCK_SIZE, b"\xff")
        for i in range(SMC_CONFIG_BLOCKS):
            block_data = smc_config[i * BLOCK_SIZE : (i + 1) * BLOCK_SIZE]
            nand.write_block(SMC_CONFIG_START + i, block_data)

    # Encrypt and place bootloader chain
    bl_chain = encrypt_bl_chain(bootloaders)
    nand.write_flat(bl_entry, bl_chain)

    # Build and place BldrFlash header
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
    nand.write_flat(0, bytes(header))

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
    src.add_argument("--smc", help="SMC binary (overrides update/input-dir)")
    src.add_argument("--keyvault", help="Keyvault binary (overrides input-dir)")
    src.add_argument("--smc-config", help="SMC config binary")
    src.add_argument("--fs", help="Filesystem directory")
    args = parser.parse_args()

    bootloaders = []
    smc = None
    kv = None
    smc_config = None
    fs_entries = None
    mobiles = None

    # Load from update binary
    if args.update:
        with open(args.update, "rb") as f:
            update_data = f.read()
        bl_list, update_smc = parse_update_binary(update_data)
        bootloaders = [bl for _, bl in bl_list]
        smc = update_smc
        for name, _ in bl_list:
            print(
                f"  {name.upper()} (update)\tBuild {Bldr.from_buffer_copy(bootloaders[bl_list.index((name,_))][:0x10]).Build}"
            )
        if smc:
            print(f"  SMC (update)\t{len(smc):#x} bytes")

    # Load from input directory
    d = args.input_dir
    if d:
        if not bootloaders:
            bl_names = []
            for s in ("s2", "s3", "s4", "s5"):
                p = os.path.join(d, f"{s}.bin")
                if os.path.exists(p):
                    with open(p, "rb") as f:
                        bootloaders.append(f.read())
                    bl_names.append(s)
            for name in bl_names:
                print(f"  {name.upper()}\t\t{os.path.join(d, name + '.bin')}")

        if smc is None:
            p = os.path.join(d, "smc.bin")
            if os.path.exists(p):
                with open(p, "rb") as f:
                    smc = f.read()
                print(f"  SMC\t\t{p}")

        if kv is None:
            p = os.path.join(d, "kv.bin")
            if os.path.exists(p):
                with open(p, "rb") as f:
                    kv = f.read()
                print(f"  KeyVault\t{p}")

        if smc_config is None:
            p = os.path.join(d, "smc_config.bin")
            if os.path.exists(p):
                with open(p, "rb") as f:
                    smc_config = f.read()
                print(f"  SMC Config\t{p}")

        mobiles = load_mobiles(d)

    # Explicit overrides
    if args.smc:
        with open(args.smc, "rb") as f:
            smc = f.read()
        print(f"  SMC\t\t{args.smc}")
    if args.keyvault:
        with open(args.keyvault, "rb") as f:
            kv = f.read()
        print(f"  KeyVault\t{args.keyvault}")
    if args.smc_config:
        with open(args.smc_config, "rb") as f:
            smc_config = f.read()
        print(f"  SMC Config\t{args.smc_config}")

    # Filesystem
    fs_dir = args.fs or (os.path.join(d, "fs") if d else None)
    if fs_dir and os.path.isdir(fs_dir):
        fs_entries = load_fs_entries(fs_dir)
        print(f"  FS\t\t{fs_dir} ({len(fs_entries)} files)")

    # Validate
    if not bootloaders:
        parser.error("No bootloaders provided (use --update or -i with s2-s5.bin)")
    if smc is None:
        parser.error("No SMC provided (use --smc, --update, or -i with smc.bin)")
    if kv is None:
        parser.error("No keyvault provided (use --keyvault or -i with kv.bin)")

    nand, fs_root_id = build_image(
        smc,
        kv,
        bootloaders,
        fs_entries,
        smc_config,
        mobiles,
    )

    if mobiles:
        print(f"  Mobiles\t{len(mobiles)} written")
    if fs_root_id is not None:
        print(f"  FS Root\t{fs_root_id:#x}")

    nand.save(args.output)
    print(f"  Output\t{args.output} ({len(nand.raw)} bytes)")


if __name__ == "__main__":
    main()
