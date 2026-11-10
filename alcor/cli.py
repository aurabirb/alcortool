"""alcortool: identify, back up and low-level format Alcor USB flash controllers.

Talks to the controller over USB Bulk-Only (usbfs, no root with the uaccess udev rule)
with the vendor commands AlcorMP uses. Reference: docs/.
"""
import argparse
import os
import signal
import sys

from . import f700, flows, provision
from .device import (BotError, CommandFailed, MpSession, Session, mounted_partitions, probe_controller, read,
                     read_code, reset_port, usb_disks, usb_ids, usb_port)
from .nand import DataError, Geometry
from .payloads import (HK_STATUS, build_config, config_checksum, config_fields, config_valid, decode_mp_info,
                       decode_record, decode_setup, hk_summary, ufd_mode)
from .protocol import COMMANDS, PROBE_SUBS, READS, SUBS, page_cdb, xdata_page_cdb

EPILOG = """\
getting started:
  alcortool extract AlcorMP_v20.rar    once per AlcorMP release: its tables and images (README.md)
  alcortool list                       the Alcor drives attached
  alcortool info                       what the stick is and its state
  alcortool backup --data              everything a restore needs, to firmware/dumps/
  alcortool unlock                     remove the write-protect, keeping the data (--lock sets it)
  alcortool format                     backup, erase, scan, provision, verify (destructive)
  alcortool help dev                   single steps, for a new chip or debugging
"""


def hexdump(data, width=16):
    out = []
    for i in range(0, len(data), width):
        chunk = data[i:i + width]
        out.append(f"{i:04x}  {chunk.hex(' '):<{width * 3}} {''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)}")
    while len(out) > 1 and not out[-1][6:6 + width * 3].replace("00", "").strip():
        out.pop()                                          # trailing zero lines
    return "\n".join(out)


def read_xdata(s, addr):
    """512 B of XDATA from `addr` (page-aligned), read by the ROM in normal mode (FA 13)."""
    return s.read(xdata_page_cdb(addr >> 8), 512)


def num(x):
    return int(x, 0)


def hexbytes(n):
    def parse(x):
        b = bytes.fromhex(x.replace(" ", ""))
        if len(b) != n:
            raise argparse.ArgumentTypeError(f"need {n} bytes ({2 * n} hex digits)")
        return b
    return parse


def addr_count(text):
    addr, _, n = text.partition(":")
    return int(addr, 0), int(n or "1", 0)


def addr_byte(text):
    addr, sep, value = text.partition("=")
    if not sep or not 0 <= int(value, 0) <= 0xFF:
        raise argparse.ArgumentTypeError("need ADDR=BYTE")
    return int(addr, 0), int(value, 0)


def parse_capacity(text):
    """'keep', a sector count, or a size with a unit (GB/MB decimal, GiB/MiB binary)."""
    t = text.strip().lower()
    if t == "keep":
        return t
    for unit, mult in (("gib", 1 << 30), ("mib", 1 << 20), ("gb", 10**9), ("mb", 10**6)):
        if t.endswith(unit):
            return int(float(t[:-len(unit)]) * mult) // 512
    return int(t, 0)


# == DEVICE SELECTION ==
def find_alcor():
    """(drives, unprobed): every 058F disk, plus USB disks whose controller passes the
    read-only probe (rebranded sticks report their own vendor ID)."""
    found, unprobed = [], []
    for dev, vid, pid, product in usb_disks():
        if vid.lower() == f700.USB_VID:
            found.append((dev, vid, pid, product))
            continue
        if mounted_partitions(dev):
            unprobed.append(dev)
            continue
        try:
            with Session(dev) as s:
                if probe_controller(s)["ok"]:
                    found.append((dev, vid, pid, product))
        except (BotError, OSError):
            unprobed.append(dev)
    return found, unprobed


def pick_device(args):
    """The -d device, or the only Alcor drive; refuses a mounted or non-USB one."""
    if args.device:
        ids = usb_ids(args.device)
        if not ids:
            sys.exit(f"/dev/{args.device} is not a USB device; refusing")
        dev = args.device
    else:
        drives, unprobed = find_alcor()
        if not drives:
            sys.exit("no Alcor drive found" + (f" ({', '.join(unprobed)} not probed: mounted, or no permission)"
                                               if unprobed else "; is the stick plugged in?"))
        if len(drives) > 1:
            sys.exit(f"{len(drives)} Alcor drives attached ({', '.join(d[0] for d in drives)}): pick one with -d")
        dev, *ids = drives[0]
    mounted = mounted_partitions(dev)
    if mounted:
        sys.exit(f"{', '.join(mounted)} mounted; unmount first (udisksctl unmount -b {mounted[0]})")
    return dev, ids


def session(args):
    """An open session on the chosen device, probed when it is not a 058F device."""
    dev, ids = pick_device(args)
    port = usb_port(dev)                                   # sysfs path goes with usb-storage
    try:
        s = Session(dev, verbose=args.verbose)
    except PermissionError as e:
        sys.exit(f"permission denied opening {e.filename} (install the udev rule in README.md, or use sudo)")
    except FileNotFoundError as e:
        sys.exit(f"{e.filename} is missing: no usbfs device nodes (in a container, pass the USB device in)")
    if ids[0].lower() != f700.USB_VID and not probe_controller(s)["ok"]:
        s.close()
        sys.exit(f"/dev/{dev} [{ids[0]}:{ids[1]}] is not an Alcor controller; refusing")
    s.dev, s.ids, s.port = dev, ids, port
    return s


# == COMMANDS ==
def cmd_list(args):
    drives, unprobed = find_alcor()
    for dev in unprobed:
        print(f"note: /dev/{dev} not probed (mounted, or no permission)", file=sys.stderr)
    for dev, vid, pid, product in drives:
        print(f"/dev/{dev}  {vid}:{pid}  port {usb_port(dev)}  {product}")
    if not drives:
        print("no Alcor drives found")


def print_config(cfg):
    if not config_valid(cfg):
        print(f"Identity    : no config block (signature {cfg[:2].hex(' ')})")
        return
    c = config_fields(cfg)
    print(f"Identity    : {c['vid_pid']}  {c['usb'][0]!r} / {c['usb'][1]!r}  SCSI {' / '.join(map(repr, c['scsi']))}"
          f"  serial {c['serial']!r}" + ("" if c["checksum_ok"] and c["serial_ok"] else "  (checksum MISMATCH)"))


def print_record(rec):
    """Every mapped field of the info record, with its meaning."""
    if not decode_record(rec or b""):
        return
    print(f"  UFDMode {ufd_mode(rec):#04x} (bits 0-4 from the flags below)")
    for off, (tag, src, meaning) in f700.RECORD_VALUES.items():
        print(f"  {off:#06x}  {tag:02X} {src:5}  {int.from_bytes(rec[off:off + 4], 'big'):#010x}  {meaning}")
    for off, (bit, f5, meaning) in f700.RECORD_FLAGS.items():
        src = f"bit {bit}" + (f", F5[{f5:#04x}]" if f5 is not None else "")
        print(f"  {off:#06x}  {src:23}  {rec[off]:#04x}  {meaning}")
    lanes = [int.from_bytes(rec[f700.RECORD_LANES + 2 * i:f700.RECORD_LANES + 2 * i + 2], "big") for i in range(4)]
    print(f"  {f700.RECORD_LANES:#06x}  usable blocks per lane: {'/'.join(map(str, lanes))}")


def print_details(st):
    """info -v: the decoded config block, info record and MP info sector."""
    if st["config"] is not None:
        c = config_fields(st["config"])
        print(f"Config      : USB {c['bcd_usb']}, bcdDevice {c['bcd_device']}, max packet {c['max_packet']}, "
              f"sizes {c['sizes']}, 0x7C {c['7c']}, tag {c['tag']!r}")
    if decode_record(st["record"] or b""):
        print("Info record fields:")
        print_record(st["record"])
    mp = decode_mp_info(st["mp_info"] or b"")
    if mp is None:
        print("Last LLF    : no MP info sector (blank)")
    else:
        print(f"Last LLF    : {mp['date'] or 'no date'}, capacity {mp['capacity']} sectors "
              f"({mp['capacity'] * 512 / 1e9:.2f} GB), MP state block "
              + ("as AlcorMP's" if not mp["unknown"] else f"differs at {', '.join(hex(o) for o in mp['unknown'])}"))
    print("Housekeeping: AlcorMP's LLF host/user and bad-block table need MP mode: `alcortool dev housekeeping`")


def cmd_info(args):
    with session(args) as s:
        st = flows.state(s)
        print(f"Device      : /dev/{s.dev} [{s.ids[0]}:{s.ids[1]}] {s.ids[2]}, port {s.port or '?'}")
        if st["locked"]:
            print("State       : LOCKED firmware (FA vendor commands refused); `alcortool format` recovers "
                  "it via the ROM mode")
        try:
            inq = read(s, "inquiry")
            print(f"Inquiry     : {inq[8:16].decode(errors='replace').strip()} / "
                  f"{inq[16:32].decode(errors='replace').strip()} / rev {inq[32:36].decode(errors='replace').strip()}")
        except (BotError, OSError) as e:
            print(f"Inquiry     : failed ({e})")
        if st["capacity"]:
            print(f"Capacity    : {st['capacity'] * 512 / 1e9:.2f} GB ({st['capacity']} sectors)")
        if st["chip"] is not None:
            print(f"Controller  : chip {st['chip']:04X}" + (" (F700)" if st["chip"] == f700.CHIP_ID else ""))
        if st["wp"] is not None:
            print(f"Write prot. : hardware {'ON' if st['wp'] & f700.WP_SWITCH else 'off'}, policy "
                  f"{'ON' if st['wp'] & f700.WP_POLICY else 'off'} (9A byte 0x10A = {st['wp']:#04x})"
                  + ("; `alcortool unlock` clears it" if st["wp"] & f700.WP_POLICY else ""))
        if st["flash_id"]:
            try:
                g = Geometry(st["flash_id"])
                part = f"{g.vendor} {g.part}"
            except DataError:
                part = "(no AlcorMP record; `alcortool dev nand info` asks the chip itself)"
            print(f"Flash       : {st['flash_id'].hex(' ')}  {part}")
        if not st["locked"]:
            code, name, frac = flows.running_boot_fw(s)
            print(f"Boot fw     : " + (f"{name} ({frac:.0%} match)" if name else
                                       f"resident code starts {code[:6].hex(' ')} (no imported image to name it)"))
            try:
                setup = decode_setup(read_xdata(s, 0x7100))
                frame = f", {setup['ecc_frame']}-B data frames" if setup["ecc_frame"] else ""
                print(f"NAND set-up : {setup['planes']} planes (loop), {setup['sectors_per_page']} sectors/page, "
                      f"BCH t = {setup['ecc_t']}{frame}, mode {setup['mode']:#04x}")
                if args.verbose:
                    regs = read_xdata(s, 0x8000)
                    print("              raw " + " ".join(f"{a:04x}={v:02x}" for a, v in setup["raw"].items())
                          + "; NAND regs " + " ".join(f"{0x8000 + r:04x}={regs[r]:02x}" for r in (0x0A, 0x0B, 0x1A, 0x40)))
            except (BotError, OSError):
                pass
        if st["config"] is not None:
            print_config(st["config"])
        print(f"Info record : {flows.record_summary(st['record'])}")
        if args.verbose:
            print_details(st)


def cmd_backup(args):
    with session(args) as s:
        flows.backup(s, args.out, data=args.data)


def cmd_restore(args):
    if args.full:
        dev, _ = pick_device(args)
        flows.format_stick(dev, restore_dump=args.dump, yes=args.yes, verbose=args.verbose)
        return
    both = not (args.identity or args.data)
    with session(args) as s:
        flows.restore(s, args.dump, identity=args.identity or both, data=args.data or both, yes=args.yes)


def cmd_format(args):
    dev = None if args.dry_run and not args.device else pick_device(args)[0]
    flows.format_stick(dev, args.flash_id, bad_blocks=args.bad_blocks, erase=args.erase, verify=args.verify,
                       fill=args.fill, skip=args.skip, capacity=args.capacity, do_backup=not args.no_backup,
                       rom_wait=args.rom_wait, yes=args.yes, dry_run=args.dry_run,
                       max_failures=args.max_failures, port=args.port, verbose=args.verbose)


def cmd_firmware(args):
    dev, _ = pick_device(args)
    flows.update_firmware(dev, args.image, args.capacity, args.blind, args.yes, args.dry_run, verbose=args.verbose)


def cmd_identity(args):
    with session(args) as s:
        cur = read(s, "config")
        print_config(cur)
        if args.save:
            with open(args.save, "wb") as f:
                f.write(cur)
            print(f"saved to {args.save}")
        if args.load:
            new = open(args.load, "rb").read()
            if not config_valid(new):
                sys.exit(f"{args.load} is not a 512-B 99 07 config block")
            ck, stored, computed = config_checksum(new)
            if stored != computed:
                sys.exit(f"{args.load}: checksum at {ck:#x} is {stored:#04x}, should be {computed:#04x}")
        elif args.vendor is not None or args.product is not None or args.serial is not None:
            try:
                new = build_config(cur, args.vendor, args.product, args.serial)
            except ValueError as e:
                sys.exit(f"cannot build the config block: {e}")
        else:
            return
        print_config(new)
        flows.write_identity(s, new, yes=args.yes, current=cur)


def cmd_unlock(args):
    with session(args) as s:
        ok = flows.set_write_protect(s, s.port, on=args.lock, yes=args.yes, dry_run=args.dry_run)
    sys.exit(0 if ok is not False else 1)


EXTRACTOR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "alcormp-extract")


def run_extractor(argv):
    """`alcortool extract ...` is the standalone alcormp-extract, run as is."""
    os.execv(sys.executable, [sys.executable, EXTRACTOR, *argv])


def cmd_help(args):
    ap, path = parser(), list(args.command)
    if path[:1] == ["extract"]:
        run_extractor(["--help"])
    try:
        ap.parse_args([*path, "--help"])
    except SystemExit:
        pass


def cmd_self_test(args):
    from . import selftest
    sys.exit(0 if selftest.run() else 1)


# -- dev --
def cmd_dev_map(args):
    print("host commands (protocol.COMMANDS), sorted by CDB so variants sit together. CDB shown up to\n"
          "its last nonzero byte (at least 3); len = the length AlcorMP sends, zero padded (the ROM\n"
          "never reads it, docs/commands.md)")
    print(f"  {'name':14} {'CDB':36} {'len':>3}  {'dir':4} {'reply':>5}  meaning")
    last = None
    for name, c in sorted(COMMANDS.items(), key=lambda kv: (kv[1].cdb.ljust(16, b"\0"), len(kv[1].cdb))):
        if last is not None and c.cdb[0] != last:
            print()
        last = c.cdb[0]
        shown = c.cdb[:max(min(3, len(c.cdb)), len(c.cdb.rstrip(b"\0")))]
        print(f"  {name:14} {shown.hex(' '):36} {len(c.cdb):3}  {c.dir:4} {c.length:5}  {c.meaning}")
    print("\nNAND ops, FA 0B <sub> (protocol.SUBS) and the RAM loaders that serve them (f700.LOADERS):")
    roles = list(f700.LOADERS)
    print(f"  sub  {'name':30} {'len':>3} poll  " + " ".join(f"{r:7}" for r in roles))
    for sub, d in SUBS.items():
        served = " ".join(f"{'x' if sub in f700.LOADERS[r][1] else '.':7}" for r in roles)
        print(f"  {sub:02x}   {d.name:30} {d.length:3} {'yes ' if d.polled else '    '}  {served}")
    print("\nloaders: " + ", ".join(f"{r} = {img}" for r, (img, _) in f700.LOADERS.items()))
    print("\nFA 0B subs of the tool's own probe loader (alcor/loaders/probe.a51, `dev loader`, `dev nand`): "
          + ", ".join(f"{sub:02x} {name}" for name, sub in PROBE_SUBS.items()))


def cmd_dev_plan(args):
    geo = Geometry(args.flash_id)
    print(geo.describe())
    sweeps, skip = flows.sweep_options(args.bad_blocks, args.erase, args.verify, args.fill, args.skip)
    ops = provision.generate(geo, sweeps=sweeps, skip=skip)
    flows.print_plan(ops)
    if args.params:
        flows.print_params(ops, geo)
    for i, o in enumerate(ops[:args.ops]):
        print(f"  {i:5} {o.phase:22} {o.direction:4} {o.cdb.hex(' '):48} {o.note}")


def cmd_dev_probe(args):
    dev, ids = pick_device(args)
    with Session(dev) as s:
        p = probe_controller(s)
    for n in p["notes"]:
        print(f"  - {n}")
    print(f"compatible: {'yes' if p['ok'] else 'NO'}")
    sys.exit(0 if p["ok"] else 2)


def cmd_dev_reset(args):
    dev, _ = pick_device(args)
    reset_port(usb_port(dev))
    print(f"reset USB port {usb_port(dev)}")


def cmd_dev_read(args):
    with session(args) as s:
        names = list(READS) if args.name == "all" else [args.name]
        if args.out:
            os.makedirs(args.out, exist_ok=True)
        for name in names:
            try:
                data = read(s, name)
            except (BotError, OSError) as e:
                print(f"{name:10} FAILED: {e}")
                continue
            if args.out:
                with open(os.path.join(args.out, f"{name}.bin"), "wb") as f:
                    f.write(data)
                print(f"{name:10} {len(data):4} bytes")
            else:
                print(f"# {READS[name].meaning}  cdb={READS[name].cdb.hex(' ')}\n{hexdump(data)}")


def cmd_dev_page(args):
    with session(args) as s:
        data = s.read(page_cdb(args.ce, args.block, args.page, args.sectors), (args.sectors + 1) * 512)
        body, status = data[:args.sectors * 512], data[args.sectors * 512:]
        print(hexdump(body))
        if status:
            bad = [i for i, b in enumerate(status[0x0C:0x0C + args.sectors]) if b == 0xEE]
            print(f"# status sector (ECC fail chunks: {bad or 'none'})\n{hexdump(status)}")
        if args.out:
            with open(args.out, "wb") as f:
                f.write(data)


def cmd_dev_code(args):
    with session(args) as s:
        if args.prime:
            try:                                           # load the overlay that serves it first
                s.read(args.prime if len(args.prime) == 1 else args.prime.ljust(16, b"\0"), 512)
            except (BotError, OSError) as e:
                print(f"prime read: {e} (continuing)")
        image = read_code(s, args.size)
    with open(args.file, "wb") as f:
        f.write(image)
    print(f"wrote {len(image)} bytes to {args.file}")
    if len(image) >= 0xE000:
        print(f"resident overlay: index {image[0xDFFC]:#04x}, signature {image[0xDFFE:0xE000].hex(' ')}"
              + ("" if image[0xDFFE:0xE000] == b"\x55\xaa" else " (no 55 AA: none resident)"))


def cmd_dev_mp(args):
    with session(args) as s:
        geo = Geometry(read(s, "flashid8")[:6])
        mp = MpSession(s, geo).enable()
        print("MP mode on (controller RAM only; replug to leave)")
        print(f"FA 0B 06 poll:\n{hexdump(s.cmd('poll'))}")
        for spec in args.read:
            tail, n = spec.rsplit(":", 1)
            c = b"\xfa\x0b" + bytes.fromhex(tail.replace(" ", ""))
            try:
                data = s.read(c, int(n, 0))
                print(f"{c.hex(' ')}: {len(data)} bytes\n{hexdump(data[:512])}")
            except CommandFailed as e:
                print(f"{c.hex(' ')}: {e}")
        del mp


def cmd_dev_loader(args):
    s = session(args)
    pages = f700.REG_PAGES if args.regs else ()
    out = flows.probe_loader(s, s.port, args.xwrite, args.xread, pages)
    for page, (before, after) in out["pages"].items():
        diff = [i for i in range(512) if before[i] != after[i]]
        print(f"XDATA {page << 8:#06x}: {len(diff)} bytes changed by the upload"
              + "".join(f"\n  {(page << 8) + i:#06x}  {before[i]:02x} -> {after[i]:02x}" for i in diff))
        if args.out:
            for tag, data in (("before", before), ("after", after)):
                open(os.path.join(args.out, f"xdata_{page:02x}00_{tag}.bin"), "wb").write(data)
    if args.out:
        os.makedirs(args.out, exist_ok=True)
    if "iram" in out:
        print(f"internal RAM 0x00-0xFF:\n{hexdump(out['iram'])}")
    for (addr, n), data in ((k, v) for k, v in out.items() if isinstance(k, tuple)):
        if args.out:
            os.makedirs(args.out, exist_ok=True)
            path = os.path.join(args.out, f"xdata_{addr:04x}_{n * 512:x}.bin")
            open(path, "wb").write(data)
            print(f"XDATA {addr:#06x}+{len(data):#x}: {path}")
        else:
            print(f"XDATA {addr:#06x}+{len(data):#x}:\n{hexdump(data)}")


def print_nand_info(nand, survey):
    g = nand["geometry"]
    raw_gbit = g["page_bytes"] * g["pages_per_block"] * g["blocks_per_lun"] * g["luns"] * 8 / 2 ** 30
    cells = {1: "SLC", 2: "MLC", 3: "TLC", 4: "QLC"}.get(g["bits_per_cell"], "?")
    ecc = "in the extended parameter page" if g["ecc_bits"] == 0xFF else f"{g['ecc_bits']} bits"
    print(f"ID          : {nand['id'].hex(' ')}  (JEDEC {g['jedec_id']:#04x})")
    print(f"ONFI        : {g['manufacturer']} {g['model']}, ONFI {'/'.join(map(str, g['onfi_versions']))}, "
          f"{g['parameter_pages']} parameter pages")
    print(f"Pages       : {g['page_bytes']} + {g['spare_bytes']} B; {g['pages_per_block']} per block "
          f"({g['pages_per_block'] // g['bits_per_cell']} in SLC mode, DAh)")
    print(f"Blocks      : {g['blocks_per_lun']} x {g['luns']} LUN, {g['planes']} planes, "
          f"address cycles {g['col_cycles']} column + {g['row_cycles']} row; {raw_gbit:.0f} Gbit of data area")
    print(f"Cells       : {g['bits_per_cell']} bits ({cells}); endurance {g['endurance']} cycles; "
          f"at most {g['max_bad_blocks']} bad blocks; programs per page {g['programs_per_page']}")
    print(f"ECC needed  : {ecc}")
    print(f"Timing      : tR {g['t_r_us']} us, tPROG {g['t_prog_us']} us, tBERS {g['t_bers_us']} us")
    for page, (what, detail) in survey.items():
        print(f"Block 0 p{page:02x} : {what}; {detail}")


def cmd_dev_nand(args):
    s = session(args)
    resident = read_code(s, 0x1000, 0xC000) if args.nand_cmd == "info" else None
    with flows.nand_session(s, s.port) as nand:
        geo = nand["geometry"]
        if args.nand_cmd == "info":
            print_nand_info(nand, flows.nand_survey(s, geo, resident))
        elif args.nand_cmd == "read":
            raw = flows.nand_read_page(s, geo, args.block, args.page, slc=args.slc)
            data = flows.strip_parity(raw, args.frame) if args.frame else raw
            print(f"read    : block {args.block:#x} page {args.page:#x}: {len(raw)} B raw"
                  + (f", {len(data)} B data in {args.frame}-B frames" if args.frame else "")
                  + (", all FF (erased)" if raw.count(0xFF) == len(raw) else ""))
            if args.out:
                open(args.out, "wb").write(data)
                print(f"saved   : {args.out}")
            else:
                print(hexdump(data[:512]))
        elif args.nand_cmd == "erase":
            if args.block != f700.SCRATCH_BLOCK and not args.any_block:
                raise SystemExit(f"refusing to erase block {args.block:#x}: only the scratch block "
                                 f"{f700.SCRATCH_BLOCK:#x} without --any-block")
            flows.confirm(f"erase NAND block {args.block:#x} on /dev/{s.dev}? its contents are lost", args.yes)
            status = flows.nand_erase_block(s, geo, args.block)
            print(f"erase   : block {args.block:#x}: status {status:#04x} ({'FAILED' if status & 1 else 'ok'})")
            raw = flows.nand_read_page(s, geo, args.block, 0)
            print(f"verify  : page 0 " + ("all FF" if raw.count(0xFF) == len(raw) else
                                          f"NOT erased ({len(raw) - raw.count(0xFF)} bytes not FF)"))


def cmd_dev_readflash(args):
    with session(args) as s:
        geo = Geometry(read(s, "flashid8")[:6])
        sub, region, frame = 0x19, None, 0
        if args.frame:                                     # 46: region ff ff + frame bytes 0-1, [14] byte 2
            sub, region, frame = 0x46, b"\xff\xff" + args.frame[:2], args.frame[2]
        elif args.region:
            sub, region, frame = 0x1F, args.region, 0 if args.no_echo else None
        loader = None
        if args.loader:                                    # e.g. "rwfw+fw_data" and "B fw28 rows slc slack"
            role, *edits = args.loader.split("+")
            words = args.param.split()
            loader = dict(role=role, stage=args.stage, edits=edits, slack="slack" in words,
                          map=next((w for w in words if w in ("slc", "nomap")), None),
                          tokens=[w for w in words if w not in ("slc", "nomap", "slack")])
        flows.read_flash(s, geo, args.out, args.block, args.blocks, args.page, args.pages, args.sectors, sub,
                         region, frame, loader, args.spare, args.verbose_pages)


def cmd_dev_writetest(args):
    protected = {f700.SYS_BLOCK, f700.MIRROR_BLOCK, f700.HK_BLOCK, *f700.DATA_SUPERBLOCKS}
    if args.block in protected:
        sys.exit(f"block {args.block:#x} is a system block; refusing")
    if args.block != f700.SCRATCH_BLOCK and not args.any_block:
        sys.exit(f"a block other than the scratch block {f700.SCRATCH_BLOCK:#x} needs --any-block")
    if args.backup and os.path.exists(args.backup):
        sys.exit(f"{args.backup} exists; refusing to overwrite it")
    with session(args) as s:
        geo = Geometry(read(s, "flashid8")[:6])
        ok = flows.write_test(s, geo, args.block, args.pages, args.ppb, args.keep, args.backup, args.yes)
    sys.exit(0 if ok else 1)


def cmd_dev_record(args):
    with session(args) as s:
        rec = flows.save_record(s, args.yes) if args.save else read(s, "record")
    print(f"info record: {flows.record_summary(rec)}")
    print_record(rec)


def cmd_dev_housekeeping(args):
    with session(args) as s:
        geo = Geometry(read(s, "flashid8")[:6])
        tables = flows.read_housekeeping(MpSession(s, geo).enable())
    print("(MP mode is on: replug to leave it)")
    if tables is None:
        sys.exit("no readable housekeeping tables (never formatted by AlcorMP or alcortool)")
    h = hk_summary(geo, tables)
    print(f"LLF date    : {h['date']} (chip {h['chip']})")
    print(f"LLF by      : host {h['host']!r}, user {h['user']!r}, INI name {h['ini_name']!r}")
    print(f"Planned cap.: {h['planned_capacity']} sectors ({h['planned_capacity'] * 512 / 1e9:.2f} GB)")
    print(f"Good blocks : {'/'.join(map(str, h['lane_good']))} per lane")
    print(f"Bad blocks  : {len(h['bad'])}: {' '.join(hex(b) for b in h['bad']) or '-'}")
    print(f"System      : {' '.join(hex(b) for b in h['system']) or '-'} (boundary bases, skipped by the sweeps)")
    if args.verbose:
        print(f"Block status (worst page value per block):\n{hexdump(tables[HK_STATUS][:geo.blocks])}")


# == PARSER ==
def format_options(p, dev=True):
    p.add_argument("--bad-blocks", choices=("quick", "full", "none", "keep"), default="quick",
                   help="quick = screen blocks 0x00-0x8f (default); full = also the die-wide program/read "
                        "sweeps (~45 of 60 min); none = assume a clean die, write no table; keep = carry "
                        "over the table the stick has")
    p.add_argument("--capacity", type=parse_capacity, metavar="SIZE",
                   help="cap the size: sectors, 8GB, 7.5GiB, or 'keep' (default: every usable block)")
    adv = p.add_argument_group("advanced")
    adv.add_argument("--erase", choices=("blocks", "chip"), default="blocks",
                     help="chip = chip erase only, without the die-wide block erase sweeps")
    adv.add_argument("--verify", choices=("none", "full"), default="none", help="full = keep the read-back checks")
    adv.add_argument("--fill", choices=("full", "minimal"), default="full", help="minimal = no LLF filler pages")
    adv.add_argument("--skip", type=lambda t: [g for g in t.split(",") if g], default=[], metavar="GROUPS",
                     help="drop op groups: " + "; ".join(f"{k} = {v}" for k, v in provision.SKIP_GROUPS.items()))
    if dev:
        adv.add_argument("--flash-id", help="the part to format for (default: read from the stick)")
        adv.add_argument("--no-backup", action="store_true", help="skip the backup to firmware/dumps/")
        adv.add_argument("--rom-wait", type=int, default=600, metavar="SECONDS",
                         help="how long to wait for a locked stick to come back in ROM mode (0 = just explain)")
        adv.add_argument("--port", help="refuse unless the stick is on this USB port (e.g. 1-8)")
        adv.add_argument("--max-failures", type=int, default=200, help="abort after this many rejected ops")


def parser():
    ap = argparse.ArgumentParser(prog="alcortool", description=__doc__, epilog=EPILOG,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-d", "--device", help="block device, e.g. sda (default: the only Alcor drive)")
    ap.add_argument("-v", "--verbose", action="count", default=0, help="log every op (-vv: and the data)")
    sub = ap.add_subparsers(dest="cmd", metavar="COMMAND", required=True)

    def add(name, fn, help, parent=sub, **kw):
        p = parent.add_parser(name, help=help, description=help, **kw)
        p.set_defaults(fn=fn)
        return p

    def yes(p):
        p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")

    add("extract", None, "alias for ./alcormp-extract: an AlcorMP release -> the data alcortool reads")
    add("list", cmd_list, "list Alcor drives")
    add("info", cmd_info, "controller, flash, firmware, identity, write-protect and info record "
        "(-v: every decoded field, UFDMode, last LLF)")
    p = add("backup", cmd_backup, "read-only backup of identity, firmware and every reply to firmware/dumps/")
    p.add_argument("--data", action="store_true", help="also image the whole disk")
    p.add_argument("--out", default=flows.DUMPS_DIR, help="dumps root (default firmware/dumps)")
    p = add("restore", cmd_restore, "write a backup back: identity and disk (default), or --full (format + restore)")
    p.add_argument("dump", help="backup directory")
    p.add_argument("--identity", action="store_true", help="only the identity block")
    p.add_argument("--data", action="store_true", help="only the disk image")
    p.add_argument("--full", action="store_true", help="format, then the backup's identity, boot firmware and disk")
    yes(p)
    p = add("format", cmd_format, "low-level format: backup, erase, bad-block scan, provision, verify (DESTRUCTIVE)")
    format_options(p)
    p.add_argument("--dry-run", action="store_true", help="show the plan and the stream; send nothing")
    yes(p)
    p = add("firmware", cmd_firmware, "rewrite the boot firmware only, keeping the FTL and the data")
    p.add_argument("image", nargs="?", help="boot image (AlcorMP .BIN or raw; default: the bundled one)")
    p.add_argument("--capacity", type=parse_capacity, metavar="SIZE", help="new size (default: keep)")
    p.add_argument("--blind", action="store_true", help="no read-back: write the clean-die layout (ROM mode)")
    p.add_argument("--dry-run", action="store_true", help="read back and validate only")
    yes(p)
    p = add("identity", cmd_identity, "show or change the USB/SCSI identity and serial")
    p.add_argument("--vendor", help="USB manufacturer + SCSI vendor string")
    p.add_argument("--product", help="USB product + SCSI product string")
    p.add_argument("--serial", help="USB serial string")
    p.add_argument("--load", metavar="FILE", help="write this 512-B config block")
    p.add_argument("--save", metavar="FILE", help="save the current block")
    yes(p)
    p = add("unlock", cmd_unlock, "remove the policy write-protect (--lock: set it), keeping the data; "
            "a locked firmware (058f:9380) needs `format` instead")
    p.add_argument("--lock", action="store_true", help="turn write-protect on instead")
    p.add_argument("--dry-run", action="store_true", help="show the state; send nothing")
    yes(p)
    add("self-test", cmd_self_test, "offline checks of the builders and the stream")
    p = add("help", cmd_help, "help for a command, e.g. `alcortool help dev plan`")
    p.add_argument("command", nargs="*")

    dev = add("dev", None, "single steps and inspection, for a new chip or debugging")
    dsub = dev.add_subparsers(dest="dev_cmd", metavar="STEP", required=True)
    add("map", cmd_dev_map, "print the command map: host commands, FA 0B subs, loaders", dsub)
    p = add("plan", cmd_dev_plan, "the format stream offline for a part: phases, op counts, loaders", dsub)
    p.add_argument("flash_id", metavar="FLASH_ID", help="e.g. '2c 84 44 32 aa 04' (see `alcortool info`)")
    format_options(p, dev=False)
    p.add_argument("--ops", type=int, default=0, metavar="N", help="also list the first N ops")
    p.add_argument("--params", action="store_true", help="also show each param block and loader patch as bytes")
    add("probe", cmd_dev_probe, "read-only compatibility probe (exit 2 if not an AlcorMP controller)", dsub)
    add("reset", cmd_dev_reset, "USB bus reset (re-enumerates; not a power cycle)", dsub)
    p = add("read", cmd_dev_read, "send a read command and hexdump the reply", dsub)
    p.add_argument("name", choices=[*READS, "all"])
    p.add_argument("-o", "--out", metavar="DIR", help="save <name>.bin files instead")
    p = add("page", cmd_dev_page, "FA 01 page read (AlcorMP never sends it; F700 answers not ready)", dsub)
    p.add_argument("--ce", type=int, default=0)
    p.add_argument("--block", type=num, default=0)
    p.add_argument("--page", type=num, default=0)
    p.add_argument("--sectors", type=int, default=1, choices=range(1, 33), metavar="1-32")
    p.add_argument("-o", "--out", help="also save data + status sector")
    p = add("code", cmd_dev_code, "read the 8051 CODE space (FA 10) to a file", dsub)
    p.add_argument("file")
    p.add_argument("--size", type=num, default=0x10000)
    p.add_argument("--prime", type=lambda x: bytes.fromhex(x.replace(" ", "")), metavar="HEX",
                   help="send this read CDB first, to load the overlay that serves it")
    p = add("mp", cmd_dev_mp, "enter MP mode (RAM only) and send raw FA 0B reads", dsub)
    p.add_argument("--read", action="append", default=[], metavar="HEX:LEN",
                   help="CDB bytes after FA 0B, and the reply length")
    p = add("loader", cmd_dev_loader, "run the tool's own probe loader: no AlcorMP code, RAM only; "
            "reads internal RAM and XDATA, then a USB bus reset restarts the firmware", dsub)
    p.add_argument("--xread", action="append", default=[], type=addr_count, metavar="ADDR[:SECTORS]",
                   help="read XDATA from ADDR, 512-B sectors (default 1); repeatable")
    p.add_argument("--xwrite", action="append", default=[], type=addr_byte, metavar="ADDR=BYTE",
                   help="write one XDATA byte (RAM or a controller register) before the reads; repeatable")
    p.add_argument("--regs", action="store_true",
                   help="read the register pages (f700.REG_PAGES) before the upload (FA 13) and after it, and diff them")
    p.add_argument("-o", "--out", metavar="DIR", help="save the XDATA reads here instead of printing them")
    nand = add("nand", None, "NAND access through the tool's own probe loader (no AlcorMP data); "
               "ends with a USB bus reset", dsub)
    nsub = nand.add_subparsers(dest="nand_cmd", metavar="OP", required=True)
    add("info", cmd_dev_nand, "everything the NAND says about itself (ID, ONFI page) and what block 0 holds", nsub)
    p = add("read", cmd_dev_nand, "read one raw page as stored: data, ECC parity and spare (no correction)", nsub)
    p.add_argument("--block", type=num, default=0)
    p.add_argument("--page", type=num, default=0)
    p.add_argument("--frame", type=num, choices=sorted(f700.ECC_FRAMES), metavar="BYTES",
                   help="strip the ECC parity: frames of this size (650 system pages, 1162 data pages)")
    p.add_argument("--slc", action="store_true", help="read in SLC mode (DAh), as the system blocks are written")
    p.add_argument("-o", "--out", metavar="FILE", help="save the data instead of printing its start")
    p = add("erase", cmd_dev_nand, "erase one block (ERASES FLASH; the scratch block unless --any-block), "
            "then check page 0 reads back erased", nsub)
    p.add_argument("--block", type=num, default=f700.SCRATCH_BLOCK)
    p.add_argument("--any-block", action="store_true", help="allow a block other than the scratch block")
    yes(p)
    p = add("readflash", cmd_dev_readflash, "read NAND pages in MP mode (19; 1f with --region; 46 with --frame)", dsub)
    p.add_argument("out", help="data file (+ OUT.map with per-page status)")
    p.add_argument("--block", type=num, default=0)
    p.add_argument("--blocks", type=num, default=1)
    p.add_argument("--page", type=num, default=0)
    p.add_argument("--pages", type=num, default=1)
    p.add_argument("--sectors", type=num, choices=range(1, 33), metavar="1-32")
    p.add_argument("--region", type=hexbytes(4), help="1f read with these CDB region bytes")
    p.add_argument("--frame", type=hexbytes(3), help="46 read with this ECC frame (e.g. 20028a)")
    p.add_argument("--no-echo", action="store_true", help="1f: CDB[14] = 0 instead of the page low byte")
    p.add_argument("--loader", metavar="ROLE[+EDIT]", help="re-enable with this loader first (e.g. rwfw+fw_data)")
    p.add_argument("--stage", choices=list(f700.STAGES), default="ftl", help="the stage of that re-enable")
    p.add_argument("--param", default="B", help="its param: tokens, slc|nomap, slack (e.g. 'B fw28 rows slc slack')")
    p.add_argument("--spare", action="store_true", help="also save the status tails")
    p.add_argument("--verbose-pages", action="store_true", help="print every page, not only failures")
    p = add("writetest", cmd_dev_writetest, "erase/program/verify one scratch block (WRITES FLASH)", dsub)
    p.add_argument("--block", type=num, default=f700.SCRATCH_BLOCK)
    p.add_argument("--any-block", action="store_true", help="allow a block other than the scratch block")
    p.add_argument("--pages", type=int, default=1, choices=range(1, 33), metavar="1-32")
    p.add_argument("--ppb", type=num, help="pages to blank-check (default: pages per block)")
    p.add_argument("--keep", action="store_true", help="leave the test data (no final erase)")
    p.add_argument("--backup", metavar="FILE", help="back up a non-blank block here, then erase it anyway")
    yes(p)
    p = add("record", cmd_dev_record, "decode the firmware's info record (82 51 00)", dsub)
    p.add_argument("--save", action="store_true", help="first make the firmware save it (96)")
    yes(p)
    add("housekeeping", cmd_dev_housekeeping,
        "AlcorMP's LLF tables: date, host/user, planned capacity, bad blocks (MP mode, RAM only)", dsub)
    return ap


def main(argv=None):
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))   # close sessions, reattach usb-storage
    argv = sys.argv[1:] if argv is None else argv
    ap = parser()
    if not argv:
        ap.print_help()
        return
    if argv[0] == "extract":
        run_extractor(argv[1:])
    args = ap.parse_args(argv)
    try:
        args.fn(args)
    except (CommandFailed, DataError) as e:
        sys.exit(f"error: {e}")
    except BotError as e:
        sys.exit(f"USB error: {e} (replug the stick; -v logs every command)")
    except KeyboardInterrupt:
        sys.exit(130)
