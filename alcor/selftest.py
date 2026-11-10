"""Offline self-test: builders, CDB layouts and the generated stream (no device). The
byte-exact comparison against AlcorMP's captures is re/oracle_selftest.py."""
import builtins

from . import asm51, f700, ftlcrypt, provision
from .nand import DataMissing, Geometry, device_bootfw, loader_image, releases
from .payloads import (bad_columns, build_anchor, build_wp_block, build_column_table, build_index_page, build_loader_patch,
                       build_param, config_valid, own_loader_image)
from .protocol import COMMANDS, PROBE_SIGNATURE, erase_cdb, nand_cdb

REFERENCE = "2c 84 44 32 aa 04"         # F700 + Micron L05B, the part both captures ran on

CHECKS = []


def check(fn=None, *, data=True):
    """Register a check; data=False: it runs without imported AlcorMP data."""
    def add(f):
        CHECKS.append((f, data))
        return f
    return add(fn) if fn else add


@check
def geometry():
    g = Geometry(REFERENCE)
    want = dict(cell_type=1, capacity_mib=16384, spare_per_page=2208, pages_per_block=512, sectors_per_page=32,
                interleave_class=3, ecc_code=0x17, program_passes=1, lanes=4, ftl_slots=8, superblock_stride=0xF4)
    return {k: getattr(g, k) for k in want} == want


@check(data=False)
def cdb_layouts():
    """Builders against CDBs from the captures."""
    vectors = [("fa 0c 00 33 00 00 00 00", COMMANDS["chip_erase"].cdb),
               ("fa 0b 11 00 00 58 01 00", erase_cdb(0, 0x58, 1)),
               ("fa 0b 1f 00 00 58 00 05 00 20 00 00 01 00 05 00", nand_cdb(0x1F, 0, 0x58, 5, 0x20, f700.R_SCRATCH)),
               ("fa 0b 1f 00 00 00 00 dd 00 1c 01 5a 00 00 00 00", nand_cdb(0x1F, 0, 0, 0xDD, 0x1C, f700.R_DATA, frame=0)),
               ("fa 0b 1e 00 00 b2 00 ff 00 1c ff ff 00 00 ff 00", nand_cdb(0x1E, 0, 0xB2, 0xFF, 0x1C, f700.R_SYS)),
               ("fa 0b 18 00 00 24 00 01 00 20 00 00 00 00 00 00", nand_cdb(0x18, 0, 0x24, 1, 0x20, frame=0)),
               ("9b" + " 00" * 15, COMMANDS["set_wp"].cdb)]       # write-protect: hardware 2026-11-09
    return (all(bytes.fromhex(want) == got for want, got in vectors)
            and build_wp_block(True)[:2] == b"\0\x02" and build_wp_block(False) == bytes(512))


@check
def builders():
    g = Geometry(REFERENCE)
    p, a, m = build_param(g), build_anchor(g), build_anchor(g, mirror=True)
    idx = build_index_page()
    return (len(p) == 2048 and p[0] == g.ce_count and p[0x59] == 0xDA and len(a) == 0x80 and a[0x2E] == 0xDA
            and a[:5] == f700.ANCHOR_MAGIC and a[0x32:0x35] != m[0x32:0x35]
            and len(loader_image("rwfw", build_loader_patch(g))) == f700.LOADER_UPLOAD
            and device_bootfw(g.fid)[:3] == bytes.fromhex("02d51f") and idx[:4] == bytes(range(4)))


@check
def bad_column_selection():
    """A few flips -> all of them; a noisy page -> the slack budget, heaviest first."""
    g = Geometry(REFERENCE)
    span, row, frames, pat = 18592, 0x480, 16, bytes(range(256)) * 4
    few, noisy = bytearray((pat * 19)[:span]), bytearray((pat * 19)[:span])
    for c in (5, 1200, 18000):
        few[c] ^= 1
    for c in range(3, 18400, 50):
        noisy[c] ^= 1
    noisy[7] ^= 7
    got_few = bad_columns([bytes(few)], [pat], row, frames, span)
    got_noisy = bad_columns([bytes(noisy)], [pat], row, frames, span)
    build_column_table(g, row, 2, 32, {0: got_noisy})
    build_param(g, ecc_t=0x48, slot_cols=len(got_noisy))
    return got_few == [5, 1200, 18000] and len(got_noisy) == span - row * frames and 7 in got_noisy


@check
def streams():
    """Every sweep mode generates under loaders that serve each op (LoaderError otherwise),
    writes an identity block and reads nothing from re/ (no capture or blob at runtime)."""
    g, opened, orig = Geometry(REFERENCE), [], builtins.open
    builtins.open = lambda path, *a, **k: (opened.append(str(path)), orig(path, *a, **k))[1]
    try:
        streams = [provision.generate(g, sweeps=sw) for sw in ("full", "erase", "none")]
    finally:
        builtins.open = orig
    return (not [p for p in opened if "/re/" in p.replace("\\", "/")]
            and all(any(o.cdb[:3] == b"\x81\x00\xff" and config_valid(o.data()) for o in ops) for ops in streams))


@check
def ftl_codec():
    return ftlcrypt.self_test()


@check(data=False)
def assembler():
    """Encodings the loaders rely on, and a disassemble/reassemble round trip of the probe."""
    known = {"MOV 0x22,0x15": "851522", "JNB ACC.7,$+3": "30e700", "DJNZ R7,$": "dffe",
             "MOVC A,@A+DPTR": "93", "LCALL 0x66C2": "1266c2", "MOV DPTR,#0x4400": "904400",
             "CJNE R0,#0,$": "b800fd", "MOV DPH,R2": "8a83", "MOV R2,DPH": "aa83", "SETB 0x2D.5": "d26d"}
    ok = all(asm51.assemble(src, 0x100)[0].hex() == want for src, want in known.items())
    code = own_loader_image("probe")[:f700.LOADER_PATCH_AT].rstrip(b"\xff")
    sig = code.index(PROBE_SIGNATURE)
    for addr, raw, text in asm51.disasm(code[:sig], f700.LOADER_BASE):
        ok &= asm51.assemble(f"ORG {addr}\n{text}", addr)[0] == raw
    return ok


@check(data=False)
def own_loader():
    """The probe image keeps the ROM contract: 31 LJMP slots inside the window, the constant
    slots, the patch marker, the size of an upload."""
    img, base = own_loader_image("probe"), f700.LOADER_BASE
    slots = [int.from_bytes(img[3 * i + 1:3 * i + 3], "big") for i in range(31)]
    if len(img) != f700.LOADER_UPLOAD or any(img[3 * i] != 0x02 for i in range(31)):
        return False
    if not all(base <= t < base + f700.LOADER_PATCH_AT for t in slots):
        return False
    const = {2: 0x80, 3: 0x01, 7: 0x01, 19: 0x00, 22: 0x01}          # MOV R7,#v; RET
    return (all(img[slots[n] - base:slots[n] - base + 3] == bytes((0x7F, v, 0x22)) for n, v in const.items())
            and img[f700.LOADER_PATCH_AT + 0x1FE:f700.LOADER_PATCH_AT + 0x200] == b"JN"
            and PROBE_SIGNATURE in img)


def run():
    try:
        releases()
        have = True
    except DataMissing as e:
        have = False
        print(f"{e}\nrunning the checks that need no AlcorMP data")
    fails = skipped = 0
    for fn, data in CHECKS:
        if data and not have:
            skipped += 1
            continue
        try:
            ok = fn()
        except Exception as e:                             # noqa: BLE001  a crash is a failure
            ok = False
            print(f"  {fn.__name__}: {type(e).__name__}: {e}")
        fails += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {fn.__name__}")
    print("SELF-TEST:", "PASS" if not fails else f"FAIL ({fails})",
          f"({skipped} skipped: need AlcorMP data)" if skipped else "")
    return not fails
