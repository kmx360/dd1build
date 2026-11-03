# dd1build

Tools to extract and rebuild raw NAND images for Xbox 360 DD1 XeDK
machines. DD1 is a prototype XeDK with its own bootloaders. It uses an
all-zero 1BL key and CPU key, so all crypto is done with known keys.

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

Builds before 1838 use a different flash layout; 1838 introduced the final
format. Values observed in local dumps (`dump.1640.bin`,
`1640.clean.settings.bin`, `1746_1.bin`, `1838.clean.bin`):

| | 1640 | 1746 | 1838 |
|---|---|---|---|
| Header magic | 0xFF3F | 0xFF3F | 0x0F3F |
| Bootloader entry | 0x1040 | 0x14A0 | 0x8000 |
| SMC address | 0x34000 | 0x64000 | 0x1000 |
| Keyvault | none | none | 0x4000 |
| Bootloader stages | S2–S4 | S2–S5 | S2–S5 |
| Flash filesystem | XTAF at block 0x80 | XTAF at block 0x80 | SFCX |

`dump.1640.bin` also has mobile blocks (B, C, D, H, J) alongside XTAF.

`build.py` has `build_image_1838` and `build_image_1640`; `--layout`
picks one, otherwise S2 build ≤1640 → 1640. Known gaps in the code (do not
paper over these in docs):
- `build_image_1640` uses SMC 0x64000 (matches 1746, not 1640's 0x34000),
  BL entry 0x1040 (matches 1640, not 1746's 0x14A0), and ignores mobiles.
- 1746 auto-detects as 1838.
- `pre_bl.bin` is loaded but writing it is commented out.
- extract's pre-BL dump on 1838 images captures the SMC + KV region.

Only document what is verified from code or dumps; don't generalize
about other Xbox 360 hardware or builds.

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
