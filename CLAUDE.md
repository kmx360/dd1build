# dd1build

Tools to extract and rebuild raw NAND images for Xbox 360 DD1 XeDK
(early development kit) machines. DD1 uses an all-zero 1BL key and CPU
key, so all crypto is done with known keys.

## Layout

- `nandio.py` — low-level NAND I/O. Raw pages are 0x200 data + 0x10
  spare; blocks are 32 pages (0x4000 bytes); 0x1000 blocks total. Handles
  ECC, spare metadata, and bad-block remapping into the reserve area
  (blocks 0xF80–0xFFF). Everything above this goes through `Nand`'s
  logical/flat addressing (`read_flat`, `write_flat`, `read_block`, ...).
- `nand.py` — DD1 structures (`Bldr`, `BldrFlash`), crypto (SMC swizzle,
  keyvault, RC4 bootloader chain S2–S5), `xboxrom_update.bin` parsing,
  mobile blocks, and the SFCX filesystem (FAT + root dir via spare metadata).
- `xtaf.py` — XTAF flash filesystem used by early builds (e.g. 1640),
  located at block 0x80. Clusters are block-sized but offset by -0x1000
  from block boundaries, so writes build one contiguous buffer.
- `extract.py` — raw NAND → directory (`smc.bin`, `kv.bin`,
  `smc_config.bin`, `s2.bin`…, `pre_bl.bin`, mobile `.dat` files, `fs/`).
  Auto-detects XTAF vs SFCX.
- `build.py` — directory and/or `xboxrom_update.bin` → raw NAND. Per-
  component CLI overrides (`--smc`, `--s2`, `--fs`, mobile names, ...).

## Two flash layouts

| | 1838 (later) | 1640 (early) |
|---|---|---|
| Header magic | 0x0F3F | 0xFF3F |
| BL entry | 0x8000 | 0x1040 |
| SMC | 0x1000 | 0x64000 |
| Keyvault | 0x4000 | none |
| Filesystem | SFCX + mobiles | XTAF @ block 0x80 |

`build.py --layout` picks one; otherwise it is inferred from the S2 build
number (≤1640 → 1640). `pre_bl.bin` is extracted for 1640 images and
loaded by build, but writing it is currently commented out in
`build_image_1640`.

## Usage

```
./extract.py <nand.bin> -o <outdir>
./build.py -i <outdir> -o <new.bin>
./build.py --update xboxrom_update.bin --keyvault kv.bin -o <new.bin>
```

Requires Python 3 with `cryptography` (ARC4 comes from
`cryptography.hazmat.decrepit`). There is no test suite; verify by
extracting a known-good dump, rebuilding it, and comparing (`cmp`, or
re-extract and diff the directories).

## Repo hygiene

- NAND dumps (`*.bin`, 0x4200000 bytes), extracted dirs (`1640.settings/`,
  `1838/`, `dump.1640/`, ...), recovery ISOs, and other binaries live in
  the working tree but are not committed.
- `hdd.py`, `hddtool/` and the `hddtool-venv*` dirs are separate HDD/XTAF
  disk tooling and are not tracked in this repo.
