"""Payload builders and decoders: everything the tool writes, computed from geometry,
the controller datums (f700), the shipped data and the live reads of a run.

Layouts and status of each structure: docs/llf.md (format payloads),
docs/structures.md (record page, config block), docs/nand.md (on-NAND records).
"""
import os
import secrets

from . import asm51, f700, ftlcrypt
from .nand import ecc_param_record, llf_pattern, module_feature, module_name
from .protocol import CAPTURE_SUB, LoaderError


def be16(v):
    return (v & 0xFFFF).to_bytes(2, "big")


def tile(rec, length):
    return (rec * (length // len(rec) + 1))[:length]


# == PARAM BLOCK (FA 0B 01) ==
def put(b, table):
    """Write a byte table into `b`: {offset: "hex" or int}."""
    for off, v in table.items():
        v = bytes.fromhex(v.replace(" ", "")) if isinstance(v, str) else bytes((v,))
        b[off:off + len(v)] = v


def ftl_map(b, n, slc=False):
    """The param's FTL page map: identity of n entries (f700, PARAM BLOCK)."""
    b[0x41:0x43] = be16(n)
    if slc:
        b[0x13:0x15] = be16(n)
    for i in range(n):
        b[0x1A0 + i] = i & 0xFF
        b[0x5A0 + i // 4] |= (i >> 8) << (i % 4 * 2)


def build_param(geo, tables=(), units=None, ecc_t=f700.ECC_DEFAULT, map=None, slack=False, slot_cols=None):
    """The 2048-B param block, built in the order f700 (PARAM BLOCK) lists: geometry, base,
    page map (None | "slc" | "nomap"), `tables` (the stage's bytes, then the call's tokens),
    then the slack (`slack`, or `slot_cols` bad columns of a boundary slot)."""
    b = bytearray(2048)
    ppb, nsect, p = geo.pages_per_block, geo.sectors_per_page, geo.pages_per_block
    b[0x00] = geo.ce_count
    b[0x01:0x03] = be16(geo.block_span)
    b[0x03:0x05] = b[0x13:0x15] = b[0x25:0x27] = be16(ppb)
    b[0x05] = b[0x15] = nsect
    b[0x06] = geo.planes_static
    b[0x20] = (ppb - 1).bit_length()
    b[0x21], b[0x30], b[0x32] = geo.raw[22], geo.raw[8], geo.raw[24]
    b[0x2E:0x30] = be16(f700.ECC_MODES[ecc_t]["row"])
    b[0x31], b[0x4C] = ecc_t, f700.ECC_MODES[ecc_t]["p4c"]
    for i, v in enumerate((0, p // 4 - 1, p // 4, p // 2 - 1, p // 2, 3 * p // 4 - 1, 3 * p // 4, p - 1)):
        b[0x80 + 2 * i:0x82 + 2 * i] = be16(v)
    b[0xD0:0xD0 + nsect] = bytes(range(nsect))
    put(b, f700.PARAM_BASE)
    if geo.ecc_code in (0x16, 0x17):
        put(b, f700.PARAM_NAND_ATTR)
    if units is not None:
        b[0x10] = b[0x22] = units
    ftl_map(b, {None: ppb, "slc": ppb // 2, "nomap": 0}[map], slc=map == "slc")
    for t in tables:
        put(b, t)
    spare = geo.raw_page_span % int.from_bytes(b[0x2E:0x30], "big")
    if slot_cols is not None and slot_cols > spare:
        raise ColumnError(f"{slot_cols} bad columns exceed the {spare}-B frame slack")
    if slack or slot_cols is not None:
        b[0x17:0x19] = be16(spare - (slot_cols or 0))
    if slot_cols is not None:
        b[0x66:0x68] = be16(spare)
    return bytes(b)


# == LOADER PATCH (FA 0A, 0x2000) ==
def build_loader_patch(geo, units=None, ecc_t=f700.ECC_DEFAULT, edits=()):
    """The 818-B loader patch: geometry fields, constants, the ECC mode and its BCH table.
    units: None at MP enable (0x06 = CE count), the post-enable unit count after."""
    p = bytearray(f700.LOADER_PATCH_LEN)
    p[0x04], p[0x08] = geo.planes, geo.sectors_per_page
    p[0x06] = geo.ce_count if units is None else units
    p[0x0A], p[0x0C] = geo.lanes, geo.interleave_class
    put(p, f700.LOADER_PATCH_CONST)
    p[0x195] = ecc_t - 2
    p[0x1B0] = f700.ECC_MODES[ecc_t]["row"] & 0xFF
    p[0x200:] = ecc_param_record(ecc_t)[:f700.ECC_TABLE_LEN]
    for e in edits:
        put(p, f700.LOADER_EDITS[e])
    return bytes(p)


LOADER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "loaders")


def own_loader_image(role, xdata=None):
    """One of the tool's own RAM loaders (f700.OWN_LOADERS) as uploaded: its code assembled
    at LOADER_BASE, then the patch window: the constants, then the stick's own NAND set-up
    when `xdata` ({address: byte}, f700.PATCH_FROM_STICK) is given, else OWN_PATCH."""
    src = open(os.path.join(LOADER_DIR, f700.OWN_LOADERS[role])).read()
    code, _ = asm51.assemble(src, org=f700.LOADER_BASE)
    if len(code) > f700.LOADER_PATCH_AT:
        raise ValueError(f"{role} loader: {len(code)} B does not fit below the patch window")
    return (code.ljust(f700.LOADER_PATCH_AT, b"\xff") + loader_patch(xdata)).ljust(f700.LOADER_UPLOAD, b"\0")


def loader_patch(xdata=None):
    """An own loader's patch window: the constants, then the stick's NAND set-up from
    `xdata` ({address: byte}, f700.PATCH_FROM_STICK), else OWN_PATCH."""
    patch = bytearray(f700.LOADER_PATCH_LEN)
    put(patch, f700.LOADER_PATCH_CONST)
    if xdata is None:
        put(patch, f700.OWN_PATCH)
    else:
        for off, addr in f700.PATCH_FROM_STICK.items():
            patch[off] = xdata[addr]
    return bytes(patch)


def page0_loader_image(code, xdata=None):
    """The boot-firmware capture loader (f700, BOOT FIRMWARE CAPTURE) from a 64-KB CODE dump of
    the running firmware: its page0 and resident overlay, the ROM-facing slots stubbed, the
    capture stub over the first slot handler that the overlay-load path neither runs nor
    jumps into. LoaderError if page0 is not recognised."""
    base, size = f700.LOADER_BASE, f700.PAGE_SIZE_FW
    win = bytearray(code[base:base + f700.LOADER_PATCH_AT])
    if win[size - 4:size - 2] != f700.PAGE0_TAG or win[3 * f700.FW_BLOCK_SLOT] != 0x02:
        raise LoaderError("no boot firmware page0 in the CODE window")
    at = win.find(f700.OVERLAY_SELECT, 0, size)
    if at < 0:
        raise LoaderError("page0 has no overlay select routine")
    src = open(os.path.join(LOADER_DIR, f700.CAPTURE_LOADER)).read()
    symbols = {"SELECT": base + at, "SUB": CAPTURE_SUB}
    stub_len = len(asm51.assemble(src, base, {**symbols, "STUB": base})[0])
    path = asm51.reached(code, [base + at])
    jumps = asm51.targets(bytes(win), base)
    slot = lambda n: int.from_bytes(win[3 * n + 1:3 * n + 3], "big")
    free = [t for t in map(slot, range(f700.LOADER_SLOTS))
            if base + 3 * f700.LOADER_SLOTS <= t <= base + size - stub_len
            and not any(a in path or (a in jumps and a != t) for a in range(t, t + stub_len))]
    if not free:
        raise LoaderError("page0 has no slot handler free for the capture stub")
    stub, sym = asm51.assemble(src, free[0], {**symbols, "STUB": free[0]})
    win[free[0] - base:free[0] - base + len(stub)] = stub
    for n in range(f700.LOADER_SLOTS):
        if n != f700.FW_BLOCK_SLOT:
            v = f700.CONST_SLOTS.get(n)
            t = sym["capture" if n == 4 else "done" if v is None else "result" if v == 0 else f"ret_{v:02x}"]
            win[3 * n:3 * n + 3] = bytes((0x02, t >> 8, t & 0xFF))
    return (bytes(win) + loader_patch(xdata)).ljust(f700.LOADER_UPLOAD, b"\0")


# == ONFI ==
def onfi_crc(data):
    crc = 0x4F4E                                   # ONFI parameter page CRC-16, poly 0x8005
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x8005 if crc & 0x8000 else crc << 1) & 0xFFFF
    return crc


def onfi_geometry(page):
    """The NAND's own description from its ONFI parameter page (the first copy whose CRC
    checks), or None. Offsets: ONFI 4.x parameter page."""
    for off in range(0, len(page) - 255, 256):
        p = page[off:off + 256]
        if p[:4] == b"ONFI" and onfi_crc(p[:254]) == int.from_bytes(p[254:256], "little"):
            le = lambda a, n: int.from_bytes(p[a:a + n], "little")
            return {"page_bytes": le(80, 4), "spare_bytes": le(84, 2), "pages_per_block": le(92, 4),
                    "blocks_per_lun": le(96, 4), "luns": p[100], "row_cycles": p[101] & 0x0F,
                    "col_cycles": p[101] >> 4, "manufacturer": p[32:44].decode("latin1").strip(),
                    "model": p[44:64].decode("latin1").strip(), "jedec_id": p[64],
                    "onfi_versions": [v for v, bit in ((1.0, 1), (2.0, 2), (2.1, 3), (2.2, 4), (2.3, 5),
                                                        (3.0, 6), (3.1, 7), (3.2, 8), (4.0, 9)) if le(4, 2) >> bit & 1],
                    "parameter_pages": p[14], "bits_per_cell": p[102], "max_bad_blocks": le(103, 2),
                    "endurance": p[105] * 10 ** p[106], "programs_per_page": p[110],
                    "ecc_bits": p[112], "planes": 1 << (p[113] & 0x0F),
                    "t_r_us": le(137, 2), "t_prog_us": le(133, 2), "t_bers_us": le(135, 2),
                    "sdr_modes": le(129, 2)}
    return None


def decode_setup(block):
    """The NAND set-up block the firmware keeps at XDATA 0x7100 (512 B from there; FA 13):
    the fields whose meaning is settled (they equal what build_loader_patch computes for
    the part), and the rest raw by offset (docs/nand.md)."""
    t = block[0x95] + 2
    return {"planes": block[0xF4], "sectors_per_page": block[0xF8], "ecc_t": t,
            "ecc_frame": f700.ECC_MODES[t]["row"] if t in f700.ECC_MODES and
                         f700.ECC_MODES[t]["row"] & 0xFF == block[0xB0] else None,
            "mode": block[0xA8], "raw": {0x71F0 + i: block[0xF0 + i] for i in range(0, 16, 2)}}


def nand_address(geo, block, page, column=0):
    """The column and row address cycles of (block, page), low byte first."""
    row = (block << (geo["pages_per_block"] - 1).bit_length()) | page
    return column.to_bytes(geo["col_cycles"], "little") + row.to_bytes(geo["row_cycles"], "little")


# == LLF PATTERN SLICES ==
# role: (complement, offset of page p); every test/screen/fill page is one of these.
LLF_SLICES = {
    "scratch": (True, lambda pg: 0),                       # 1e scratch self-test
    "screen":  (True, lambda pg: 0x80),                    # 18 screen / address-march pages
    "prepass": (False, lambda pg: pg),                     # 1e 015a pre-pass
    "fill":    (False, lambda pg: pg % 0x173 * 0x1000),    # 18/45 system fill
    "sys":     (True, lambda pg: pg % 0x173 * 0x1000),     # 1e system pages
    "meta":    (False, lambda pg: pg % 0x180 * 0x1000),    # 03 4 KB buffer
}


def llf(role, length, page=0):
    comp, off = LLF_SLICES[role]
    return llf_pattern(off(page), length, comp)


def llf_pair(k, length=1024):
    """The k-th 1 KB pattern buffer of a run: pair k//2, pattern for even k, complement
    for odd."""
    return tile(llf_pattern((k >> 1) * 0x800, 0x400, k & 1), length)


# == ANCHOR, INDEX PAGES, SYSTEM RECORDS ==
def build_anchor(geo, mirror=False):
    """The 0x80-B FTL anchor (block 0 page 0, mirror page 0x88); byte 0x2e = NAND attribute."""
    r = bytearray(0x80)
    r[0:5] = f700.ANCHOR_MAGIC
    r[0x08] = geo.planes
    for off, v in f700.ANCHOR_CONST.items():
        r[off] = v
    r[0x2E] = geo.nand_attr
    r[0x32:0x35] = bytes(f700.ANCHOR_SELF[mirror])
    return bytes(r)


INDEX_HEAD = 0xFFFF                     # index-page target of an FTL column's head page


def build_index_page(target=INDEX_HEAD, flag=0):
    """FA 0B 99 FTL index page (1024 B): the identity logical->physical ramp, and the
    trailer naming the boundary base it belongs to (None = plain page)."""
    page = bytearray(1024)
    page[0:256] = bytes(range(256))
    has, t = target is not None, target or 0
    page[0x280:0x287] = bytes((flag, has, t >> 8, t & 0xFF, 0x01, 0x00, has))
    return bytes(page)


def ftl_entry(geo, attr, page, blk):
    """A 12-B FTL page pointer (anchor format): attribute cluster, page, block in plane
    units (LE16), 0x30. attr 0x08 = firmware page, 0x01 = record."""
    return (bytes((0x0C, attr, 0x08, 0x83, geo.nand_attr, 0, 0, 0, page))
            + (blk * geo.planes).to_bytes(2, "little") + b"\x30")


def build_commit_record(geo, blk, lead, fw_first, nfw, fa13):
    """FTL configuration record of a system block's second commit page (0x200, twice);
    0x14b/0x14f/0x150 from the FA 13 register reply read while building it."""
    lane, idx = blk % geo.lanes, blk // geo.lanes
    b = bytearray(0x200)
    b[0:0x14] = bytes.fromhex("0d0605063801120000000002500000000003" "3810")
    b[0x14:0x20] = ftl_entry(geo, 0x08, fw_first, blk)
    b[0x20:0x2C] = ftl_entry(geo, 0x08, fw_first + 1, blk)
    b[0x100:0x107] = bytes((0xA0, 0, lane, idx >> 8, idx & 0xFF, 0, 0))
    b[0x10F] = 0x38
    b[0x111] = 1 if lead else 0
    b[0x14A], b[0x14E] = 0x51, 0x51
    if fa13 is not None and len(fa13) > 0x2D:
        b[0x14B] = (fa13[0x2D] & 7) | ((fa13[0x27] & 0x7C) << 1)
        b[0x14F], b[0x150] = fa13[0x26], fa13[0x27] & 3
    b[0x160:0x16C] = ftl_entry(geo, 0x01, fw_first + nfw, blk)
    b[0x185] = 0xAA
    b[0x194:0x196] = b"\x33\x4c"
    b[0x19F] = 0x51
    b[0x1A7:0x1AB] = bytes((0x05, 0x10, 0x02, 0x28))
    b[0x1B0] = 0x8A
    budget = 0x7F3 if lane else min(0x7F3, 0x7F2 - geo.lanes)
    b[0x1EC:0x200] = (bytes((0x01, 0x00, 0x01, 0xFF, 0x40, 0x00, geo.lanes, 0x00, 0x02, 0x00,
                             geo.lanes, 0x00, 0x20, 0x00, 0x08, 0x00)) + be16(budget) + be16(idx))
    return bytes(b) * 2


def build_frame_record(geo, blk, fw_first, nfw):
    """Third commit page record (0x100, twice): 8 ECC frames of a 4 KB firmware block,
    then a pointer to the previous commit page."""
    b = bytearray(0x100)
    b[0:0x23] = bytes.fromhex("0d06050638" "00000002" "00005b" "000000000000" "3810"
                              "00680100" "00000000000000" "2b" "000008")
    for k in range(8):
        b[0x2B + 5 * k:0x30 + 5 * k] = bytes((0, 0, k)) + be16(k * f700.ECC_ROW_512)
    b[0x5B:0x67] = ftl_entry(geo, 0x01, fw_first + nfw + 1, blk)
    b[0x67:0x6A] = b"\xaa\xff\xff"
    return bytes(b) * 2


# Firmware-pass commit pages, in order: (header builder, sectors its read-back verifies).
COMMIT_HEADERS = (
    (lambda geo, blk, lead, first, nfw, fa13: ecc_param_record(f700.ECC_DEFAULT)[:0x200], 2),
    (build_commit_record, 2),
    (lambda geo, blk, lead, first, nfw, fa13: build_frame_record(geo, blk, first, nfw), 1),
)


def patch_bootfw(geo, fw, pages, sys_blk, ds_blk, spares):
    """The boot image as written to a system block: its FTL self-descriptor patched into
    the tails of 4 KB blocks 0 and 1. pages = the pages of firmware blocks 1..; spares =
    the four FTL spare blocks."""
    b = bytearray(fw)
    cs = geo.cell_slices

    def put(off, vals):
        for i, v in enumerate(vals):
            b[off + 2 * i:off + 2 * i + 2] = be16(v)
    b[0xF14:0xF20] = f700.FW_DESC_HEAD
    put(0xF30, range(cs))
    put(0xF50, [geo.page_bytes] * cs)
    for i, pair in enumerate(((sys_blk, ds_blk), spares[:2], spares[2:4])):
        put(0xF70 + 8 * i, (*pair, 0xFFFF, 0xFFFF))
    b[0xF89], b[0xF8F] = cs, 1
    put(0xFFE, [-(-len(fw) // 0x1000)])
    put(0x1F50, [f700.ECC_ROW_512 * k for k in range(8)])
    b[0x1F60:0x1FB0] = b"\xff" * 0x50
    put(0x1F60, pages)
    b[0x1FB0:0x1FF0] = b"\xff" * 0x40
    put(0x1FB0, [0x1000 + i for i in range(cs)])
    return bytes(b)


def descriptor_mask(geo, nbytes):
    """Byte positions patch_bootfw owns."""
    a = patch_bootfw(geo, b"\x00" * nbytes, [1], 0, 0, [0, 0, 0, 0])
    b = patch_bootfw(geo, b"\xff" * nbytes, [1], 0, 0, [0, 0, 0, 0])
    return [i for i in range(min(nbytes, 0x2000)) if a[i] == b[i]]


# == LANES AND CAPACITY ==
def initial_data_blocks(geo, lane):
    return [f700.DATA_FIRST + geo.lanes * i + lane for i in range(f700.DS_HEAD_ENTRIES[lane])]


def build_lane_bitmaps(geo, marked):
    """System-record page: per lane a bitmap, bit i = lane block lane+lanes*i marked
    (bad or allocated), its initial data blocks, every slot past the lane's blocks."""
    zone = int(module_feature(module_name(geo.fid))[3])
    out = bytearray()
    for lane in range(geo.lanes):
        b = bytearray(zone // 8)
        idx = set(range(geo.lane_blocks, zone))
        idx |= {(blk - lane) // geo.lanes for blk in (*marked, *initial_data_blocks(geo, lane))
                if blk % geo.lanes == lane and blk < geo.lanes * geo.lane_blocks}
        for i in idx:
            b[i >> 3] |= 1 << (i & 7)
        out += b
    return bytes(out)


def marks_from_bitmaps(geo, bitmaps):
    """Inverse of build_lane_bitmaps (without the initial data blocks and the tail)."""
    size, marked = len(bitmaps) // geo.lanes, set()
    for lane in range(geo.lanes):
        rec = bitmaps[lane * size:(lane + 1) * size]
        skip = {(b - lane) // geo.lanes for b in initial_data_blocks(geo, lane)}
        marked |= {lane + geo.lanes * i for i in range(geo.lane_blocks)
                   if rec[i >> 3] >> (i & 7) & 1 and i not in skip}
    return marked


def build_ds_header(geo, lane):
    """2 KB FTL header of data-superblock head page `lane` (field semantics INFERRED)."""
    b = bytearray(0x800)
    cs, n = geo.cell_slices, f700.DS_HEAD_ENTRIES[lane]
    b[0x000:0x100] = b[0x240:0x340] = bytes(range(256))
    b[0x100:0x200] = b"\xff" * 0x100
    b[0x220:0x240] = b"\xff" * 0x20
    b[0x360:0x37C] = bytes((2, 0, 0, 1, 0, 0, 0, cs, cs, 0x18, 0, 1, 0, 0, n, 0,
                           0, 0, 0, 0, 0, 1, 0, 0, 0xFF, 0xFF, 0, 0))
    row = f700.ECC_MODES[f700.ECC_DEFAULT]["row"]
    for k in range(geo.sectors_per_page // 2):
        b[0x3A0 + 2 * k:0x3A2 + 2 * k] = be16(row * k)
    b[0x400:0x402] = b"\xff\xff"
    for i, blk in enumerate(initial_data_blocks(geo, lane)):
        b[0x700 + 2 * i:0x702 + 2 * i] = be16(blk)
    b[0x7E0:0x800] = bytes(range(0x20, 0, -1))
    return bytes(b)


def lane_reserves(geo):
    """(per-lane reserve, system-lane extra) in blocks from the module's [MODULE_FETURE]
    line (_3D_MLC branch: f6*f7 + 3 + f9 bits 4-7, hi(f8) + 2 + lo(f8))."""
    name = module_name(geo.fid)
    f = module_feature(name)
    if "_3D_MLC" not in name.upper():
        raise NotImplementedError(f"lane reserves for module {name}: only _3D_MLC is ported")
    f8, f9 = int(f[8], 16), int(f[9], 16)
    return int(f[6]) * int(f[7]) + 3 + ((f9 >> 4) & 0xF), (f8 >> 8) + 2 + (f8 & 0xFF)


def lane_counts(geo, marked, per_lane_extra=0):
    """Usable blocks per FTL lane: its blocks less the marked ones, `per_lane_extra`
    (FTL map blocks), the reserve; the first lane with room also takes the extra."""
    reserve, extra = lane_reserves(geo)
    counts, sys_lane = [], None
    for lane in range(geo.lanes):
        used = (sum(1 for b in marked if b % geo.lanes == lane and b < geo.lanes * geo.lane_blocks)
                + per_lane_extra + reserve)
        if used + 8 >= geo.lane_blocks:
            counts.append(0)
            continue
        if sys_lane is None and used + extra + 8 <= geo.lane_blocks:
            sys_lane, used = lane, used + extra
        counts.append(geo.lane_blocks - used)
    return counts


def build_capacity_record(geo, counts, limit=None):
    """The 99 record: @0x40 capacity in sectors (LE32, capped at `limit`), @0x100 usable
    blocks per lane (BE16)."""
    b = bytearray(512)
    b[0x14] = 1
    cap = sum(counts) * geo.pages_per_block * geo.sectors_per_page
    if limit is not None:
        if limit > cap:
            print(f"NOTE: --capacity {limit} sectors exceeds the usable {cap}; using {cap}")
        cap = min(cap, limit)
    b[0x40:0x44] = cap.to_bytes(4, "little")
    b[0x44] = 0x51
    b[0xFE:0x100] = b"JN"
    for i, c in enumerate(counts):
        b[0x100 + 2 * i:0x102 + 2 * i] = be16(c)
    b[0x1FF] = 0x51
    return bytes(b)


# == SWEEPS: SKIP BITMAPS, VERDICTS, COLUMNS ==
def block_bitmap(skip, first, n, stride=4, limit=None):
    """FA 0B 04: bit i = block first+stride*i is skipped (in `skip`, or at/past `limit`)."""
    b = bytearray(512)
    for i in range(n):
        blk = first + stride * i
        if blk in skip or (limit is not None and blk >= limit):
            b[i >> 3] |= 1 << (i & 7)
    return bytes(b)


def sweep_bad_blocks(reads, pages, sweep="c0"):
    """Blocks a read sweep failed: a page value above SWEEP_FAILED."""
    bad = set()
    for key, datas in reads.items():
        if key[0] == sweep:
            for d in datas:
                for i in range(len(d) // pages):
                    if max(d[i * pages:(i + 1) * pages]) > f700.SWEEP_FAILED:
                        bad.add(key[1] + 4 * i)
    return bad


def screen_bad_blocks(reads, pattern):
    """Blocks the screen failed: page-1 read-back differs from the pattern, or an erase
    reply is nonzero (keeps ("screen", block) / ("screen erase", block))."""
    bad = set()
    for key, datas in reads.items():
        if key[0] == "screen" and any(d[:len(pattern)] != pattern for d in datas):
            bad.add(key[1])
        elif key[0] == "screen erase" and any(d[:1] != b"\0" for d in datas):
            bad.add(key[1])
    return bad


def failed_blocks(reads):
    """Blocks a 05 report flagged (keep = ("05", first, entries, stride))."""
    out = set()
    for key, datas in reads.items():
        if key[0] == "05":
            _, first, n, stride = key
            for d in datas:
                out |= {first + stride * i for i in range(min(n, len(d) * 8)) if d[i >> 3] >> (i & 7) & 1}
    return out


def block_status(geo, reads, also_failed=()):
    """Per block: its worst c0/c1 page value, SWEEP_FAIL_MARK if a 05 report (or
    `also_failed`, the screen) flagged it, FF past the die; 0 (untested) without sweeps."""
    b = bytearray(b"\xff" * geo.block_span)
    seen = set()
    for sweep, pages in (("c0", geo.pages_per_block // 2), ("c1", geo.pages_per_block)):
        for key, datas in reads.items():
            if key[0] != sweep:
                continue
            for d in datas:
                for i in range(len(d) // pages):
                    blk = key[1] + geo.lanes * i
                    if blk < geo.blocks:
                        v = max(d[i * pages:(i + 1) * pages])
                        b[blk] = v if blk not in seen else max(b[blk], v)
                        seen.add(blk)
    if not seen:
        b[:geo.blocks] = bytes(geo.blocks)
    for blk in failed_blocks(reads) | set(also_failed):
        if blk < geo.blocks:
            b[blk] |= f700.SWEEP_FAIL_MARK
    return bytes(b)


class ColumnError(RuntimeError):
    """A measured bad-column list that cannot be laid out."""


def build_column_table(geo, row, frame_sectors, sectors, bad_cols=None, cap=0x1C0):
    """FA 0B 94 (2048 B): the ECC frame layout with each entry's bad columns skipped; a
    frame grows by every bad column it spans. row = frame bytes (param 0x2e)."""
    n = geo.program_passes
    b = bytearray(0x200) + b"\xff" * 0x600
    b[0] = n
    frames = sectors // frame_sectors
    for e in range(n):
        cols = sorted((bad_cols or {}).get(e, ()))
        if len(cols) > cap // n or row * frames + len(cols) > geo.raw_page_span:
            raise ColumnError(f"{len(cols)} bad columns do not fit the {row}-B x {frames} frame "
                              f"layout of a {geo.raw_page_span}-B page (list cap {cap // n})")
        b[0x10 + 2 * e:0x12 + 2 * e] = be16(len(cols))
        start = 0
        for k in range(frames):
            end = row * (k + 1)
            for c in cols:
                end += c < end
            b[0x20 + 0x40 * e + 2 * k:0x22 + 0x40 * e + 2 * k] = be16(start)
            b[0xA0 + 0x20 * e + 2 * k:0xA2 + 0x20 * e + 2 * k] = be16(start - row * k)
            b[0xE0 + 0x20 * e + k] = end - start - row
            start = end
        lst = 0x200 + 2 * (cap // n) * e
        for i, c in enumerate(cols):
            b[lst + 2 * i:lst + 2 * i + 2] = c.to_bytes(2, "little")
    return bytes(b)


def column_weights(reads, patterns, span):
    """Per byte column: summed popcount(read ^ pattern) over the raw 92 read-backs, within
    the physical page (`span`; past it is controller buffer leftovers)."""
    w, tiled = [0] * span, {}
    for rd, unit in zip(reads, patterns):
        pat = tiled.setdefault(unit, tile(unit, span))
        n = min(len(rd), span)
        for o in range(0, n, 64):
            if rd[o:o + 64] != pat[o:o + 64]:
                for i in range(o, min(o + 64, n)):
                    if rd[i] != pat[i]:
                        w[i] += bin(rd[i] ^ pat[i]).count("1")
    return w


def bad_columns(reads, patterns, row, frames, span, cap=f700.COLUMN_CAP):
    """Bad columns of one FTL slot, AlcorMP's greedy: within the page slack budget, take
    the heaviest column of the heaviest frame (frames slide right past each pick) until
    no error is left. Returned sorted."""
    w = column_weights(reads, patterns, span)
    budget = min(span - frames * row, cap, 0x1BF)
    cols = list(range(span))
    tot = [sum(w[k * row:(k + 1) * row]) for k in range(frames)]
    chosen = []
    for _ in range(max(budget, 0)):
        b = max(range(frames), key=lambda k: (tot[k], -k))
        if tot[b] == 0:
            break
        lo = b * row
        j = max(range(lo, lo + row), key=lambda i: (w[cols[i]], -i))
        c = cols.pop(j)
        chosen.append(c)
        tot[b] -= w[c]
        for k in range(b, frames):
            if k > b:
                tot[k] -= w[cols[k * row - 1]]
            if (k + 1) * row - 1 < len(cols):
                tot[k] += w[cols[(k + 1) * row - 1]]
    return sorted(chosen)


# == HOUSEKEEPING TABLES (FA 0B 21) ==
# Table types (page-list entry = type | page, LE16); 0x400 B of plaintext per page.
HK_HEADER, HK_LIST, HK_SYSREC, HK_MAP, HK_STATUS, HK_LANES, HK_INFO = (
    0x2000, 0x4000, 0xC000, 0x6000, 0x8000, 0xE000, 0x0000)
HK_PAGE = 0x400


def hk_layout(geo):
    """(type, plaintext length) per table, in page-list order."""
    return [(HK_HEADER, HK_PAGE), (HK_LIST, HK_PAGE), (HK_SYSREC, HK_PAGE), (HK_MAP, HK_PAGE),
            (HK_STATUS, geo.block_span), (HK_LANES, geo.lanes * 0x1000), (HK_INFO, HK_PAGE)]


def hk_page_list(geo):
    b, pg = bytearray(b"\xff" * HK_PAGE), 0
    for typ, length in hk_layout(geo):
        for _ in range(-(-length // HK_PAGE)):
            b[2 * pg:2 * pg + 2] = (typ | pg).to_bytes(2, "little")
            pg += 1
    return bytes(b)


def hk_header(geo, date, chip=f700.CHIP_ID):
    """Header struct: where the other tables are, the chip, the format date."""
    b = bytearray(0x100)
    for off, v in f700.HK_HEADER_STATE.items():
        b[off] = v
    pages = [typ for typ, length in hk_layout(geo) for _ in range(-(-length // HK_PAGE))]
    b[0xA2:0xA4] = be16(pages.index(HK_LIST))
    b[0xC8:0xCA] = be16(pages.index(HK_SYSREC))
    b[0xB0:0xB2] = be16(chip)
    b[0xE0] = geo.block_span >> 8
    lanes = geo.lanes * 0x1000
    b[0xED], b[0xFE] = (lanes >> 8) & 0xFF, (lanes >> 16) & 0xFF
    b[0xF0:0xFA] = b"\xff" * 10
    b[0xFA:0xFE] = date.year.to_bytes(2, "big") + bytes((date.month, date.day))
    return bytes(b)


def hk_sysrec(capacity):
    b = bytearray(HK_PAGE)
    b[0] = 0xAA
    b[0x27:0x2B] = capacity.to_bytes(4, "little")
    b[0x2B] = 0x80
    b[0x100:0x200] = b"\xff" * 0x100
    return bytes(b)


def hk_map(entries=0x100):
    b = bytearray(b"\xff" * HK_PAGE)
    for i in range(entries):
        b[2 * i:2 * i + 2] = i.to_bytes(2, "little")
    return bytes(b)


def hk_lane_record(geo, lane, good):
    """One lane's 4 KB record; besides the geometry and `good`, F700 constants."""
    b = bytearray(0x1000)
    b[0x00] = 0x80
    b[0x12] = b[0x29] = lane
    b[0x14:0x18] = lane.to_bytes(4, "big")
    b[0x18:0x1C] = geo.lane_blocks.to_bytes(4, "big")
    b[0x1C] = 1
    b[0x20:0x24] = (0xFFFF).to_bytes(4, "big")
    b[0x24:0x26] = be16(geo.pages_per_block)
    b[0x26] = geo.sectors_per_page
    b[0x28] = geo.lanes
    b[0x2A:0x2E] = geo.capacity_mib.to_bytes(4, "big")
    b[0x2F] = 0x1E
    b[0x30:0x34] = good.to_bytes(4, "big")
    b[0x34], b[0x36], b[0x38], b[0x52] = 8, 1, 4, 1
    b[0xC5:0xE5] = b"\xff" * 0x20
    b[0xE5], b[0xE9] = 4, 1
    b[0x400:0x800] = b"\xff" * 0x400
    b[0x800:0xA00] = bytes(range(256)) * 2
    b[0xA00:0xC00] = b"\xff" * 0x200
    b[0xC40:0xC80] = b"\x55" * 0x40
    b[0xC80:0xD00] = b"\xff" * 0x80
    return bytes(b)


def hk_tables(geo, reads, capacity, date, status=None, also_failed=()):
    """{type: plaintext}; `status` (a kept block-status table) replaces the sweeps'."""
    if status is None:
        status = block_status(geo, reads, also_failed)
    bad = [sum(1 for b in range(lane, geo.blocks, geo.lanes) if status[b] > f700.SWEEP_FAILED)
           for lane in range(geo.lanes)]
    info = bytearray(HK_PAGE)
    info[0:9] = f700.HK_INFO_HEAD
    info[0x40:0x49] = b"alcortool"                         # host name field
    return {HK_HEADER: hk_header(geo, date), HK_LIST: hk_page_list(geo),
            HK_SYSREC: hk_sysrec(capacity), HK_MAP: hk_map(), HK_STATUS: status,
            HK_LANES: b"".join(hk_lane_record(geo, ln, geo.lane_blocks - bad[ln]) for ln in range(geo.lanes)),
            HK_INFO: bytes(info)}


def hk_pages(geo, tables):
    """The encoded 2048-B pages in page-list order (header and info pages repeat theirs)."""
    pages = []
    for typ, length in hk_layout(geo):
        plain = tables[typ]
        for p in range(-(-length // HK_PAGE)):
            if typ == HK_HEADER:
                pages.append(b"".join(ftlcrypt.encode_header(plain) for _ in range(4)))
            elif typ == HK_INFO:
                pages.append(b"".join(ftlcrypt.encode_chunk(plain[:0x100], 0) for _ in range(4)))
            else:
                pages.append(b"".join(ftlcrypt.encode_chunk(plain[o:o + 0x100], o // 0x100)
                                      for o in range(p * HK_PAGE, (p + 1) * HK_PAGE, 0x100)))
    return pages


def hk_decode(geo, pages):
    """{type: plaintext} of the stored pages, or None unless the page list decodes to
    hk_page_list (which proves keys and layout). The header comes back at its struct
    offsets (0x20-0x27 hold 0x08-0x0f, see ftlcrypt.decode_header)."""
    out, first = {}, 0
    for typ, length in hk_layout(geo):
        n = -(-length // HK_PAGE)
        if typ == HK_HEADER:
            out[typ] = bytes(0x10) + ftlcrypt.decode_header(pages[first][:0x200])
        elif typ == HK_INFO:
            out[typ] = ftlcrypt.decode_chunk(pages[first][:0x200], 0)
        else:
            out[typ] = b"".join(ftlcrypt.decode_chunk(pages[first + p][o:o + 0x200], (p * HK_PAGE + o // 2) // 0x100)
                                for p in range(n) for o in range(0, len(pages[first + p]), 0x200))[:length]
        first += n
    return out if out[HK_LIST] == hk_page_list(geo) else None


def hk_summary(geo, tables):
    """What AlcorMP's last LLF left in the tables: date, chip, host/user, INI name,
    planned capacity, good blocks per lane, the bad blocks; the boundary bases, which the
    sweeps skip by design, are listed as system blocks instead."""
    h, sysrec, info, lanes = tables[HK_HEADER], tables[HK_SYSREC], tables[HK_INFO], tables[HK_LANES]
    text = lambda b: b.split(b"\0")[0].decode("ascii", errors="replace").strip()
    return {"date": f"{int.from_bytes(h[0xFA:0xFC], 'big')}-{h[0xFC]:02}-{h[0xFD]:02}",
            "chip": f"{int.from_bytes(h[0xB0:0xB2], 'big'):04X}",
            "host": text(info[0x40:0x60]), "user": text(info[0x60:0x80]), "ini_name": text(sysrec[0x02:0x22]),
            "planned_capacity": int.from_bytes(sysrec[0x27:0x2B], "little"),
            "lane_good": [int.from_bytes(lanes[ln * 0x1000 + 0x30:ln * 0x1000 + 0x34], "big")
                          for ln in range(geo.lanes)],
            "bad": [b for b in range(geo.blocks)
                    if tables[HK_STATUS][b] > f700.SWEEP_FAILED and b not in f700.BOUNDARY_BASES],
            "system": sorted(b for b in f700.BOUNDARY_BASES if tables[HK_STATUS][b] > f700.SWEEP_FAILED)}


# == CLOSING RECORDS ==
def build_options(ident):
    """F5 option record: 0x51 = on; [9]/[0xa] unless identify already has 'P' at 0x38."""
    b = bytearray(512)
    b[1] = b[3] = b[0xB] = b[0xF] = 0x51
    if len(ident) > 0x38 and ident[0x38] != 0x50:
        b[9], b[0xA] = 0x51, 0x50
    return bytes(b)


def build_mp_info(sectors, date):
    """The 81 51 04 MP info sector of a stick of `sectors`, formatted on `date`."""
    b = bytearray(b"\xff" * 512)
    b[0x32:0x132] = bytes(0x100)
    for off, hexs in f700.MP_INFO_BLOCK.items():
        v = bytes.fromhex(hexs)
        b[off:off + len(v)] = v
    b[f700.MP_INFO_DATE:f700.MP_INFO_DATE + 4] = date.year.to_bytes(2, "big") + bytes((date.month, date.day))
    b[f700.MP_INFO_CAPACITY:f700.MP_INFO_CAPACITY + 4] = sectors.to_bytes(4, "big")
    b[0x140] = 0
    b[0x1FE:0x200] = b"\x55\xaa"
    return bytes(b)


def build_wp_block(on):
    """The 9B data block: byte 1 sets INTMEM 9A bits 1 (policy WP) and 2; bit 6 sets both."""
    b = bytearray(512)
    b[1] = f700.WP_POLICY if on else 0
    return bytes(b)


def decode_mp_info(sec):
    """{date, capacity, unknown} of an MP info sector (the last LLF), or None when blank;
    `unknown` lists the offsets of the constant block that differ from AlcorMP's."""
    if len(sec) < 512 or sec[0x1FE:0x200] != b"\x55\xaa":
        return None
    d, c = f700.MP_INFO_DATE, f700.MP_INFO_CAPACITY
    year = int.from_bytes(sec[d:d + 2], "big")
    return {"date": f"{year}-{sec[d + 2]:02}-{sec[d + 3]:02}" if 0 < year < 0xFFFF else None,
            "capacity": int.from_bytes(sec[c:c + 4], "big"),
            "unknown": [off for off, v in f700.MP_INFO_BLOCK.items()
                        if sec[off:off + len(v) // 2] != bytes.fromhex(v)]}


# == INFO RECORD ==
def record_capacity(rec):
    """Capacity in sectors from an info record, or None when it is empty (as erased)."""
    if len(rec) < 0x0E or rec[9] != 0xF1:
        return None
    cap = int.from_bytes(rec[0x0A:0x0E], "big")
    return None if cap in (0, 0xFFFFFFFF) else cap


def decode_record(rec, lanes=4):
    """{field: value} of an info record, or None when empty (tag F1 missing)."""
    if len(rec) < 0x200 or rec[9] != 0xF1:
        return None
    out = {f"{src} ({tag:02X})": int.from_bytes(rec[o:o + 4], "big")
           for o, (tag, src, _) in f700.RECORD_VALUES.items()}
    out.update({f"bit {bit}": rec[o] for o, (bit, _, _) in f700.RECORD_FLAGS.items()})
    out["UFDMode"] = ufd_mode(rec)
    out["lanes"] = [int.from_bytes(rec[f700.RECORD_LANES + 2 * i:f700.RECORD_LANES + 2 * i + 2], "big")
                    for i in range(lanes)]
    return out


def ufd_mode(rec):
    """AlcorMP's UFDMode, from the record's flag bytes that hold its bits."""
    return sum(1 << i for i, off in enumerate(f700.RECORD_UFDMODE) if rec[off])


# == CONFIG BLOCK ==
def usb_string(desc):
    """Text of a USB string descriptor (bLength, 0x03, UTF-16LE), or None."""
    if len(desc) < 2 or desc[1] != 0x03:
        return None
    return desc[2:desc[0]].decode("utf-16-le", errors="replace")


def usb_desc(text):
    b = text.encode("utf-16-le")
    if len(b) + 2 > 255:
        raise ValueError("string too long for a USB descriptor")
    return bytes([len(b) + 2, 0x03]) + b


def config_valid(cfg):
    return len(cfg) == 512 and cfg[:2] == b"\x99\x07"


def config_checksum(cfg):
    """(offset, stored, computed) of the strings checksum (sum of the bytes before it)."""
    ck = 0x16 + cfg[2] + cfg[3] + 28
    return ck, cfg[ck], sum(cfg[:ck]) & 0xFF


def config_fields(cfg):
    """{vid_pid, usb, scsi, serial, ...} of a valid config block, for display."""
    dev, s1 = cfg[4:0x16], 0x16
    s2 = s1 + cfg[2]
    off = s2 + cfg[3]
    txt = lambda b: b.decode("ascii", errors="replace").rstrip()
    ck, stored, computed = config_checksum(cfg)
    serial = usb_string(cfg[0x80:]) if cfg[0x7E:0x80] == b"\x3c\xc3" else None
    end = 0x80 + cfg[0x80]
    return {"vid_pid": f"{dev[9]:02x}{dev[8]:02x}:{dev[11]:02x}{dev[10]:02x}",
            "usb": (usb_string(cfg[s1:s2]), usb_string(cfg[s2:off])),
            "scsi": (txt(cfg[off:off + 8]), txt(cfg[off + 8:off + 24]), txt(cfg[off + 24:off + 28])),
            "checksum_ok": stored == computed and cfg[ck + 1:ck + 3] == b"\xaa\x55",
            "serial": serial,
            "serial_ok": serial is not None and (sum(cfg[0x80:end]) & 0xFF) == cfg[end],
            "bcd_usb": f"{dev[3]:x}.{dev[2]:02x}", "bcd_device": f"{dev[13]:x}.{dev[12]:02x}",
            "max_packet": dev[7], "sizes": cfg[0xC0:0xC8].hex(" "), "7c": cfg[0x7C:0x7E].hex(" "),
            "tag": txt(cfg[0xD5:0xD8].rstrip(b"\0"))}


def config_serial(cfg):
    return usb_string(cfg[0x80:0x80 + cfg[0x80]]) or ""


def _place_serial(cfg, serial):
    sd = usb_desc(serial)
    if 0x80 + len(sd) >= 0xC0:
        raise ValueError("serial too long")
    cfg[0x7E:0x80] = b"\x3c\xc3"
    cfg[0x80:0xC0] = bytes(0x40)
    cfg[0x80:0x80 + len(sd)] = sd
    cfg[0x80 + len(sd)] = sum(sd) & 0xFF


def default_config(serial=None):
    """AlcorMP's default config block (058f:6387, 'Generic Flash Disk'); `serial`
    defaults to 8 random hex digits, as AlcorMP's."""
    d = f700.CONFIG_DEFAULT
    d1, d2 = (usb_desc(t) for t in d["usb"])
    v, p, r = d["scsi"]
    body = d1 + d2 + v.encode().ljust(8) + p.encode().ljust(16) + r.encode()
    cfg = bytearray(512)
    cfg[0:4] = bytes([0x99, 0x07, len(d1), len(d2)])
    cfg[4:4 + len(d["device"])] = d["device"]
    cfg[0x16:0x16 + len(body)] = body
    ck = 0x16 + len(body)
    cfg[ck] = sum(cfg[:ck]) & 0xFF
    cfg[ck + 1:ck + 3] = b"\xaa\x55"
    cfg[0x7C:0x7E] = d["7c"]
    _place_serial(cfg, serial or secrets.token_hex(4).upper())
    cfg[0xC0:0xC8] = d["sizes"]
    cfg[0xD4] = secrets.randbelow(256)
    cfg[0xD5:0xD8] = d["tag"]
    return bytes(cfg)


def build_config(base, vendor=None, product=None, serial=None):
    """`base` with new strings; SCSI vendor/product mirror the USB strings, everything
    else (device descriptor, revision, 0x7C, sizes, tag) is kept."""
    cfg = bytearray(base)
    if not config_valid(cfg):
        raise ValueError("base is not a valid 99 07 config block")
    old = 0x16 + cfg[2] + cfg[3]
    v = usb_string(cfg[0x16:0x16 + cfg[2]]) or "" if vendor is None else vendor
    p = usb_string(cfg[0x16 + cfg[2]:old]) or "" if product is None else product
    rev = bytes(cfg[old + 24:old + 28])
    d1, d2 = usb_desc(v), usb_desc(p)
    ck = 0x16 + len(d1) + len(d2) + 28
    if ck + 3 > 0x7C:
        raise ValueError(f"strings too long: block would reach {ck + 3:#x}, limit 0x7c")
    cfg[0x16:0x7C] = bytes(0x7C - 0x16)
    cfg[2], cfg[3] = len(d1), len(d2)
    cfg[0x16:ck - 28] = d1 + d2
    o = ck - 28
    cfg[o:o + 8] = v.encode("ascii", "replace")[:8].ljust(8, b" ")
    cfg[o + 8:o + 24] = p.encode("ascii", "replace")[:16].ljust(16, b" ")
    cfg[o + 24:o + 28] = rev
    cfg[ck] = sum(cfg[:ck]) & 0xFF
    cfg[ck + 1:ck + 3] = b"\xaa\x55"
    if serial is not None:
        _place_serial(cfg, serial)
    return bytes(cfg)
