"""
Xbox 360 DD1 XeDK -- low-level NAND I/O.

Logical block interface over raw NAND images with spare bytes, ECC,
and bad block reallocation. All block reads/writes go through the
logical layer which transparently handles remapping.

Raw NAND pages are 0x210 bytes: 0x200 data + 0x10 spare.
Blocks are 32 pages = 0x4000 data bytes.
The last 128 blocks (0xF80-0xFFF) are the reserve area for bad block
reallocation.
"""

# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

PAGE_SIZE = 0x200
SPARE_SIZE = 0x10
PAGE_WITH_SPARE = PAGE_SIZE + SPARE_SIZE  # 0x210
PAGES_PER_BLOCK = 32
BLOCK_SIZE = PAGES_PER_BLOCK * PAGE_SIZE  # 0x4000
TOTAL_BLOCKS = 0x1000  # 4096
RAW_BLOCK_SIZE = PAGES_PER_BLOCK * PAGE_WITH_SPARE  # 0x4200

# Bad block reallocation area -- last 128 blocks
RESERVE_START = 0xF80


# ---------------------------------------------------------------------------
# Spare field access
# ---------------------------------------------------------------------------


class Spare:
    """Parsed spare metadata for one page."""

    __slots__ = (
        "block_id",
        "fs_sequence",
        "bad_block",
        "fs_size",
        "fs_page_count",
        "fs_block_type",
        "ecc",
    )

    def __init__(self, raw):
        self.block_id = (raw[1] & 0xF) << 8 | raw[0]
        self.fs_sequence = raw[6] << 24 | raw[4] << 16 | raw[3] << 8 | raw[2]
        self.bad_block = raw[5]
        self.fs_size = raw[8] << 8 | raw[7]
        self.fs_page_count = raw[9]
        ecc_type = raw[12] | raw[13] << 8 | raw[14] << 16 | raw[15] << 24
        self.fs_block_type = ecc_type & 0x3F
        self.ecc = ecc_type >> 6

    @property
    def is_bad(self):
        return self.bad_block != 0xFF

    def __repr__(self):
        return (
            f"Spare(id={self.block_id:#x}, seq={self.fs_sequence:#x}, "
            f"type={self.fs_block_type:#x}, bad={self.bad_block:#x})"
        )

    @staticmethod
    def encode(
        block_id,
        fs_block_type=0,
        fs_sequence=0,
        fs_size=0,
        fs_page_count=0,
        bad_block=0xFF,
    ):
        """Build a 16-byte spare (ECC bits in byte 12 left clear)."""
        s = bytearray(SPARE_SIZE)
        s[0] = block_id & 0xFF
        s[1] = (block_id >> 8) & 0x0F
        s[2] = fs_sequence & 0xFF
        s[3] = (fs_sequence >> 8) & 0xFF
        s[4] = (fs_sequence >> 16) & 0xFF
        s[5] = bad_block
        s[6] = (fs_sequence >> 24) & 0xFF
        s[7] = fs_size & 0xFF
        s[8] = (fs_size >> 8) & 0xFF
        s[9] = fs_page_count & 0xFF
        s[12] = fs_block_type & 0x3F
        return s


# ---------------------------------------------------------------------------
# ECC
# ---------------------------------------------------------------------------

_POLY = 0x6954559


def _build_ecc_table():
    table = []
    for byte in range(256):
        ecc = 0
        for bit in range(8):
            if ((byte >> bit) & 1) == 0:
                ecc ^= 1
            if ecc & 1:
                ecc ^= _POLY
            ecc >>= 1
        table.append(ecc)
    return table


_ECC_TABLE = _build_ecc_table()


def calc_ecc(page_with_spare):
    """Calculate ECC over a 0x210-byte page (data + spare)."""
    total_bits = PAGE_WITH_SPARE * 8 - 26
    full_bytes, rem_bits = divmod(total_bits, 8)
    ecc = 0
    for i in range(full_bytes):
        ecc = _ECC_TABLE[(ecc ^ page_with_spare[i]) & 0xFF] ^ (ecc >> 8)
    if rem_bits:
        byte = page_with_spare[full_bytes]
        for bit in range(rem_bits):
            if (byte & (1 << bit)) == 0:
                ecc ^= 1
            if ecc & 1:
                ecc ^= _POLY
            ecc >>= 1
    return ecc ^ 0xFFFFFFFF


def _apply_ecc(page_and_spare):
    """Compute and write ECC into the spare bytes of a 0x210 page."""
    ecc = calc_ecc(page_and_spare) << 6
    page_and_spare[PAGE_SIZE + 12] = (ecc & 0xC0) | (
        page_and_spare[PAGE_SIZE + 12] & 0x3F
    )
    page_and_spare[PAGE_SIZE + 13] = (ecc >> 8) & 0xFF
    page_and_spare[PAGE_SIZE + 14] = (ecc >> 16) & 0xFF
    page_and_spare[PAGE_SIZE + 15] = (ecc >> 24) & 0xFF


# ---------------------------------------------------------------------------
# Nand
# ---------------------------------------------------------------------------


class Nand:
    """
    Logical block device over a raw NAND image.

    On load, scans for bad blocks and builds a remap table from the
    reserve area. All read/write operations use logical block IDs and
    transparently follow remaps.
    """

    def __init__(self, raw):
        """
        Create from raw NAND bytes (with spare).
        Use Nand.from_file() or Nand.empty() instead of calling directly.
        """
        self._raw = bytearray(raw)
        self._remap = {}  # logical block -> physical block
        self._bad = []  # (physical_block, marker)
        self._scan_bad_blocks()

    @classmethod
    def from_file(cls, path):
        with open(path, "rb") as f:
            return cls(f.read())

    @classmethod
    def empty(cls, bad_blocks=None):
        """Create a blank NAND image, optionally with bad block remaps."""
        nand = cls(b"\xff" * (TOTAL_BLOCKS * RAW_BLOCK_SIZE))
        if bad_blocks:
            reserve_next = TOTAL_BLOCKS - 1
            for block in bad_blocks:
                if block >= RESERVE_START:
                    continue
                while reserve_next >= RESERVE_START and reserve_next in bad_blocks:
                    reserve_next -= 1
                nand.mark_bad(block, reserve_next)
                reserve_next -= 1
        return nand

    def save(self, path):
        with open(path, "wb") as f:
            f.write(self._raw)

    # -- Bad block management -----------------------------------------------

    def _scan_bad_blocks(self):
        """Scan spare metadata and build the remap table."""
        self._bad = []
        self._remap = {}

        bad_set = set()
        for b in range(TOTAL_BLOCKS):
            spare = self._read_page_spare(b, 0)
            if spare.is_bad:
                bad_set.add(b)
                self._bad.append((b, spare.bad_block))

        if not bad_set:
            return

        # Scan reserve area for replacement blocks
        for b in range(RESERVE_START, TOTAL_BLOCKS):
            spare = self._read_page_spare(b, 0)
            if spare.is_bad:
                continue
            if spare.block_id in bad_set:
                self._remap[spare.block_id] = b

    def mark_bad(self, block, replacement):
        """
        Mark a block as bad and remap it to a replacement in the reserve area.
        The bad block gets BadBlock=0x00 in its spare; subsequent reads/writes
        of the logical block go to the replacement.
        """
        for p in range(PAGES_PER_BLOCK):
            spare = Spare.encode(block_id=block, bad_block=0x00)
            self._write_raw_page(block, p, b"\xff" * PAGE_SIZE, spare)
        self._remap[block] = replacement
        self._bad.append((block, 0x00))

    @property
    def bad_blocks(self):
        """List of (physical_block, bad_marker) for all bad blocks."""
        return list(self._bad)

    @property
    def remap_table(self):
        """Dict of {logical_block: physical_replacement_block}."""
        return dict(self._remap)

    # -- Physical page access (no remapping) --------------------------------

    def _phys_page_offset(self, block, page):
        return (block * PAGES_PER_BLOCK + page) * PAGE_WITH_SPARE

    def _read_raw_page(self, block, page):
        """Read raw 0x210 bytes (data + spare) for a physical page."""
        off = self._phys_page_offset(block, page)
        return self._raw[off : off + PAGE_WITH_SPARE]

    def _read_page_spare(self, block, page):
        """Parse the spare of a physical page."""
        off = self._phys_page_offset(block, page) + PAGE_SIZE
        return Spare(self._raw[off : off + SPARE_SIZE])

    def _write_raw_page(self, block, page, data, spare):
        """Write data + spare to a physical page, computing ECC."""
        full = bytearray(data[:PAGE_SIZE].ljust(PAGE_SIZE, b"\xff"))
        full += bytearray(spare[:SPARE_SIZE].ljust(SPARE_SIZE, b"\x00"))
        _apply_ecc(full)
        off = self._phys_page_offset(block, page)
        self._raw[off : off + PAGE_WITH_SPARE] = full

    # -- Logical block access (with remapping) ------------------------------

    def _resolve(self, block):
        """Resolve a logical block ID to its physical block."""
        return self._remap.get(block, block)

    def read_block(self, block):
        """Read 0x4000 bytes of logical block data."""
        phys = self._resolve(block)
        data = bytearray()
        for p in range(PAGES_PER_BLOCK):
            off = self._phys_page_offset(phys, p)
            data += self._raw[off : off + PAGE_SIZE]
        return data

    def write_block(self, block, data, spare_fn=None):
        """
        Write data to a logical block, only touching pages that hold data.

        spare_fn(page_in_block) -> 16-byte spare template, or None for
        a default spare with just the block ID and BadBlock=0xFF.
        """
        phys = self._resolve(block)
        num_pages = (len(data) + PAGE_SIZE - 1) // PAGE_SIZE
        data = data.ljust(num_pages * PAGE_SIZE, b"\x00")
        for p in range(num_pages):
            page_data = data[p * PAGE_SIZE : (p + 1) * PAGE_SIZE]
            if spare_fn:
                spare = spare_fn(p)
            else:
                spare = Spare.encode(block_id=block)
            self._write_raw_page(phys, p, page_data, spare)

    def read_page_spare(self, block, page):
        """Read parsed spare for a logical block's page."""
        return self._read_page_spare(self._resolve(block), page)

    # -- Flat data access (convenience) -------------------------------------

    def read_flat(self, offset, size):
        """Read from the logical flat address space (block-remapped)."""
        result = bytearray()
        while size > 0:
            block = offset // BLOCK_SIZE
            block_off = offset % BLOCK_SIZE
            chunk = min(size, BLOCK_SIZE - block_off)
            bdata = self.read_block(block)
            result += bdata[block_off : block_off + chunk]
            offset += chunk
            size -= chunk
        return result

    def write_flat(self, offset, data):
        """
        Write to the logical flat address space.

        Data must be block-aligned at the start. Only writes pages
        that contain data; trailing pages in the last block stay erased.
        """
        assert offset % BLOCK_SIZE == 0, "write_flat offset must be block-aligned"
        i = 0
        while i < len(data):
            block = (offset + i) // BLOCK_SIZE
            chunk = min(len(data) - i, BLOCK_SIZE)
            self.write_block(block, data[i : i + chunk])
            i += chunk

    # -- Raw access ---------------------------------------------------------

    @property
    def raw(self):
        """The underlying raw NAND data with spare bytes."""
        return bytes(self._raw)

    def __len__(self):
        return TOTAL_BLOCKS
