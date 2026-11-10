"""Workflows: each reads as its steps. The CLI (cli.py) parses options and calls these."""
import contextlib
import hashlib
import json
import os
import sys
import time

from . import f700, provision
from .device import (BotError, CommandFailed, MpSession, Session, disk_geometry, drive, firmware_locked,
                     flash_id, read, read_code, read_disk, reopen_on_port, usb_port, wait_for_rom_mode,
                     write_accepted, write_and_verify, write_disk)
from .nand import Geometry, boot_fw_image, device_bootfw, hex_decode, identify_boot_fw
from .payloads import (build_wp_block, config_serial, config_valid, decode_record, default_config, HK_STATUS,
                       hk_decode, nand_address, onfi_geometry, own_loader_image, record_capacity, usb_string)
from .protocol import (COMMANDS, ONFI, PROBE_SIGNATURE, READS, code_cdb, probe_cdb, probe_nand_cdb,
                       xdata_page_cdb)

DUMPS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "firmware", "dumps")

ROM_MODE_HELP = """\
The stick's firmware is locked: it refuses every FA vendor command, so it cannot be
formatted from here. Force the controller into ROM mode: if it cannot read the NAND at
power-on it skips the firmware and enumerates as 058f:1234, from which the format runs.

  1. Unplug the stick and open the case.
  2. Short two adjacent NAND data pins (TSOP48: pins 29+30, I/O0-I/O1) with tweezers
     and keep holding them.
  3. Plug the stick into the same port, wait ~2 s, then release the short.
"""


def confirm(question, yes=False):
    if yes:
        return
    if not sys.stdin.isatty():
        raise SystemExit(f"{question} -- not confirmed (no terminal); pass -y to proceed")
    if input(f"{question} [y/N] ").strip().lower() not in ("y", "yes"):
        raise SystemExit("aborted; nothing was written")


# == STATE ==
def state(s):
    """What the stick reports, read-only: {locked, chip, flash_id, serial, capacity, wp,
    record, mp_info, config}; fields a locked firmware refuses are None."""
    def attempt(name):
        try:
            return read(s, name)
        except (BotError, OSError):
            return None
    st = {"locked": firmware_locked(s)}
    ident, cfg, rec = attempt("ident"), attempt("config"), attempt("record")
    st["mp_info"] = attempt("sysconfig")
    st["chip"] = (ident[4] << 8) | ident[5] if ident and len(ident) > 5 else None
    st["wp"] = ident[0x10A] if ident and len(ident) > 0x10A else None
    st["flash_id"] = None if st["locked"] else flash_id(s, st["chip"])
    st["config"] = cfg if cfg and config_valid(cfg) else None
    st["serial"] = config_serial(cfg) if st["config"] else None
    st["record"] = rec
    try:
        st["capacity"] = disk_geometry(s)[0]
    except (BotError, OSError):
        st["capacity"] = None
    return st


def running_boot_fw(s):
    """(code window, bundled _CB image or None, match fraction)."""
    code = read_code(s, f700.BOOT_FW_WINDOW[1], f700.BOOT_FW_WINDOW[0])
    name, frac = identify_boot_fw(code)
    return code, (name if frac >= 0.9 else None), frac


def record_summary(rec):
    cap, d = record_capacity(rec or b""), decode_record(rec or b"")
    if cap is None or d is None:
        return "empty: the next power-on comes up locked"
    wp = d[f"bit {f700.RECORD_FLAGS[f700.RECORD_WP][0]}"] & f700.WP_POLICY
    return (f"capacity {cap} sectors ({cap * 512 / 1e9:.2f} GB), lanes {'/'.join(map(str, d['lanes']))}, "
            f"WP {'on' if wp else 'off'}, lock gate {'SET' if d['bit 65'] else 'clear'}")


def restart(s, port):
    """USB bus reset from session `s`, which it closes: the firmware restarts from NAND and
    mounts its info record. A new session on the same port, or None."""
    for step in (s.reset, s.close):
        try:
            step()
        except (BotError, OSError):
            pass
    time.sleep(2)
    return reopen_on_port(port, timeout=60)                # the kernel's retries of a bad LBA 0 take a while


# == BACKUP / RESTORE ==
def backup(s, root=DUMPS_DIR, data=False, log=print):
    """Read-only backup into root/<flash id>_<serial>_<time>/: every READS reply, the CODE
    space, manifest.json naming the boot image it runs, with `data` the whole disk."""
    replies = {}
    for name in READS:
        try:
            replies[name] = read(s, name)
        except (BotError, OSError) as e:
            log(f"  backup: {name} failed ({e})")
    code = read_code(s)
    ident, cfg = replies.get("ident", b""), replies.get("config", b"")
    chip = (ident[4] << 8) | ident[5] if len(ident) > 5 else None
    fid = replies.get("flashid8" if chip and chip >= 0xF500 else "flashid", b"")[:6]
    serial = usb_string(cfg[0x80:]) if cfg[0x7E:0x80] == b"\x3c\xc3" else None
    win = code[f700.BOOT_FW_WINDOW[0]:sum(f700.BOOT_FW_WINDOW)]
    image, frac = identify_boot_fw(win)
    inq = replies.get("inquiry", b"")
    manifest = {
        "time": time.strftime("%Y-%m-%dT%H:%M:%S"), "chip": f"{chip:04X}" if chip is not None else None,
        "flash_id": fid.hex(" "), "serial": serial,
        "firmware_rev": inq[32:36].decode(errors="replace").strip() if len(inq) >= 36 else None,
        "boot_fw": {"bundled_image": image if frac >= 0.9 else None, "match": round(frac, 3),
                    "window_sha1": hashlib.sha1(win).hexdigest()},
        "files": ["code.bin"] + [f"replies/{n}.bin" for n in replies],
    }
    out = os.path.join(root, f"{fid.hex()}_{serial or 'noserial'}_{time.strftime('%Y%m%d-%H%M%S')}")
    os.makedirs(os.path.join(out, "replies"))
    for name, reply in replies.items():
        with open(os.path.join(out, "replies", f"{name}.bin"), "wb") as f:
            f.write(reply)
    with open(os.path.join(out, "code.bin"), "wb") as f:
        f.write(code)
    if data:
        sectors, bs, sha = read_disk(s, os.path.join(out, "disk.img"))
        manifest["disk"] = {"file": "disk.img", "sectors": sectors, "sector_size": bs, "sha256": sha}
        manifest["files"].append("disk.img")
    with open(os.path.join(out, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    log(f"backup: {out}  (boot fw {manifest['boot_fw']['bundled_image'] or 'not a bundled image'})")
    return out


def load_dump(path):
    """(manifest, identity config block) of a backup directory."""
    manifest = json.load(open(os.path.join(path, "manifest.json")))
    return manifest, open(os.path.join(path, "replies", "config.bin"), "rb").read()


def write_identity(s, new, yes=False, current=None):
    """Back up, show the change, confirm, write the config block and read it back."""
    current = current if current is not None else read(s, "config")
    diffs = [i for i in range(512) if current[i] != new[i]]
    print(f"{len(diffs)} of 512 bytes change" + (": " + ", ".join(f"@{i:#05x} {current[i]:02x}->{new[i]:02x}"
                                                                for i in diffs[:12]) if diffs else ""))
    if not diffs:
        return
    confirm("write the new config block?", yes)
    backup(s)
    status, reply = write_and_verify(s, "write_config", new, "config", lambda rb: rb == new)
    print({"verified": "written and read back identical",
           "pending-replug": "written; the stick re-enumerated: replug and run `alcortool info`",
           "mismatch": "written, but the read-back differs (the controller may normalize some bytes)",
           "failed": "the controller rejected the write", "unknown": "outcome unknown: replug and check"}[status])


def restore(s, path, identity=True, data=True, yes=False):
    """A backup's identity block and/or disk image onto the stick, without a format."""
    manifest, cfg = load_dump(path)
    if data:
        if "disk" not in manifest:
            raise SystemExit(f"{path} has no disk image (back up with --data)")
        confirm(f"overwrite every sector of the disk with {path}?", yes)
        write_disk(s, os.path.join(path, manifest["disk"]["file"]))
    if identity:
        write_identity(s, cfg, yes=True)


# == WRITE-PROTECT ==
def set_write_protect(s, port, on, yes=False, dry_run=False):
    """Policy write-protect on or off, keeping the data: check -> confirm -> backup -> 9B
    (RAM) -> 96 (saves the record) -> bus reset -> the record, the flag and a sector write
    agree. The reset closes `s`. Returns True when verified, None when not sent or unknown."""
    want = "on" if on else "off"
    st = state(s)
    if st["locked"]:
        raise SystemExit("the firmware is locked (vendor commands refused, not just write-protected): "
                         "only `alcortool format` from ROM mode recovers it")
    if st["wp"] is None or decode_record(st["record"] or b"") is None:
        raise SystemExit(f"identify or the info record is unreadable (record: {record_summary(st['record'])}); "
                         "nothing sent")
    if st["wp"] & f700.WP_SWITCH:
        print("note    : the hardware write-protect switch is on; it overrides the policy flag")
    print(f"stick   : write-protect {'on' if st['wp'] & f700.WP_POLICY else 'off'}; "
          f"info record {record_summary(st['record'])}")
    if bool(st["wp"] & f700.WP_POLICY) == on and bool(st["record"][f700.RECORD_WP] & f700.WP_POLICY) == on:
        print(f"write-protect is already {want}; nothing to do")
        return True
    if dry_run:
        print(f"dry run: would send 9B (write-protect {want}), 96 twice, then a USB reset")
        return None
    confirm(f"turn write-protect {want} (rewrites the info record block; the data stays)?", yes)
    backup(s)
    before = st["record"]
    s.write(COMMANDS["set_wp"].cdb, build_wp_block(on))
    s.cmd("save_record")                                    # AlcorMP sends it twice
    s.cmd("save_record")
    s = restart(s, port)
    if s is None:
        print("verify  : the stick did not come back; replug it and run `alcortool info`")
        return None
    with s:
        st = state(s)
        rec = st["record"] or b""
        changed = [i for i in range(len(rec)) if i < len(before) and rec[i] != before[i]]
        written = write_accepted(s, disk_geometry(s)[0] - 1)
    ok = (bool((st["wp"] or 0) & f700.WP_POLICY) == on and decode_record(rec) is not None and not st["locked"]
          and bool(rec[f700.RECORD_WP] & f700.WP_POLICY) == on and written != on
          and set(changed) <= {f700.RECORD_WP, len(rec) - 1})  # 0x1FF holds INTMEM 9A itself
    print(f"verify  : info record {record_summary(rec)}; bytes changed "
          f"{' '.join(hex(i) for i in changed) or 'none'}; a sector write {'accepted' if written else 'refused'}")
    print(f"write-protect {want}" if ok else "WARNING: not the expected state; see `alcortool -v info`")
    return ok


# == FORMAT ==
def sweep_options(bad_blocks="quick", erase="blocks", verify="none", fill="full", skip=()):
    """(sweeps, skip groups) for generate() from the format options."""
    sweeps = "full" if bad_blocks == "full" else "erase" if erase == "blocks" else "none"
    skip = (list(skip) + (["screen"] if bad_blocks in ("none", "keep") else [])
            + (["hk"] if bad_blocks == "none" else []) + (["verify", "polls"] if verify == "none" else [])
            + (["fill"] if fill == "minimal" else []))
    return sweeps, skip


def chip_erase(mp):
    with mp.s.timeout_ms(180000):
        mp.s.cmd("chip_erase")


def read_housekeeping(mp):
    """AlcorMP's housekeeping tables ({type: plaintext}), or None when they do not decode.
    Read under the housekeeping loader, at the stage the format writes them."""
    with mp.loader("hlf", **provision.HOUSEKEEPING_STAGE):
        pages = [mp.nand(0x22, f700.HK_BLOCK, pg, region=f700.R_HK)[0]
                 for pg in range(provision.housekeeping_pages(mp.geo))]
    return hk_decode(mp.geo, pages)


def format_stick(dev, fid=None, *, bad_blocks="quick", erase="blocks", verify="none", fill="full",
                 skip=(), capacity=None, do_backup=True, restore_dump=None, rom_wait=600, yes=False,
                 dry_run=False, max_failures=200, port=None, verbose=0):
    """Low-level format: inspect -> confirm -> (ROM mode if locked) -> backup -> stream
    (chip erase, screen, FTL build, sweeps, tables, firmware, closing) -> verify."""
    sweeps, skip = sweep_options(bad_blocks, erase, verify, fill, skip)
    live_port = usb_port(dev) if dev else None
    if port and live_port != port:
        raise SystemExit(f"/dev/{dev} is on port {live_port}, not {port}; refusing")
    st = None
    if dev:
        with Session(dev) as s:
            st = state(s)
        fid = fid or st["flash_id"]
    if fid is None and not (st and st["locked"]):
        raise SystemExit("no flash ID: give -d DEVICE or --flash-id")
    geo = Geometry(fid) if fid else None
    if geo:
        print(geo.describe())
    if st:
        print(f"stick   : /dev/{dev} on port {live_port}, serial {st['serial'] or '-'}, "
              + ("LOCKED (ROM mode needed)" if st["locked"] else f"{(st['capacity'] or 0) * 512 / 1e9:.2f} GB"))
    print(f"format  : bad blocks {bad_blocks}, sweeps {sweeps}, skip {','.join(skip) or '-'}, "
          f"capacity {capacity or 'every usable block'}" + (f", restore {restore_dump}" if restore_dump else ""))
    if dry_run or not dev:
        if geo is None:
            raise SystemExit("the locked firmware refuses the flash ID read: give --flash-id")
        ops = provision.generate(geo, sweeps=sweeps, skip=skip, capacity_limit=None if capacity == "keep" else capacity)
        print_plan(ops)
        print("dry run: nothing sent")
        return None
    confirm(f"ERASE /dev/{dev} and format it? (all data is lost)", yes)

    if st["locked"]:
        print(ROM_MODE_HELP, flush=True)
        if not rom_wait:
            raise SystemExit("locked firmware; rerun with --rom-wait SECONDS to wait for ROM mode")
        print(f"waiting up to {rom_wait} s for {f700.ROM_MODE_ID} on port {live_port} (Ctrl-C to abort)...", flush=True)
        dev = wait_for_rom_mode(live_port, rom_wait)
        if dev is None:
            raise SystemExit("the stick did not come back in ROM mode; nothing was written")
        print(f"ROM mode on /dev/{dev}", flush=True)
        if geo is None:
            with Session(dev) as s:
                geo = Geometry(flash_id(s))
            print(geo.describe())

    s = Session(dev, verbose=verbose)
    mp = live = kept = None
    try:
        if capacity == "keep":
            capacity = disk_geometry(s)[0]
            print(f"capacity: keeping {capacity} sectors ({capacity * 512 / 1e9:.2f} GB)")
        identity = load_dump(backup(s))[1] if do_backup else read(s, "config")
        if not config_valid(identity):
            identity = default_config()
            print(f"no valid config block: writing a generated one (serial {config_serial(identity)})")
        if restore_dump:
            manifest, identity = load_dump(restore_dump)
            if manifest["boot_fw"]["bundled_image"] != boot_fw_image(geo.fid):
                raise SystemExit(f"{restore_dump} ran boot image {manifest['boot_fw']['bundled_image']}; "
                                 f"this format writes {boot_fw_image(geo.fid)}")
        if bad_blocks == "keep":
            mp = MpSession(s, geo).enable()
            tables = read_housekeeping(mp)
            if tables is None:
                raise SystemExit("--bad-blocks keep: no readable table in the housekeeping block; "
                                 "nothing was erased. Use --bad-blocks quick or full")
            kept = tables[HK_STATUS]
            bad = [b for b in range(geo.blocks) if kept[b] > f700.SWEEP_FAILED]
            print(f"kept bad-block table: {len(bad)} bad blocks {' '.join(hex(b) for b in bad[:16])}")
            print("chip erase (minutes)...", flush=True)
            chip_erase(mp)
            mp.enable()
            live = s
        ops = provision.generate(geo, with_enable=live is None, identity=identity, sweeps=sweeps, skip=skip,
                                 capacity_limit=capacity, kept_status=kept)
    except BaseException:
        s.close()
        raise
    if live is None:
        s.close()
    print(f"stream  : {len(ops)} ops (about an hour with full sweeps); run detached (setsid) "
          "so a closed terminal does not stop it half-way", flush=True)
    sent, failures = drive(dev, live_port, ops, session=live, max_failures=max_failures, verbose=verbose)
    result = verify_format(live_port)
    if restore_dump and failures == 0 and "disk" in load_dump(restore_dump)[0]:
        s = reopen_on_port(live_port)
        if s is None:
            raise SystemExit(f"stick did not come back; restore the disk with: alcortool restore {restore_dump} --data")
        with s:
            manifest = load_dump(restore_dump)[0]
            write_disk(s, os.path.join(restore_dump, manifest["disk"]["file"]), skip_zero=True)
    return sent, failures, result


def verify_format(port):
    """After the closing reset: the record is saved, WP is off, the FA set is open."""
    s = reopen_on_port(port)
    if s is None:
        print("verify  : the stick did not come back; replug it and run `alcortool info`")
        return None
    with s:
        st = state(s)
    ok = (record_capacity(st["record"] or b"") is not None and not st["locked"]
          and not (st["wp"] or 0) & (f700.WP_POLICY | f700.WP_SWITCH))
    print(f"verify  : info record {record_summary(st['record'])}; FA {'refused' if st['locked'] else 'open'}; "
          f"WP byte {st['wp']:#04x}" if st["wp"] is not None else "verify  : identify failed")
    print("format OK: replug the stick to use it" if ok else "WARNING: the stick will not mount as formatted")
    return ok


def print_plan(ops):
    """The stream's phases with op counts and the loaders each uploads."""
    print(f"stream  : {len(ops)} ops")
    i = 0
    for phase, n in provision.phases(ops):
        lds = dict.fromkeys(o.note.split()[1] for o in ops[i:i + n] if o.note.startswith("loader "))
        print(f"  {phase:22} {n:6} ops   loaders {', '.join(lds) or '-'}")
        i += n


def byte_diff(data, base):
    """'0x5d=00 0x90-0x9f=0036.. 0x2a1-0x39f (255 B)': where `data` differs from `base`."""
    runs, i = [], 0
    while i < len(data):
        if data[i] == base[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(data) and data[j + 1] != base[j + 1]:
            j += 1
        val = data[i:j + 1].hex() if j - i < 16 else f"({j - i + 1} B)"
        runs.append(f"{i:#x}={val}" if i == j else f"{i:#x}-{j:#x}={val}")
        i = j + 1
    return " ".join(runs) or "(base)"


def print_params(ops, geo):
    """Every distinct param block and loader patch the stream sends, as its difference from
    the base block (no stage, B member) and the MP-enable patch, with where it is sent."""
    from .payloads import build_loader_patch, build_param
    from .protocol import Live
    for title, sel, base, cut in (
            ("param blocks (FA 0B 01) vs the base block", b"\xfa\x0b\x01", build_param(geo), slice(None)),
            ("loader patches (FA 0A, 0x2000) vs the MP-enable patch", b"\xfa\x0a",
             build_loader_patch(geo), slice(f700.LOADER_PATCH_AT, f700.LOADER_PATCH_AT + f700.LOADER_PATCH_LEN))):
        seen = {}
        for o in ops:
            if o.cdb[:len(sel)] == sel:
                key = (o.data({})[cut], isinstance(o.payload, Live))
                seen.setdefault(key, []).append(o.phase)
        print(f"\n{title}: {sum(map(len, seen.values()))} sent, {len(seen)} distinct")
        for (data, live), where in seen.items():
            print(f"  x{len(where):<3} {', '.join(dict.fromkeys(where))}{'  [live columns]' if live else ''}\n"
                  f"       {byte_diff(data, base)}")


# == FIRMWARE-ONLY UPDATE ==
def read_fw_layout(mp, image, save=None):
    """Both system blocks' records, read in MP mode with the system-area frame and parsed;
    FwLayoutError on any mismatch."""
    blocks = {}
    for blk, lead in ((f700.SYS_BLOCK, True), (f700.MIRROR_BLOCK, False)):
        pages = {}
        for role, pg in provision.fw_layout_pages(lead).items():
            sectors, region, frame = f700.FW_READ
            data, _ = mp.nand(0x46, blk, pg, sectors, region, frame=frame)
            pages[role] = data[:sectors * 512]
            if save:
                with open(os.path.join(save, f"b{blk:03x}_p{pg:02x}_{role}.bin"), "wb") as f:
                    f.write(pages[role])
        blocks[blk] = provision.parse_fw_block(mp.geo, blk, lead, pages, image)
    if blocks[f700.SYS_BLOCK]["bitmaps"] != blocks[f700.MIRROR_BLOCK]["bitmaps"]:
        raise provision.FwLayoutError("block 0 and the mirror carry different lane bitmaps")
    return blocks


def load_fw_image(path, fid):
    """(image, name): AlcorMP hex-text .BIN or raw bytes; None = the bundled image."""
    if path is None:
        return device_bootfw(fid), boot_fw_image(fid)
    raw = open(path, "rb").read()
    text = raw.strip()
    hexy = text and all(c in b"0123456789abcdefABCDEF \t\r\n" for c in text)
    return (hex_decode(raw) if hexy else raw), os.path.basename(path)


def update_firmware(dev, image_path=None, capacity=None, blind=False, yes=False, dry_run=False,
                    max_failures=50, verbose=0):
    """Rewrite the two system blocks with a boot image, keeping the FTL and the data:
    read back and validate both blocks -> confirm -> erase and rewrite -> closing."""
    port = usb_port(dev)
    s = Session(dev, verbose=verbose)
    try:
        if firmware_locked(s):
            raise SystemExit("the firmware refuses vendor commands (locked); a firmware update needs "
                             "a working stick: use `alcortool format` from ROM mode")
        geo = Geometry(flash_id(s))
        image, name = load_fw_image(image_path, geo.fid)
        nfw = -(-len(image) // 0x1000)
        if not image or image[0] != 0x02:
            raise SystemExit(f"{name}: not an 8051 boot image (expected LJMP 02 at offset 0)")
        if 2 + nfw + 3 > f700.MIRROR_ANCHOR_PAGE:
            raise SystemExit(f"{name}: {nfw} firmware blocks overrun the mirror anchor page")
        if blind:
            sectors, identity, mp_info, dump = None, default_config(), None, None
        else:
            sectors, identity, mp_info = disk_geometry(s)[0], read(s, "config"), read(s, "sysconfig")
            if not config_valid(identity):
                raise SystemExit("config block is not 99 07; refusing (the update writes it back)")
            dump = backup(s)
        mp = MpSession(s, geo).enable()
        try:
            blocks = None if blind else read_fw_layout(mp, image, save=dump)
        except (provision.FwLayoutError, BotError, OSError) as e:
            raise SystemExit(f"read-back failed, nothing was erased: {e}\nreplug the stick to leave MP mode")
        print(f"image   : {name} ({len(image)} B, {nfw} firmware blocks)")
        for blk, b in (blocks or {}).items():
            print(f"block {blk:#05x}: data superblock {b['ds']:#x}, spares {' '.join(hex(x) for x in b['spares'])}, "
                  f"{b['nfw']} firmware blocks now, chunks 0-1 {b['image_match']:.0%} identical to the new image")
        new = sectors if capacity in (None, "keep") else capacity
        if blind:
            print(f"BLIND: no read-back or backup; clean-die layout, capacity "
                  f"{'every usable block' if new is None else f'{new} sectors'}, generated identity "
                  f"(serial {config_serial(identity)})")
        elif new != sectors:
            print(f"capacity: {sectors} -> {new} sectors ({new * 512 / 1e9:.2f} GB)"
                  + ("; WARNING: shrinking, data above the new size becomes unreachable" if new < sectors else ""))
        if dry_run:
            print("dry run: read-back validated, nothing erased. The controller is in MP mode: replug the stick.")
            return None
        confirm(f"erase and rewrite system blocks {f700.SYS_BLOCK:#x} and {f700.MIRROR_BLOCK:#x}?", yes)
        layout = {"blocks": blocks, "image": image, "identity": identity, "boot": mp_info, "capacity": new}
        ops = provision.generate(geo, with_enable=False, fw_update=layout)
    except BaseException:
        s.close()
        raise
    sent, failures = drive(dev, port, ops, session=s, max_failures=max_failures, verbose=verbose)
    verify_format(port)
    return sent, failures


# == SINGLE STEPS (dev) ==
def test_pattern(block, page, size):
    out, i = bytearray(), 0
    while len(out) < size:
        out += hashlib.sha256(f"alcortool {block:#x}/{page:#x}/{i}".encode()).digest()
        i += 1
    return bytes(out[:size])


def write_test(s, geo, block=f700.SCRATCH_BLOCK, pages=1, ppb=None, keep=False, backup_file=None, yes=False):
    """Native NAND write test on one block, AlcorMP's scratch recipe: blank check (or
    back the block up), erase, program, read back, erase again."""
    ppb, nsect = ppb or geo.pages_per_block, geo.sectors_per_page
    size = nsect * 512
    mp = MpSession(s, geo, strict=False).enable()
    blank, saved = True, []
    for p in range(ppb):
        data, verdict = mp.nand(0x19, block, p, nsect, frame=0)
        data = (data + bytes(size + 1024))[:size + 1024]
        saved.append(data)
        if verdict != "ok" or data[:size] != b"\xff" * size:
            blank = False
            if not backup_file:
                raise SystemExit(f"block {block:#x} page {p:#x} is not blank; give --backup FILE to back it "
                                 "up and erase it anyway (nothing written; replug before use)")
    if not blank:
        with open(backup_file, "wb") as f:
            f.write(b"".join(d[:size] for d in saved))
        with open(backup_file + ".tails", "wb") as f:
            f.write(b"".join(d[size:] for d in saved))
        print(f"block backed up ({ppb} pages) to {backup_file} (+ .tails)")
    confirm(f"erase and program block {block:#x}?", yes)
    err = mp.erase(block)
    if err:
        raise SystemExit(f"{err}; replug before use")
    bad = 0
    for p in range(pages):
        want = test_pattern(block, p, size)
        _, wv = mp.nand(0x1E, block, p, nsect, f700.R_SCRATCH, data=want)
        got, _ = mp.nand(0x1F, block, p, nsect, f700.R_SCRATCH)
        diff = sum(a != b for a, b in zip(want, got[:size])) + max(0, size - len(got))
        tail = got[size:size + 2]
        ok = wv == "ok" and diff == 0 and tail[:1] == b"\x40"
        bad += not ok
        print(f"  page {p:#x}: program {wv}, read-back {'identical' if diff == 0 else f'{diff} bytes differ'}, "
              f"tail {tail.hex(' ')}{'' if ok else '  <-- FAIL'}")
    print(f"program/verify: {pages - bad}/{pages} pages OK")
    if not keep:
        err = mp.erase(block)
        print(f"cleanup: {err}" if err else "cleanup: block erased again")
    print("the controller is in MP mode: replug before normal use")
    return bad == 0


def read_flash(s, geo, out, block, blocks=1, page=0, pages=1, sectors=None, sub=0x19, region=None,
               frame=None, loader=None, spare=False, verbose_pages=False):
    """NAND pages in MP mode (flash read-only; the enable is RAM only). A page decodes
    only under the param it was written with (docs/processes.md, reading the NAND): `loader` = the
    MpSession.loader arguments of the re-enable to read under, or None for the enable's."""
    sectors = sectors or geo.sectors_per_page
    mp = MpSession(s, geo, strict=False).enable()
    counts = {"ok": 0, "ecc-fail": 0, "error": 0}
    ctx = mp.loader(**loader) if loader else None
    if ctx:
        ctx.__enter__()
    try:
        with open(out, "wb") as fo, open(out + ".map", "w") as log, \
                (open(out + ".spare", "wb") if spare else open(os.devnull, "wb")) as fs:
            log.write("# block\tpage\tresult\tstatus_tail_hex16\n")
            for b in range(block, block + blocks):
                for p in range(page, page + pages):
                    try:
                        data, verdict = mp.nand(sub, b, p, sectors, region or b"\0\0\0\0", frame=frame)
                    except CommandFailed as e:
                        data, verdict = b"", f"error ({e})"
                    data = (data + bytes(sectors * 512 + 1024))[:sectors * 512 + 1024]
                    tail = data[sectors * 512:]
                    result = ("error" if verdict != "ok" else
                              "ok" if tail[0] == 0x40 or tail[1] == 0xC0 else "ecc-fail")
                    counts[result] += 1
                    fo.write(data[:sectors * 512])
                    fs.write(tail)
                    log.write(f"{b:#x}\t{p:#x}\t{result if verdict == 'ok' else verdict}\t{tail[:16].hex()}\n")
                    if verbose_pages or result != "ok":
                        print(f"  block {b:#06x} page {p:#06x}: {result}  tail {tail[:8].hex(' ')}")
    finally:
        if ctx:
            ctx.__exit__(None, None, None)
    print(f"done: {counts['ok']} ok, {counts['ecc-fail']} ECC-fail, {counts['error']} error -> {out} (+ .map)")
    print("the controller is in MP mode: replug before normal use")


@contextlib.contextmanager
def own_loader(s, port, pages=(), log=print):
    """Run the tool's probe loader (alcor/loaders/probe.a51, no AlcorMP code) on session `s`:
    read the stick's own NAND set-up (FA 13) for the patch window, upload, check that it is
    resident and answers, yield {"iram": internal RAM, "before": {page: XDATA read before}}.
    On leaving: a USB bus reset from the session, which restarts the firmware from NAND (an
    FA 08 does not: docs/processes.md), then ["after"] = the stick's state."""
    want = {*pages, *(a >> 8 for a in f700.PATCH_FROM_STICK.values())}
    before = {p: s.read(xdata_page_cdb(p), 512) for p in want}
    image = own_loader_image("probe", {(p << 8) + i: b for p, data in before.items() for i, b in enumerate(data)})
    ctx = {"before": before}
    try:
        s.cmd("mp_write")
        s.cmd("loader", image)
        if s.read(code_cdb(f700.LOADER_BASE, 1), 512) != image[:512]:
            raise BotError(f"CODE {f700.LOADER_BASE:#06x} does not read back as the upload")
        ident = s.read(probe_cdb("ident"), 512)
        if not ident.startswith(PROBE_SIGNATURE):
            raise BotError(f"no probe signature in the E0 reply: {ident[:32].hex(' ')}")
        log(f"loader  : {PROBE_SIGNATURE.decode()} resident at {f700.LOADER_BASE:#06x}")
        ctx["iram"] = ident[0x100:0x200]
        yield ctx
    finally:
        s2 = restart(s, port)                               # reset first: nothing may read through the probe
        if s2 is None:
            log("reset   : the stick did not come back; replug it")
        else:
            with s2:
                ctx["after"] = st = state(s2)
            log(f"reset   : USB bus reset, firmware restarted; info record {record_summary(st['record'])}, "
                f"FA {'refused' if st['locked'] else 'open'}")


def probe_loader(s, port, xwrites=(), xreads=(), pages=(), log=print):
    """The probe loader's XDATA steps: write bytes, read ranges, and the register pages
    before (FA 13) and after the upload. xwrites: (address, byte); xreads: (address, sectors).
    Returns {"iram", (address, sectors): XDATA, "pages": {page: (before, after)}, "after"}."""
    out = {"pages": {}}
    with own_loader(s, port, pages, log) as ctx:
        out["iram"] = ctx["iram"]
        for addr, value in xwrites:
            s.read(probe_cdb("xwrite", addr, value), 0)
            log(f"xwrite  : XDATA {addr:#06x} = {value:#04x}")
        for addr, n in xreads:
            out[addr, n] = s.read(probe_cdb("xread", addr, sectors=n), n * 512)
        for p in pages:
            out["pages"][p] = ctx["before"][p], s.read(probe_cdb("xread", p << 8, sectors=1), 512)
    out["after"] = ctx.get("after")
    return out


# == NAND THROUGH THE PROBE LOADER ==
def nand_identify(s, ce=0):
    """READ ID, the ONFI signature and the parameter page: {id, onfi, geometry}."""
    cmd = lambda c, a, n, wait=False: s.read(probe_nand_cdb("nandcmd", ce, bytes((c, a)), n, wait), 512)[:n]
    ident = cmd(ONFI["read_id"], 0x00, 8)
    sig = cmd(ONFI["read_id"], 0x20, 4)
    geo = onfi_geometry(cmd(ONFI["param_page"], 0x00, 512, wait=True)) if sig == b"ONFI" else None
    return {"id": ident, "onfi": sig == b"ONFI", "geometry": geo}


def nand_read_page(s, geo, block, page, ce=0, slc=False):
    """One raw page as stored (data, ECC parity, spare; no correction), byte by byte.
    slc: read in the NAND's SLC mode (Micron DAh), as the system blocks are written."""
    size = geo["page_bytes"] + geo["spare_bytes"]
    sectors = -(-size // 512)
    cdb = probe_nand_cdb("read", ce, nand_address(geo, block, page), sectors, prefix=ONFI["slc"] if slc else 0)
    with s.timeout_ms(60000):
        return s.read(cdb, sectors * 512)[:size]


def strip_parity(raw, frame):
    """The data of a raw page laid out in ECC frames of `frame` bytes (f700.ECC_FRAMES)."""
    data = f700.ECC_FRAMES[frame]
    return b"".join(raw[o:o + data] for o in range(0, len(raw) - frame + 1, frame))


def nand_erase_block(s, geo, block, ce=0):
    """Block erase; the NAND status byte (bit 0 = failed, bit 6 = ready)."""
    row = nand_address(geo, block, 0)[geo["col_cycles"]:]
    return s.read(probe_nand_cdb("erase", ce, row), 512)[0]


def nand_survey(s, geo, resident=None):
    """What block 0 holds, read raw in SLC mode (docs/controllers/F700.md, block 0x000 page map):
    {page: (what, detail)}. resident: CODE 0xC000-0xCFFF of the running firmware."""
    out = {}
    for page in (0, 1, 2, 3, 4, 0x0D):
        data = strip_parity(nand_read_page(s, geo, 0, page, slc=True), f700.ECC_ROW_512)
        if page == 0:
            ok = data[:5] == f700.ANCHOR_MAGIC
            out[page] = ("FTL anchor" if ok else "no anchor", f"attribute byte 0x2E = {data[0x2E]:#04x}" if ok else data[:8].hex(" "))
        elif page == 2:
            same = resident is not None and data[:len(resident)] == resident
            out[page] = ("boot firmware page 0", f"starts {data[:6].hex(' ')}" +
                         (", = the running firmware" if same else ", differs from the running firmware" if resident else ""))
        elif page == 3:
            out[page] = ("boot firmware overlay 0" if data[0xFFE:0x1000] == b"\x55\xaa" else "no overlay signature",
                         f"index {data[0xFFC]:#04x}, signature {data[0xFFE:0x1000].hex(' ')}")
        elif page == 4:
            spread = len(set(data[:4096]))
            out[page] = ("overlay 1, stored scrambled" if spread > 240 else "overlay 1", f"{spread} distinct byte values in 4 KB")
        elif page == 0x0D:
            out[page] = ("ECC parameter record" if data[:3] == bytes(f700.ECC_FAMILY) else "unexpected",
                         f"key {data[:4].hex(' ')} (t = {data[3]})")
        else:
            out[page] = ("system record", f"starts {data[:8].hex(' ')}")
    return out


def nand_session(s, port, log=print):
    """own_loader plus identification; refuses a NAND without an ONFI parameter page."""
    @contextlib.contextmanager
    def run():
        with own_loader(s, port, log=log) as ctx:
            ctx.update(nand_identify(s))
            log(f"nand    : ID {ctx['id'].hex(' ')}" + (f", ONFI {ctx['geometry']['manufacturer']} "
                                                        f"{ctx['geometry']['model']}" if ctx["geometry"] else ""))
            if not ctx["geometry"]:
                raise BotError("the NAND has no valid ONFI parameter page: its geometry is unknown")
            yield ctx
    return run()


def save_record(s, yes=False):
    """96: the firmware serializes its info record from RAM and saves it to NAND."""
    confirm("save the firmware's info record (writes a fresh record block)?", yes)
    s.cmd("save_record")
    s.cmd("save_record")
    return read(s, "record")

