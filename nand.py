"""
Xbox 360 DD1 XeDK -- NAND structures, crypto, and SFCX filesystem.

DD1 XeDK machines use all-zero 1BL key and CPU key.

Flash layout (logical, flat addressing via nandio.Nand):
  0x0000  BldrFlash header (contains offsets to everything)
  SMC     at SmcAddr,       size SmcSize       (XeCryptSwizzle encrypted)
  KV      at KeyVaultAddr,  size 0x4000        (KeyVaultSize in header may be 0)
  BLs     at Bldr.Entry,    size Bldr.Size     (RC4 chain: S2, S3, S4, S5)
  FS      managed by SFCX spare metadata       (FAT + directory in root block)
"""

import ctypes
import datetime
import hmac
from hashlib import sha1
from cryptography.hazmat.primitives.ciphers import Cipher
from cryptography.hazmat.decrepit.ciphers.algorithms import ARC4

from nandio import Spare, BLOCK_SIZE, PAGES_PER_BLOCK, PAGE_SIZE, TOTAL_BLOCKS


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

KV_SIZE = 0x4000

FAT_END = 0x1FFF
FAT_RESERVED = 0x1FFB
FAT_FREE = 0x1FFE

# DD1 flash layout defaults
DEFAULT_SMC_ADDR = 0x1000
DEFAULT_KV_ADDR = 0x4000
DEFAULT_BL_ENTRY = 0x8000
DEFAULT_SYSUPDATE_ADDR = 0x74000

# First block available for filesystem allocation
FS_START_BLOCK = 0x50

# SMC config -- 4 blocks before the reserve area
SMC_CONFIG_START = 0xF7C
SMC_CONFIG_BLOCKS = 4  # 0xF7C-0xF7F

# SFCX spare block types
FS_TYPE_ROOT = 0x30

# Mobile block types (0x31-0x39) -- standalone blocks with size in spare
MOBILE_TYPES = {
    0x31: "MobileB",
    0x32: "MobileC",
    0x33: "MobileD",
    0x34: "MobileE",
    0x35: "MobileF",
    0x36: "MobileG",
    0x37: "MobileH",
    0x38: "MobileI",
    0x39: "MobileJ",
}

# DD1 XeDK machines use all-zero 1BL key and CPU key
_1BL_KEY = b"\x00" * 16
_CPU_KEY = b"\x00" * 16


# ---------------------------------------------------------------------------
# Structures
# ---------------------------------------------------------------------------


class Bldr(ctypes.BigEndianStructure):
    _fields_ = (
        ("Magic", ctypes.c_uint16),
        ("Build", ctypes.c_uint16),
        ("Qfe", ctypes.c_uint16),
        ("Flags", ctypes.c_uint16),
        ("Entry", ctypes.c_uint32),
        ("Size", ctypes.c_uint32),
    )

    def __repr__(self):
        return (
            f"Bldr(Magic={self.Magic:#x}, Build={self.Build}, "
            f"Qfe={self.Qfe:#x}, Flags={self.Flags:#x}, "
            f"Entry={self.Entry:#x}, Size={self.Size:#x})"
        )


class BldrFlash(ctypes.BigEndianStructure):
    _fields_ = (
        ("Bldr", Bldr),
        ("Copyright", ctypes.c_uint8 * 64),
        ("Reserved", ctypes.c_uint8 * 16),
        ("KeyVaultSize", ctypes.c_uint32),
        ("SysUpdateAddr", ctypes.c_uint32),
        ("SysUpdateCount", ctypes.c_uint16),
        ("KeyVaultVersion", ctypes.c_uint16),
        ("KeyVaultAddr", ctypes.c_uint32),
        ("PatchSlotSize", ctypes.c_uint32),
        ("SmcConfigAddr", ctypes.c_uint32),
        ("SmcSize", ctypes.c_uint32),
        ("SmcAddr", ctypes.c_uint32),
    )

    def __repr__(self):
        return (
            f"BldrFlash(Bldr={self.Bldr}, "
            f"Copyright={bytes(self.Copyright).rstrip(b'\\0')}, "
            f"KeyVaultSize={self.KeyVaultSize:#x}, "
            f"SysUpdateAddr={self.SysUpdateAddr:#x}, "
            f"SysUpdateCount={self.SysUpdateCount}, "
            f"KeyVaultVersion={self.KeyVaultVersion}, "
            f"KeyVaultAddr={self.KeyVaultAddr:#x}, "
            f"PatchSlotSize={self.PatchSlotSize:#x}, "
            f"SmcConfigAddr={self.SmcConfigAddr:#x}, "
            f"SmcSize={self.SmcSize:#x}, "
            f"SmcAddr={self.SmcAddr:#x})"
        )


class FatPage(ctypes.BigEndianStructure):
    _fields_ = (("Entries", ctypes.c_uint16 * 0x100),)


class DirEnt(ctypes.BigEndianStructure):
    _fields_ = (
        ("Name", ctypes.c_char * 22),
        ("BlockId", ctypes.c_uint16),
        ("Size", ctypes.c_uint32),
        ("Timestamp", ctypes.c_int32),
    )

    def __repr__(self):
        return (
            f"DirEnt(Name={self.Name}, BlockId={self.BlockId:#x}, "
            f"Size={self.Size:#x}, Timestamp={parse_dos_timestamp(self.Timestamp)})"
        )


# ---------------------------------------------------------------------------
# Crypto
# ---------------------------------------------------------------------------


def XeCryptSwizzle(data, decrypt):
    """In-place XOR swizzle used on SMC image. Operates on a bytearray."""
    key = bytearray((0x42, 0x75, 0x4E, 0x79))
    for i in range(len(data)):
        if decrypt:
            mod = data[i] * 0xFB
            data[i] ^= key[i & 3]
        else:
            data[i] ^= key[i & 3]
            mod = data[i] * 0xFB
        key[(i + 1) & 3] = (key[(i + 1) & 3] + mod) & 0xFF
        key[(i + 2) & 3] = (key[(i + 2) & 3] + (mod >> 8)) & 0xFF


def decrypt_keyvault(kv):
    """
    Decrypt a keyvault. The first 16 bytes are the nonce, HMAC'd with
    the CPU key (all zeros on DD1) to derive the RC4 key. Body from
    offset 0x10 onward is RC4-decrypted.
    """
    kv = bytearray(kv)
    rc4_key = hmac.new(_CPU_KEY, kv[0x00:0x10], sha1).digest()[:0x10]
    kv[0x10:] = Cipher(ARC4(rc4_key), mode=None).encryptor().update(kv[0x10:])
    return kv


def encrypt_keyvault(kv):
    """RC4 is symmetric so this is the same operation as decrypt."""
    return decrypt_keyvault(kv)


def decrypt_bl_chain_raw(region):
    """
    Decrypt a DD1 bootloader chain from raw bytes.

    Walks the region parsing Bldr headers until the data runs out.
    The number of stages varies (3 on early builds, 4 on later ones).
    Returns list of (name, decrypted_bytearray) tuples.
    """
    off = 0
    key = _1BL_KEY
    result = []
    stage = 2

    while off + ctypes.sizeof(Bldr) <= len(region):
        bldr = Bldr.from_buffer_copy(region[off:])
        if bldr.Size == 0 or off + bldr.Size > len(region):
            break
        padded_size = (bldr.Size + 15) & ~15
        bl = bytearray(region[off : off + padded_size])
        off += padded_size

        key = hmac.new(key, bl[0x10:0x20], sha1).digest()[:0x10]
        bl[0x20:] = Cipher(ARC4(key), mode=None).encryptor().update(bl[0x20:])

        if stage == 2:
            key = b"\x00" * 16

        result.append((f"s{stage}", bl))
        stage += 1

    return result


def decrypt_bl_chain(nand, header):
    """Decrypt the DD1 bootloader chain from a NAND image."""
    region = nand.read_flat(header.Bldr.Entry, header.Bldr.Size)
    return decrypt_bl_chain_raw(region)


def encrypt_bl_chain(bootloaders):
    """
    Re-encrypt a list of decrypted bootloader bytearrays in order.
    Returns the concatenated encrypted bootloader region.
    """
    key = _1BL_KEY
    result = bytearray()

    for i, bl in enumerate(bootloaders):
        bl = bytearray(bl)
        # Each stage must be 16-byte padded
        padding = (16 - len(bl) % 16) % 16
        bl += b"\x00" * padding

        key = hmac.new(key, bl[0x10:0x20], sha1).digest()[:0x10]
        bl[0x20:] = Cipher(ARC4(key), mode=None).encryptor().update(bl[0x20:])

        if i == 0:
            key = b"\x00" * 16

        result += bl

    return result


# ---------------------------------------------------------------------------
# Update binary
# ---------------------------------------------------------------------------


def parse_update_binary(data):
    """
    Parse an xboxrom_update.bin file.

    Returns (bootloaders, smc_or_none) where bootloaders is a list of
    (name, decrypted_bytearray) tuples and smc is a decrypted bytearray
    or None if not present.
    """
    header = BldrFlash.from_buffer_copy(data[: ctypes.sizeof(BldrFlash)])
    region = data[header.Bldr.Entry :]
    bootloaders = decrypt_bl_chain_raw(region)

    smc = None
    if header.SmcSize > 0 and header.SmcAddr + header.SmcSize <= len(data):
        smc = bytearray(data[header.SmcAddr : header.SmcAddr + header.SmcSize])
        XeCryptSwizzle(smc, decrypt=True)

    return bootloaders, smc


# ---------------------------------------------------------------------------
# DOS timestamps
# ---------------------------------------------------------------------------


def parse_dos_timestamp(dos_32: int) -> datetime.datetime:
    dos_date = (dos_32 >> 16) & 0xFFFF
    dos_time = dos_32 & 0xFFFF
    day = max(1, dos_date & 0x1F)
    month = max(1, (dos_date >> 5) & 0x0F)
    year = max(1980, ((dos_date >> 9) & 0x7F) + 1980)
    second = (dos_time & 0x1F) * 2
    minute = (dos_time >> 5) & 0x3F
    hour = (dos_time >> 11) & 0x1F
    return datetime.datetime(year, month, day, hour, minute, second)


def make_dos_timestamp(dt: datetime.datetime) -> int:
    dos_date = (
        (dt.day & 0x1F) | ((dt.month & 0xF) << 5) | (((dt.year - 1980) & 0x7F) << 9)
    )
    dos_time = (
        ((dt.second // 2) & 0x1F) | ((dt.minute & 0x3F) << 5) | ((dt.hour & 0x1F) << 11)
    )
    return (dos_date << 16) | dos_time


# ---------------------------------------------------------------------------
# SFCX filesystem
# ---------------------------------------------------------------------------


def scan_typed_blocks(nand):
    """
    Scan all spare metadata in one pass. Returns a dict of
    {block_type: (block_id, sequence, size)} keeping only the highest
    sequence per type. Type 0 (untyped data blocks) is excluded.
    """
    best = {}
    for b in range(TOTAL_BLOCKS):
        spare = nand.read_page_spare(b, 0)
        t = spare.fs_block_type
        if t != 0 and (t not in best or spare.fs_sequence > best[t][1]):
            best[t] = (spare.block_id, spare.fs_sequence, spare.fs_size)
    return best


def read_mobiles(nand, typed_blocks):
    """
    Read mobile blocks from scan results. Returns list of
    (filename, data_bytes) tuples.

    The console appends new versions of a mobile to the next free pages
    within the same block. The current version is found by locating the
    last written page and using fs_size to determine how many pages back
    to read.
    """
    result = []
    for t in sorted(MOBILE_TYPES):
        if t not in typed_blocks:
            continue
        block_id = typed_blocks[t][0]
        # Find last written page
        last_page = PAGES_PER_BLOCK - 1
        while last_page >= 0 and nand.read_page_spare(block_id, last_page).fs_block_type == 0x3F:
            last_page -= 1
        if last_page < 0:
            continue
        spare = nand.read_page_spare(block_id, last_page)
        pages_used = (spare.fs_size + PAGE_SIZE - 1) // PAGE_SIZE
        first_page = last_page - pages_used + 1
        block_data = nand.read_block(block_id)
        data = block_data[first_page * PAGE_SIZE : (last_page + 1) * PAGE_SIZE]
        result.append((f"{MOBILE_TYPES[t]}.dat", data[:spare.fs_size]))
    return result


def write_mobiles(nand, mobiles, fs_start_block=FS_START_BLOCK, fs_sequence=1):
    """
    Write mobile blocks into the NAND starting at fs_start_block.

    mobiles is a list of (block_type, data_bytes) tuples.
    Returns the next free block after the last mobile written.
    """
    next_block = fs_start_block
    for block_type, data in mobiles:
        bid = next_block
        next_block += 1
        pages_used = (len(data) + PAGE_SIZE - 1) // PAGE_SIZE
        free_pages = PAGES_PER_BLOCK - pages_used
        nand.write_block(
            bid,
            data,
            spare_fn=lambda p, _bid=bid, _type=block_type, _size=len(
                data
            ), _free=free_pages: Spare.encode(
                block_id=_bid,
                fs_block_type=_type,
                fs_sequence=fs_sequence,
                fs_size=_size,
                fs_page_count=_free,
            ),
        )
    return next_block


def write_fs(nand, entries, fs_start_block=FS_START_BLOCK):
    """
    Build SFCX filesystem from file entries and write into the NAND.

    entries is a list of (name, data_bytes, dos_timestamp) tuples.
    Returns the root block ID.
    """
    next_block = fs_start_block
    fat = [FAT_FREE] * TOTAL_BLOCKS
    dir_entries = []
    fs_sequence = 1

    for i in range(fs_start_block):
        fat[i] = FAT_RESERVED
    for i in range(SMC_CONFIG_START, TOTAL_BLOCKS):
        fat[i] = FAT_RESERVED

    for name, data, timestamp in entries:
        n_blocks = max(1, (len(data) + BLOCK_SIZE - 1) // BLOCK_SIZE)

        if next_block >= SMC_CONFIG_START:
            raise ValueError("NAND full: not enough blocks for filesystem")

        first_block = next_block
        file_blocks = []
        for _ in range(n_blocks):
            if next_block >= SMC_CONFIG_START:
                raise ValueError("NAND full: not enough blocks for filesystem")
            file_blocks.append(next_block)
            next_block += 1

        for i, bid in enumerate(file_blocks):
            fat[bid] = file_blocks[i + 1] if i + 1 < len(file_blocks) else (FAT_END | 0x8000)

        for i, bid in enumerate(file_blocks):
            chunk = data[i * BLOCK_SIZE : (i + 1) * BLOCK_SIZE]
            pages_used = (len(chunk) + PAGE_SIZE - 1) // PAGE_SIZE
            free_pages = PAGES_PER_BLOCK - pages_used
            nand.write_block(
                bid,
                chunk,
                spare_fn=lambda p, _bid=bid, _free=free_pages: Spare.encode(
                    block_id=_bid,
                    fs_block_type=0x00,
                    fs_sequence=fs_sequence,
                    fs_page_count=_free,
                ),
            )

        dir_ent = DirEnt()
        dir_ent.Name = name.encode()[:22]
        dir_ent.BlockId = first_block
        dir_ent.Size = len(data)
        dir_ent.Timestamp = timestamp
        dir_entries.append(dir_ent)

    root_block_id = next_block
    fat[root_block_id] = 0x1FFD

    root_data = bytearray(BLOCK_SIZE)
    dir_idx = 0
    for page in range(PAGES_PER_BLOCK):
        page_off = page * PAGE_SIZE
        if page % 2 == 0:
            fat_page_idx = page // 2
            fat_start = fat_page_idx * 256
            fat_page = FatPage()
            for i in range(256):
                fat_page.Entries[i] = fat[fat_start + i] & 0xFFFF
            root_data[page_off : page_off + PAGE_SIZE] = bytes(fat_page)
        else:
            for i in range(16):
                ent_off = page_off + i * ctypes.sizeof(DirEnt)
                if dir_idx < len(dir_entries):
                    root_data[ent_off : ent_off + ctypes.sizeof(DirEnt)] = bytes(
                        dir_entries[dir_idx]
                    )
                    dir_idx += 1

    nand.write_block(
        root_block_id,
        root_data,
        spare_fn=lambda p: Spare.encode(
            block_id=root_block_id,
            fs_block_type=FS_TYPE_ROOT,
            fs_sequence=fs_sequence + 1,
        ),
    )
    return root_block_id


def read_fs(nand, typed_blocks):
    """
    Read the SFCX filesystem from the NAND.

    The root block contains interleaved FAT and directory pages:
      even pages -> FAT (256 x uint16 entries each)
      odd pages  -> directory (16 x DirEnt each)

    Returns (root_block_id, list of (filename, file_bytes) tuples),
    or (None, []) if no root block found.
    """
    if FS_TYPE_ROOT not in typed_blocks:
        return None, []
    fs_root_id = typed_blocks[FS_TYPE_ROOT][0]
    root_data = nand.read_block(fs_root_id)
    fat = []
    directory = []
    for p in range(PAGES_PER_BLOCK):
        page = root_data[p * PAGE_SIZE : (p + 1) * PAGE_SIZE]
        if p % 2 == 0:
            fat += FatPage.from_buffer_copy(page).Entries
        else:
            directory += (DirEnt * 16).from_buffer_copy(page)

    files = []
    for ent in directory:
        if len(ent.Name) == 0:
            break
        if ent.Name[0] == 5:  # deleted
            continue

        file_data = bytearray()
        block_id = ent.BlockId
        while block_id != FAT_END:
            file_data += nand.read_block(block_id)
            block_id = fat[block_id] & 0x7FFF
        file_data = file_data[: ent.Size]
        files.append((ent.Name.decode(), file_data))

    return fs_root_id, files
