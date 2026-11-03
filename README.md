# dd1build

Extract and rebuild NAND images for Xbox 360 DD1 XeDK development kits.

DD1 is a prototype XeDK that uses its own bootloaders. Its 1BL key and
CPU key are all zeros, so every encrypted part of the flash (SMC,
keyvault, bootloader chain) can be decrypted and re-encrypted. That makes
it possible to pull a dump apart, swap components (a different SMC,
bootloaders, a modified flash filesystem), and write a new image.

## Requirements

- Python 3
- [`cryptography`](https://pypi.org/project/cryptography/)

```
pip install cryptography
```

Input and output images are raw NAND dumps **with spare bytes**
(0x210 bytes per page, 69,206,016 bytes total for a 64 MB NAND).

## Extracting a dump

```
./extract.py dump.bin -o dump/
```

This writes:

| File | Contents |
|---|---|
| `smc.bin` | Decrypted SMC firmware |
| `kv.bin` | Decrypted keyvault, if present |
| `smc_config.bin` | SMC config blocks |
| `s2.bin` … `s5.bin` | Decrypted bootloader stages |
| `pre_bl.bin` | Data between the flash header and the bootloaders, if any |
| `*.dat` | Mobile blocks, if present |
| `fs/` | Files from the flash filesystem |

## Building an image

From an extracted directory:

```
./build.py -i dump/ -o new.bin
```

From a `xboxrom_update.bin` plus a keyvault:

```
./build.py --update xboxrom_update.bin --keyvault kv.bin -o new.bin
```

Any single component can be replaced on top of either source:

```
./build.py -i dump/ --smc my_smc.bin --s2 other_s2.bin --fs my_fs/ -o new.bin
```

Run `./build.py --help` for the full list of overrides, including mobile
blocks and `--bad-blocks` to mark specific blocks bad in the output.

## Supported flash layouts

Builds before 1838 use a different flash layout; 1838 introduced the
final format. Values seen in the dumps this tool has been used on:

| | 1640 | 1746 | 1838 |
|---|---|---|---|
| Header magic | 0xFF3F | 0xFF3F | 0x0F3F |
| Bootloader entry | 0x1040 | 0x14A0 | 0x8000 |
| SMC address | 0x34000 | 0x64000 | 0x1000 |
| Keyvault | none | none | 0x4000 |
| Bootloader stages | S2–S4 | S2–S5 | S2–S5 |
| Flash filesystem | XTAF at block 0x80 | XTAF at block 0x80 | SFCX |

`extract.py` reads all of these. It finds every component through the
flash header and checks block 0x80 for XTAF before falling back to SFCX.

`build.py` has two layouts, `1838` and `pre1838`, chosen from the S2
bootloader's build number (override with `--layout`). For `pre1838` it
works out the offsets from the component sizes instead of using fixed
values:

- `pre_bl.bin` (a bootloader stage of its own) goes right after the
  0x80-byte flash header, and the bootloader chain starts after it.
- The SMC starts at the first block boundary after the bootloader chain.

Rebuilding the 1640 and 1746 dumps this way reproduces their boot regions
byte for byte. Without `pre_bl.bin` (e.g. when building from
`--update`), the bootloader entry falls back to 0x1040.

## Known limitations

- The `pre1838` build layout does not write mobile blocks, although one
  1640 dump contains them.
- `build.py` always writes the same XTAF volume ID (the one from the 1640
  dumps); the 1746 dump has a different one.
- On 1838 images, `pre_bl.bin` contains the SMC and keyvault, because
  both sit between the header and the bootloader entry.

## Verifying a rebuild

There is no automated test suite. A quick sanity check is a round trip:

```
./extract.py dump.bin -o a/
./build.py -i a/ -o rebuilt.bin
./extract.py rebuilt.bin -o b/
diff -r a/ b/
```

## Code layout

- `nandio.py`: raw NAND access, ECC, spare bytes, bad block remapping
- `nand.py`: flash header structures, crypto, SFCX filesystem, mobile blocks
- `xtaf.py`: XTAF flash filesystem used by early builds
- `extract.py`, `build.py`: command-line tools
