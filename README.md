# dd1build

Extract and rebuild NAND images for Xbox 360 DD1 XeDK development kits.

DD1 kits use an all-zero 1BL key and CPU key, so every encrypted part of
the flash (SMC, keyvault, bootloader chain) can be decrypted and
re-encrypted without any per-console secrets. That makes it possible to
pull a dump apart, swap components (a different SMC, bootloaders from
another build, a modified flash filesystem), and write a working image
back.

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
| `kv.bin` | Decrypted keyvault (later layout only) |
| `smc_config.bin` | SMC config blocks |
| `s2.bin` … `s5.bin` | Decrypted bootloader stages |
| `pre_bl.bin` | Data between the flash header and the bootloaders, if any |
| `*.dat` | Mobile data blocks (later layout only) |
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

DD1 kits shipped with two different flash layouts depending on the
system build:

| | Later builds (e.g. 1838) | Early builds (e.g. 1640) |
|---|---|---|
| Keyvault | yes | no |
| Flash filesystem | SFCX, plus mobile blocks | XTAF partition at block 0x80 |

`extract.py` detects the layout automatically. `build.py` picks it from
the S2 bootloader's build number; override with `--layout 1640` or
`--layout 1838`.

## Known limitations

- `pre_bl.bin` is extracted from early-layout images but not yet written
  back by `build.py`.
- Only DD1 XeDK hardware is supported. Retail and later dev kits use
  per-console keys and a different flash format.

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
