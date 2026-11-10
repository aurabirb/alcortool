"""The command map: every CDB the tool sends, the FA 0B NAND-op table, and the Op a
stream is made of. `alcortool dev map` prints these tables.

Transport, framing and what each command does: docs/commands.md. Which RAM loader
serves which FA 0B sub is a controller datum (f700.LOADERS).
"""
import collections


def cdb(*b, length=16):
    return bytes(b) + bytes(length - len(b))


# == HOST COMMANDS ==
# name: (CDB, data direction, reply length, meaning). The first block is what
# `backup` saves (replies/<name>.bin) and `dev read` sends.
Command = collections.namedtuple("Command", "cdb dir length meaning")
READS = {
    "ident":     Command(cdb(0x9A, length=1), "in", 512, "identify: chip ID [4:6], FA 0E flag [0x2C], WP [0x10A]"),
    "info":      Command(cdb(0xFA, 0x0E), "in", 512, "controller info (refused when locked)"),
    "flashid":   Command(cdb(0xFA, 0x00), "in", 512, "flash ID per CE, 16-B slots"),
    "flashid8":  Command(cdb(0xFA, 0, 0, 0, 0, 0, 0, 0x08, length=8), "in", 512, "flash ID per CE, chip >= F500"),
    "regs":      Command(cdb(0xFA, 0x13, 0x00, 0x90), "in", 512, "register page 0x90"),
    "config":    Command(cdb(0x82, 0x51, 0x01), "in", 512, "config block: USB/SCSI identity, serial"),
    "record":    Command(cdb(0x82, 0x51, 0x00), "in", 512, "info record as mounted (tag F1 + capacity at 9)"),
    "sysconfig": Command(cdb(0x82, 0x51, 0x04), "in", 512, "MP info sector (record page sector 4)"),
    "inquiry":   Command(cdb(0x12, 0, 0, 0, 36, 0, length=6), "in", 36, "standard INQUIRY"),
    "modesense": Command(cdb(0x1A, 0, 0x3F, 0, 192, 0, length=6), "in", 192, "MODE SENSE(6), all pages"),
    "capacity":  Command(cdb(0x25, length=10), "in", 8, "READ CAPACITY(10)"),
}
COMMANDS = {
    **READS,
    "chip_erase":   Command(cdb(0xFA, 0x0C, 0x00, 0x33, length=8), "none", 0, "chip-wide erase (minutes)"),
    "mp_write":     Command(cdb(0xFA, 0x1D, length=8), "none", 0, "write-phase enable / re-enable opener"),
    "mp_leave":     Command(cdb(0xFA, 0x1D, 0x01, length=8), "none", 0, "leave MP write mode"),
    "loader":       Command(cdb(0xFA, 0x0A, 0x00, 0x10, length=8), "out", 9216, "upload a RAM loader"),
    "regs_write":   Command(cdb(0xFA, 0x12, 0x00, 0x00, 0x00, 0x03), "out", 512, "register block write"),
    "regs_read":    Command(cdb(0xFA, 0x13, 0x00, 0x90, length=8), "in", 512, "register page 0x90 (8-B form)"),
    "regs_close":   Command(cdb(0xFA, 0x12, 0x00, 0x90, 0x00, 0x09, length=8), "out", 512, "closing register block"),
    "wp_check":     Command(cdb(0xF8, 0x51, 0x00, 0xFF, length=8), "in", 512, "write-enable / hardware-WP check"),
    "wp_check_end": Command(cdb(0xF8, 0x51, 0xFF, 0x00), "in", 512, "write-enable check after the reset"),
    "status":       Command(cdb(0xFA, 0x05, length=8), "in", 512, "status read (FA 05)"),
    "code":         Command(cdb(0xFA, 0x10, 0x10, length=8), "in", 8192, "CODE space read: [2] sectors, [4] addr >> 8"),
    "page":         Command(cdb(0xFA, 0x01, 0x01, length=8), "in", 1024, "NAND page read outside MP mode: [2] sectors, [3] CE, [4:6] block, [6:8] page"),
    "ident10":      Command(cdb(0x9A, length=10), "in", 512, "identify, 10-B form (before F5)"),
    "options":      Command(cdb(0xF5, length=10), "out", 512, "F5 option record (0x51 = on)"),
    "geometry_echo": Command(cdb(0xFA, 0x80, 0x00, 0x80, length=8), "in", 512, "final info/geometry echo"),
    "capacity_rec": Command(cdb(0x99, length=10), "out", 512, "99 capacity + usable blocks per FTL lane"),
    "reset":        Command(cdb(0xFA, 0x08, 0x00, length=8), "none", 0, "controller reset: firmware reloads from NAND"),
    "after_reset":  Command(cdb(0xFA, 0x1F, length=10), "none", 0, "sent after the closing reset"),
    "set_wp":       Command(cdb(0x9B), "out", 512, "policy write-protect in RAM from data[1] (bit 1); 96 saves it"),
    "save_record":  Command(cdb(0x96), "in", 512, "serialize the info record from RAM and save it (sector 0)"),
    "write_config": Command(cdb(0x81, 0x00, 0xFF), "out", 512, "write the config block (record page sector 1)"),
    "write_mp_info": Command(cdb(0x81, 0x51, 0x04), "out", 512, "write the MP info sector (sector 4)"),
    "param":        Command(cdb(0xFA, 0x0B, 0x01, length=8), "out", 2048, "FA 0B 01 param block"),
    "poll":         Command(cdb(0xFA, 0x0B, 0x06, length=8), "in", 512, "FA 0B 06 status poll: [0] 01 error, 02 fail"),
    "burst_open":   Command(cdb(0xFA, 0x0B, 0x09, 0x00, 0x20, 0x02, 0x8A, length=8), "none", 0, "open a commit burst"),
    "burst_close":  Command(cdb(0xFA, 0x0B, 0x08, length=8), "none", 0, "close a commit burst"),
    "mp_enabled":   Command(cdb(0xFA, 0x0B, 0x3E, 0x00, 0x00, 0x01), "none", 0, "end of the MP write-phase enable"),
}
CHIP_ERASE_TIMEOUT_MS = 180000
SWEEP_TIMEOUT_MS = 30000

# == NAND OPS (FA 0B <sub>) ==
# CDB: [2] sub, [3] CE, [4:6] block, [6:8] page; 16-B forms add [10:14] region. Per sub:
# its CDB length, where the sector / frame / count bytes go (None: absent), whether the
# frame byte echoes the page low byte when no frame is given, its reply, whether a 06
# poll follows it, and a sweep's long timeout. VERIFIED against every FA 0B op of both
# AlcorMP captures.
Sub = collections.namedtuple("Sub", "name length sectors frame echo count reply polled slow")
_P, _S = "sectors", "ecc map"       # reply: (sectors + 2) x 512 / 4 KB (8 KB at page 0x100)
SUBS = {
    0x01: Sub("param block", 8, None, None, False, None, 0, False, False),
    0x03: Sub("pattern buffer load", 16, 8, None, False, None, 0, False, False),
    0x04: Sub("sweep skip bitmap", 16, 8, None, False, None, 0, True, False),
    0x05: Sub("sweep failure report", 16, 8, None, False, None, 512, True, False),
    0x06: Sub("status poll", 8, None, None, False, None, 512, False, False),
    0x08: Sub("close commit burst", 8, None, None, False, None, 0, False, False),
    0x09: Sub("open commit burst", 8, None, None, False, None, 0, False, False),
    0x11: Sub("block erase (page hi = mode)", 8, None, None, False, None, 512, True, False),
    0x18: Sub("page write", 16, 9, 14, True, None, 0, True, False),
    0x19: Sub("page read", 16, 9, 14, True, None, _P, True, False),
    0x1E: Sub("28/32-sector page program", 16, 9, 14, True, None, 0, True, False),
    0x1F: Sub("page read-back", 16, 9, 14, True, None, _P, True, False),
    0x21: Sub("housekeeping table write", 16, 9, None, False, None, 0, True, False),
    0x22: Sub("housekeeping table read", 16, 9, None, False, None, 2048, True, False),
    0x3A: Sub("FTL marker", 16, 9, None, False, None, 0, False, False),
    0x3E: Sub("scratch-phase marker", 16, 9, 14, True, None, 0, False, False),
    0x42: Sub("scan marker read", 8, None, None, False, None, 512, True, False),
    0x43: Sub("ECC bit-error map", 8, None, None, False, None, _S, True, False),
    0x45: Sub("system page write", 16, 9, 14, True, None, 0, True, False),
    0x46: Sub("system page read (ECC frame)", 16, 9, 14, True, None, _P, True, False),
    0x47: Sub("scan marker read", 8, None, None, False, None, 512, True, False),
    0x90: Sub("FTL spare/marker scan", 16, 9, None, False, None, 512, True, False),
    0x91: Sub("FTL activate", 16, 9, 15, False, None, 512, True, False),
    0x92: Sub("FTL map read-back", 16, 9, 15, False, None, 19456, False, False),
    0x94: Sub("column-skip table", 16, 3, None, False, None, 0, False, False),
    0x96: Sub("housekeeping marker", 16, 9, 14, True, None, 0, False, False),
    0x97: Sub("boundary base write", 16, 9, None, False, None, 0, True, False),
    0x99: Sub("FTL index page", 16, 9, None, False, None, 0, False, False),
    0x9A: Sub("FTL commit marker", 16, 9, 15, False, None, 512, True, False),
    0x9B: Sub("FTL marker read", 16, 9, None, False, None, 512, True, False),
    0xA0: Sub("erase sweep", 8, None, None, False, None, 0, True, True),
    0xA1: Sub("read check sweep", 16, 9, None, False, None, 512, True, True),
    0xA2: Sub("multi-plane erase sweep", 8, None, None, False, None, 0, True, True),
    0xA7: Sub("die-wide erase sweep", 8, None, None, False, None, 0, True, True),
    0xAE: Sub("translation table write", 16, 9, None, False, None, 0, True, False),
    0xAF: Sub("translation table verify", 16, 9, None, False, None, 512, True, False),
    0xB0: Sub("erase + program sweep", 8, None, None, False, 7, 0, True, True),
    0xB1: Sub("copyback sweep", 16, None, None, False, 9, 0, True, True),
    0xC0: Sub("SLC read-back sweep", 16, 9, None, False, 8, 2048, False, True),
    0xC1: Sub("read-back sweep", 16, 9, None, False, 8, 2048, False, True),
}


# == PROBE LOADER OPS (FA 0B <sub> under alcor/loaders/probe.a51, layouts in its header) ==
# CDB: [2] sub, [3] CE (the ROM refuses CE >= patch byte 2). XDATA ops: [4:6] address,
# [7] byte to write, [9] 512-B sectors. NAND ops: [4] command and [5] address cycle, [6:8]
# bytes, [10] wait flag (nandcmd); [4:9] address cycles, [9] 512-B sectors, [10] prefix
# command (read: the raw page, byte by byte); [4:7] row (erase).
PROBE_SUBS = {"ident": 0xE0, "xread": 0xE1, "xwrite": 0xE2, "nandcmd": 0xE3, "read": 0xE4, "erase": 0xE5}
PROBE_SIGNATURE = b"alcortool probe 5"
CAPTURE_SUB = 0xE6                      # capture loader: CDB[4] = the overlay to load
ONFI = {"read_id": 0x90, "param_page": 0xEC, "slc": 0xDA}  # NAND commands; DAh: Micron SLC-mode prefix


def xdata_page_cdb(page):
    """FA 13 00 PP: 512 B of XDATA from PP00, served by the ROM in normal mode too."""
    return bytes((0xFA, 0x13, 0x00, page, 0, 0, 0, 0))


def capture_cdb(overlay):
    """Under the boot-firmware capture loader (alcor/loaders/capture.a51): load `overlay`."""
    return bytes((0xFA, 0x0B, CAPTURE_SUB, 0, overlay)).ljust(16, b"\0")


def probe_cdb(name, addr=0, value=0, sectors=0):
    return bytes((0xFA, 0x0B, PROBE_SUBS[name], 0, addr >> 8, addr & 0xFF, 0, value, 0, sectors)).ljust(16, b"\0")


def probe_nand_cdb(name, ce=0, cycles=b"", count=0, wait=False, prefix=0):
    """nandcmd: cycles = command + address byte, count = bytes to read; read: cycles = the
    five address cycles, count = 512-B sectors, prefix = a command before 00h; erase:
    cycles = the three row cycles."""
    c = bytearray(16)
    c[0:4] = (0xFA, 0x0B, PROBE_SUBS[name], ce)
    c[4:4 + len(cycles)] = cycles
    if name == "nandcmd":
        c[6:8], c[10] = count.to_bytes(2, "big"), int(wait)
    elif name == "read":
        c[9], c[10] = count, prefix
    return bytes(c)


def nand_cdb(sub, ce=0, block=0, page=0, sectors=0, region=b"\0\0\0\0", frame=None, count=None):
    """FA 0B CDB for `sub` in its SUBS layout."""
    s = SUBS[sub]
    c = bytearray(16)
    c[0:8] = bytes((0xFA, 0x0B, sub, ce, block >> 8, block & 0xFF, page >> 8, page & 0xFF))
    if s.length == 16:
        c[10:14] = region
    if s.sectors is not None:
        c[s.sectors] = sectors
    if s.frame is not None:
        c[s.frame] = frame if frame is not None else (page & 0xFF if s.echo else 0)
    if count is not None:
        c[s.count] = count
    return bytes(c[:s.length])


def erase_cdb(ce, block, mode):
    """Single-block erase; AlcorMP erases mode 1 (SLC pre-erase), then mode 0."""
    return nand_cdb(0x11, ce, block, mode << 8)


def code_cdb(addr, sectors=0x10):
    return cdb(0xFA, 0x10, sectors, 0x00, addr >> 8, length=8)


def page_cdb(ce, block, page, sectors):
    return cdb(0xFA, 0x01, sectors, ce, block >> 8, block & 0xFF, page >> 8, page & 0xFF, length=8)


def read_len(c):
    """IN transfer length of a command: the sub's full reply (under-reading leaves bytes
    queued and desyncs the next command), 512 for everything else."""
    if len(c) >= 3 and c[:2] == b"\xfa\x0b" and c[2] in SUBS:
        r = SUBS[c[2]].reply
        if r == _P:
            return (c[9] + 2) * 512 if len(c) > 9 and c[9] else 512
        if r == _S:
            return 8192 if c[6] else 4096
        return r or 512
    return 512


def timeout_ms(c):
    """Per-op bulk timeout override, or None for the session default."""
    if c[:2] == b"\xfa\x0c":
        return CHIP_ERASE_TIMEOUT_MS
    if c[:2] == b"\xfa\x0b" and len(c) > 2 and c[2] in SUBS and SUBS[c[2]].slow:
        return SWEEP_TIMEOUT_MS
    return None


def poll_verdict(reply):
    """FA 0B 06 reply: 'error' (bytes 1-3 = failing address), 'fail' or 'ok'."""
    return {1: "error", 2: "fail"}.get(reply[0], "ok")


# == OPS ==
class Live:
    """A payload computed at send time from earlier reads of the same run."""
    __slots__ = ("size", "fn")

    def __init__(self, size, fn):
        self.size, self.fn = size, fn

    def __len__(self):
        return self.size

    def __call__(self, reads):
        return self.fn(reads)


class Op:
    """One host->device command of a stream. `keep` names the read log an IN op's data
    goes to ({keep: [data]}); a Live payload is computed from that log when sent. `phase`
    labels progress; `group` names the --skip group the op belongs to."""
    __slots__ = ("cdb", "direction", "payload", "note", "keep", "phase", "group")

    def __init__(self, cdb, direction, payload=None, note="", keep=None, phase="", group=None):
        self.cdb, self.direction, self.payload, self.note = cdb, direction, payload, note
        self.keep, self.phase, self.group = keep, phase, group

    def data(self, reads=None):
        return self.payload(reads or {}) if isinstance(self.payload, Live) else self.payload

    def sig(self):
        """Opcode signature: FA family by sub, others by their first two bytes."""
        if self.cdb[:1] == b"\xfa" and len(self.cdb) >= 3:
            return self.cdb[:3].hex(" ")
        return self.cdb[:2].hex(" ")

    @staticmethod
    def of(name, payload=None, note=None, **kw):
        c = COMMANDS[name]
        return Op(c.cdb, c.dir, payload, note or c.meaning, **kw)


class LoaderError(RuntimeError):
    """An FA 0B op the resident RAM loader does not serve."""
