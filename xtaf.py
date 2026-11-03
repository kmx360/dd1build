"""
Xbox 360 DD1 XeDK -- XTAF filesystem.

Early builds (e.g. 1640) use an XTAF partition for storing flash
filesystem files instead of the later SFCX layout.

XTAF partition layout (at a fixed block offset in NAND):
  Block 0 (header block):
    0x0000  "XTAF" magic + volume ID + sectors_per_cluster + root_dir_cluster
    0x0010  Padding (0xFF)
    0x1000  FAT (4096 x 16-bit big-endian entries)
    0x3000  Root directory (64-byte entries)
  Block 1+:
    Data clusters. Cluster N maps to flat offset:
      partition_base + N * cluster_size - 0x1000
"""

import struct

from nandio import BLOCK_SIZE, PAGE_SIZE, PAGES_PER_BLOCK

XTAF_MAGIC = b"XTAF"
CLUSTER_SIZE = BLOCK_SIZE  # 32 sectors of 0x200
FAT_OFFSET = 0x1000
DIR_OFFSET = 24 * PAGE_SIZE  # 0x3000
DIR_ENTRY_SIZE = 64
FAT_ENTRIES = 4096
FAT_END = 0xFFFF
FAT_RESERVED = 0xFFF8


class XtafDirEnt:
    __slots__ = ("name", "first_cluster", "size", "attrs",
                 "create_ts", "modify_ts")

    def __init__(self, raw=None):
        if raw is None or raw[0] == 0xFF or raw[0] == 0x00:
            self.name = ""
            self.first_cluster = 0
            self.size = 0
            self.attrs = 0
            self.create_ts = b"\x00" * 6
            self.modify_ts = b"\x00" * 6
            return
        name_len = raw[0]
        self.attrs = raw[1]
        self.name = raw[2:2 + name_len].decode("ascii", errors="replace")
        self.first_cluster = struct.unpack(">I", raw[44:48])[0]
        self.size = struct.unpack(">I", raw[48:52])[0]
        self.create_ts = bytes(raw[52:58])
        self.modify_ts = bytes(raw[58:64])

    def encode(self):
        buf = bytearray(DIR_ENTRY_SIZE)
        name_bytes = self.name.encode("ascii")[:42]
        buf[0] = len(name_bytes)
        buf[1] = self.attrs
        buf[2:2 + len(name_bytes)] = name_bytes
        struct.pack_into(">I", buf, 44, self.first_cluster)
        struct.pack_into(">I", buf, 48, self.size)
        buf[52:58] = self.create_ts[:6]
        buf[58:64] = self.modify_ts[:6]
        return buf


def has_xtaf(nand, partition_block):
    """Check if the given block starts with XTAF magic."""
    data = nand.read_block(partition_block)
    return data[:4] == XTAF_MAGIC


def read_xtaf(nand, partition_block):
    """
    Read an XTAF filesystem partition.

    Returns a list of (filename, file_bytes) tuples.
    """
    header_block = nand.read_block(partition_block)
    if header_block[:4] != XTAF_MAGIC:
        return []

    partition_flat = partition_block * BLOCK_SIZE

    fat_data = header_block[FAT_OFFSET:FAT_OFFSET + FAT_ENTRIES * 2]
    fat = [struct.unpack(">H", fat_data[i:i+2])[0]
           for i in range(0, len(fat_data), 2)]

    dir_data = header_block[DIR_OFFSET:]
    entries = []
    for i in range(0, len(dir_data), DIR_ENTRY_SIZE):
        raw = dir_data[i:i + DIR_ENTRY_SIZE]
        if raw[0] == 0xFF or raw[0] == 0x00:
            break
        entries.append(XtafDirEnt(raw))

    def cluster_to_flat(cluster):
        return partition_flat + cluster * CLUSTER_SIZE - FAT_OFFSET

    files = []
    for ent in entries:
        data = bytearray()
        cluster = ent.first_cluster
        remaining = ent.size
        while remaining > 0 and cluster < 0xFFF0:
            offset = cluster_to_flat(cluster)
            chunk = nand.read_flat(offset, min(CLUSTER_SIZE, remaining))
            data += chunk
            remaining -= len(chunk)
            cluster = fat[cluster]
        files.append((ent.name, bytes(data[:ent.size])))

    return files


def write_xtaf(nand, entries, partition_block):
    """
    Build an XTAF filesystem and write it into the NAND.

    entries is a list of (name, data_bytes) tuples.

    XTAF data is not block-aligned (clusters offset by -0x1000 from
    block boundaries), so we build the entire partition as a contiguous
    buffer and write it at once.
    """
    fat = [0] * FAT_ENTRIES
    fat[0] = FAT_RESERVED
    fat[1] = FAT_END

    next_cluster = 2
    dir_entries = []

    for name, data in entries:
        n_clusters = max(1, (len(data) + CLUSTER_SIZE - 1) // CLUSTER_SIZE)
        first_cluster = next_cluster

        for i in range(n_clusters):
            c = next_cluster
            next_cluster += 1
            fat[c] = next_cluster if i + 1 < n_clusters else FAT_END

        ent = XtafDirEnt()
        ent.name = name
        ent.first_cluster = first_cluster
        ent.size = len(data)
        ent.attrs = 0
        ent.create_ts = b"\x00" * 6
        ent.modify_ts = b"\x00" * 6
        dir_entries.append(ent)

    # Total partition size: from header through last used cluster
    partition_size = next_cluster * CLUSTER_SIZE - FAT_OFFSET
    partition = bytearray(b"\xff" * partition_size)

    # Header at offset 0
    partition[:4] = XTAF_MAGIC
    struct.pack_into(">I", partition, 4, 0x299ED947)
    struct.pack_into(">I", partition, 8, PAGES_PER_BLOCK)
    struct.pack_into(">I", partition, 12, 1)

    # FAT
    for i, val in enumerate(fat):
        struct.pack_into(">H", partition, FAT_OFFSET + i * 2, val)

    # Directory
    for i, ent in enumerate(dir_entries):
        off = DIR_OFFSET + i * DIR_ENTRY_SIZE
        partition[off:off + DIR_ENTRY_SIZE] = ent.encode()

    # File data
    for ent, (name, data) in zip(dir_entries, entries):
        cluster = ent.first_cluster
        remaining = len(data)
        src_off = 0
        while remaining > 0 and cluster < 0xFFF0:
            dst_off = cluster * CLUSTER_SIZE - FAT_OFFSET
            chunk = min(CLUSTER_SIZE, remaining)
            partition[dst_off:dst_off + chunk] = data[src_off:src_off + chunk]
            src_off += chunk
            remaining -= chunk
            cluster = fat[cluster]

    nand.write_flat(partition_block * BLOCK_SIZE, partition)
