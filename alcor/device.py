"""The device side: USB Bulk-Only over usbfs (no kernel SCSI layer, no root), sysfs
lookups, the MP session and the op driver.

usbfs bypasses the kernel SCSI layer, which rejects short CDBs and rewrites LUN bits;
usb-storage is detached for a session and reattached when it closes. The controller
reports CSW residue = full length on vendor commands even when it sends data, so the
received bytes are trusted, not the residue.
"""
import contextlib
import ctypes
import errno
import fcntl
import os
import re
import struct
import sys
import time

from . import f700
from .protocol import COMMANDS, READS, SUBS, LoaderError, code_cdb, nand_cdb, poll_verdict, read_len, timeout_ms

# == USBFS ==
class BulkTransfer(ctypes.Structure):
    _fields_ = [("ep", ctypes.c_uint), ("len", ctypes.c_uint), ("timeout", ctypes.c_uint),
                ("data", ctypes.c_void_p)]


class UsbIoctl(ctypes.Structure):
    _fields_ = [("ifno", ctypes.c_int), ("ioctl_code", ctypes.c_int), ("data", ctypes.c_void_p)]


def _ioc(direction, nr, size=0):
    """Linux _IOC('U', nr, size): the size is the struct's, so 32- and 64-bit hosts differ."""
    return direction << 30 | size << 16 | ord("U") << 8 | nr


_NONE, _R, _RW = 0, 2, 3                   # _IOC_* directions; the uint ones are _IOR
USBDEVFS_BULK = _ioc(_RW, 2, ctypes.sizeof(BulkTransfer))
USBDEVFS_CLAIMINTERFACE = _ioc(_R, 15, 4)
USBDEVFS_RELEASEINTERFACE = _ioc(_R, 16, 4)
USBDEVFS_CLEAR_HALT = _ioc(_R, 21, 4)
USBDEVFS_IOCTL = _ioc(_RW, 18, ctypes.sizeof(UsbIoctl))
USBDEVFS_DISCONNECT = _ioc(_NONE, 22)
USBDEVFS_CONNECT = _ioc(_NONE, 23)
USBDEVFS_RESET = _ioc(_NONE, 20)
CBW_SIG, CSW_SIG = 0x43425355, 0x53425355   # "USBC", "USBS"
SENSE = bytes([0x03, 0, 0, 0, 18, 0])
SENSE_KEYS = {0x0: "no sense", 0x1: "recovered error", 0x2: "not ready", 0x3: "medium error",
              0x4: "hardware error", 0x5: "illegal request", 0x6: "unit attention",
              0x7: "data protect", 0xB: "aborted command"}


class BotError(Exception):
    pass


class CommandFailed(BotError):
    """CSW status 1; `sense` is the REQUEST SENSE reply."""

    def __init__(self, cdb, sense):
        self.sense = sense
        detail = ""
        if len(sense) >= 14:
            key = sense[2] & 0xF
            detail = (f" sense {key:#x} ({SENSE_KEYS.get(key, 'unknown')}) "
                      f"asc={sense[12]:#04x} ascq={sense[13]:#04x}")
        super().__init__(f"cdb {cdb.hex(' ')} failed{detail}")


class Session:
    """One Bulk-Only session on /dev/<blockdev>'s USB interface."""

    def __init__(self, blockdev, timeout_ms=10000, verbose=0):
        self.timeout, self.blockdev, self.tag, self.verbose = timeout_ms, blockdev, 1, verbose
        self.port = usb_port(blockdev)                     # sysfs path goes with usb-storage
        iface = find_interface(blockdev)
        dev = os.path.dirname(iface)
        rd = lambda p: open(p).read().strip()
        if (rd(f"{iface}/bInterfaceClass"), rd(f"{iface}/bInterfaceProtocol")) != ("08", "50"):
            raise BotError("interface is not mass-storage Bulk-Only")
        self.ifno = int(rd(f"{iface}/bInterfaceNumber"), 16)
        self.ep_in = self.ep_out = None
        for name in os.listdir(iface):
            ep = os.path.join(iface, name)
            if name.startswith("ep_") and rd(f"{ep}/type") == "Bulk":
                if rd(f"{ep}/direction") == "in":
                    self.ep_in = int(name[3:], 16)
                else:
                    self.ep_out = int(name[3:], 16)
        if self.ep_in is None or self.ep_out is None:
            raise BotError("bulk endpoints not found")
        bus, devnum = int(rd(f"{dev}/busnum")), int(rd(f"{dev}/devnum"))
        self.devnode = f"/dev/bus/usb/{bus:03d}/{devnum:03d}"
        self._trace(1, f"session: /dev/{blockdev} -> usb {bus:03d}/{devnum:03d} iface {self.ifno}")
        self.fd = os.open(self.devnode, os.O_RDWR)
        try:
            fcntl.ioctl(self.fd, USBDEVFS_IOCTL, UsbIoctl(ifno=self.ifno, ioctl_code=USBDEVFS_DISCONNECT))
        except OSError as e:
            if e.errno != errno.ENODATA:                   # no driver bound
                raise
        fcntl.ioctl(self.fd, USBDEVFS_CLAIMINTERFACE, struct.pack("I", self.ifno))

    def close(self):
        """Release and reattach usb-storage; the device may already be gone (re-enumerated).
        A second call does nothing."""
        if self.fd is None:
            return
        for code, arg in ((USBDEVFS_RELEASEINTERFACE, struct.pack("I", self.ifno)),
                          (USBDEVFS_IOCTL, UsbIoctl(ifno=self.ifno, ioctl_code=USBDEVFS_CONNECT))):
            try:
                fcntl.ioctl(self.fd, code, arg)
            except OSError as e:
                self._trace(2, f"close: ioctl failed (errno {e.errno}); device likely gone")
        os.close(self.fd)
        self.fd = None
        deadline = time.monotonic() + 8                    # usb-storage rescans asynchronously
        while self.alive() and not os.path.exists(f"/sys/block/{self.blockdev}") and time.monotonic() < deadline:
            time.sleep(0.1)

    def takeover(self, other):
        """Continue as `other`, a session on the stick after it re-enumerated."""
        self.close()
        self.__dict__.update(other.__dict__)

    def reset(self):
        """USB bus reset with the interface still claimed: the controller restarts from NAND
        before usb-storage can send it anything (close() then reattaches it)."""
        try:
            fcntl.ioctl(self.fd, USBDEVFS_RESET, 0)
        except OSError as e:
            if e.errno != errno.ENODEV:
                raise

    def alive(self):
        """False once the device left the bus (a re-enumeration gets a new device node)."""
        return os.path.exists(self.devnode)

    @contextlib.contextmanager
    def timeout_ms(self, ms):
        saved, self.timeout = self.timeout, ms
        try:
            yield
        finally:
            self.timeout = saved

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _trace(self, level, msg):
        if self.verbose >= level:
            print(msg, file=sys.stderr, flush=True)

    def _bulk(self, ep, buf, length):
        return fcntl.ioctl(self.fd, USBDEVFS_BULK,
                           BulkTransfer(ep=ep, len=length, timeout=self.timeout, data=ctypes.addressof(buf)))

    def _clear_halt(self, ep):
        fcntl.ioctl(self.fd, USBDEVFS_CLEAR_HALT, struct.pack("I", ep))

    def _cbw(self, cdb, length, flags):
        if not 1 <= len(cdb) <= 16:
            raise BotError("CDB must be 1..16 bytes")
        tag, self.tag = self.tag, self.tag + 1
        cbw = struct.pack("<IIIBBB16s", CBW_SIG, tag, length, flags, 0, len(cdb), cdb)
        self._bulk(self.ep_out, ctypes.create_string_buffer(cbw, len(cbw)), len(cbw))
        return tag

    def _csw(self, tag):
        csw = ctypes.create_string_buffer(13)
        try:
            n = self._bulk(self.ep_in, csw, 13)
        except OSError as e:
            if e.errno != errno.EPIPE:
                self._trace(1, f"   CSW errno {e.errno} (device may have re-enumerated)")
                raise
            self._clear_halt(self.ep_in)
            n = self._bulk(self.ep_in, csw, 13)
        sig, rtag, residue, status = struct.unpack("<IIIB", csw.raw[:13])
        if n != 13 or sig != CSW_SIG or rtag != tag:
            raise BotError(f"bad CSW (len={n} sig={sig:#x} tag={rtag} want {tag})")
        return status

    def _check(self, cdb, status):
        if status == 1:
            raise CommandFailed(cdb, self.read_command(SENSE, 18)[0])
        if status != 0:
            raise BotError(f"cdb {cdb.hex(' ')}: phase error (CSW status {status})")

    def read_command(self, cdb, length):
        """Data-in command: (data, CSW status)."""
        tag = self._cbw(cdb, length, 0x80)
        self._trace(1, f"-> IN  {cdb.hex(' ')} want={length}")
        data = b""
        if length:
            buf = ctypes.create_string_buffer(length)
            try:
                n = self._bulk(self.ep_in, buf, length)
                data = buf.raw[:n]
            except OSError as e:
                if e.errno != errno.EPIPE:                 # stalled data phase: clear and read the CSW
                    raise
                self._clear_halt(self.ep_in)
            self._trace(2, f"   IN  {len(data)} B: {data[:64].hex(' ')}")
        return data, self._csw(tag)

    def read(self, cdb, length):
        data, status = self.read_command(cdb, length)
        self._check(cdb, status)
        return data

    def write(self, cdb, data):
        tag = self._cbw(cdb, len(data), 0x00)
        self._trace(1, f"-> OUT {cdb.hex(' ')} send={len(data)}")
        if data:
            try:
                self._bulk(self.ep_out, ctypes.create_string_buffer(bytes(data), len(data)), len(data))
            except OSError as e:
                if e.errno != errno.EPIPE:
                    raise
                self._clear_halt(self.ep_out)
        self._check(cdb, self._csw(tag))

    # -- named commands --
    def cmd(self, name, data=None):
        """Send a COMMANDS entry; returns the reply for data-in commands."""
        c = COMMANDS[name]
        if c.dir == "out":
            return self.write(c.cdb, data)
        return self.read(c.cdb, c.length if c.dir == "in" else 0)

    def send(self, op, reads=None):
        """Send one Op (payloads resolved from `reads`); returns IN data."""
        t = timeout_ms(op.cdb)
        with self.timeout_ms(t or self.timeout):
            if op.direction == "out":
                return self.write(op.cdb, op.data(reads) or b"")
            return self.read(op.cdb, read_len(op.cdb) if op.direction == "in" else 0)


# == SYSFS ==
def usb_ids(blockdev):
    """(vid, pid, product) of the USB device under /sys/block/<dev>, or None."""
    path = os.path.realpath(f"/sys/block/{blockdev}/device")
    while path != "/":
        if os.path.exists(os.path.join(path, "idVendor")):
            rd = lambda f: open(os.path.join(path, f)).read().strip() if os.path.exists(os.path.join(path, f)) else ""
            return rd("idVendor"), rd("idProduct"), rd("product")
        path = os.path.dirname(path)
    return None


def usb_disks():
    for dev in sorted(os.listdir("/sys/block")):
        ids = usb_ids(dev)
        if ids:
            yield (dev, *ids)


def usb_port(blockdev):
    """USB port path ('1-8') of a block device; read before a session detaches it."""
    ports = re.findall(r"/(\d+-[\d.]+)(?=/)", os.path.realpath(f"/sys/block/{blockdev}/device"))
    return ports[-1] if ports else None


def usb_id(port):
    d = f"/sys/bus/usb/devices/{port}"
    try:
        return f"{open(d + '/idVendor').read().strip()}:{open(d + '/idProduct').read().strip()}"
    except OSError:
        return None


def find_interface(blockdev):
    path = os.path.realpath(f"/sys/block/{blockdev}/device")
    while path != "/":
        if os.path.exists(os.path.join(path, "bInterfaceClass")):
            return path
        path = os.path.dirname(path)
    raise BotError(f"/dev/{blockdev} is not on a USB interface")


def mounted_partitions(blockdev):
    with open("/proc/mounts") as f:
        return [ln.split()[0] for ln in f if ln.split()[0].startswith(f"/dev/{blockdev}")]


def blockdev_on_port(port):
    for name in sorted(os.listdir("/sys/block")):
        if name.startswith("sd") and usb_port(name) == port:
            return name
    return None


def reset_port(port):
    """USB bus reset (re-enumeration, not a power cycle: controller RAM may survive)."""
    d = f"/sys/bus/usb/devices/{port}"
    bus, dev = (int(open(f"{d}/{n}").read()) for n in ("busnum", "devnum"))
    fd = os.open(f"/dev/bus/usb/{bus:03d}/{dev:03d}", os.O_RDWR)
    try:
        fcntl.ioctl(fd, USBDEVFS_RESET, 0)
    except OSError as e:
        if e.errno != errno.ENODEV:                        # re-enumerated mid-reset
            raise
    finally:
        os.close(fd)


def wait_for_rom_mode(port, timeout, settle=10):
    """The sdX of the stick once it enumerates on `port` in ROM mode and stays `settle` s
    (releasing the pin short can make it re-enumerate once more), or None."""
    deadline, seen, since = time.monotonic() + timeout, None, None
    while time.monotonic() < deadline:
        uid = usb_id(port)
        bd = blockdev_on_port(port) if uid == f700.ROM_MODE_ID else None
        try:
            devnum = open(f"/sys/bus/usb/devices/{port}/devnum").read().strip()
        except OSError:
            devnum = None
        if (uid, devnum, bd) != seen:
            print(f"  port {port}: {uid or 'nothing'}", flush=True)
            seen, since = (uid, devnum, bd), time.monotonic()
        if bd and time.monotonic() - since >= settle:
            return bd
        time.sleep(0.5)
    return None


def reopen_on_port(port, timeout=25):
    """A fresh session once the stick is back on `port` after a reset, or None."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        bd = blockdev_on_port(port)
        if bd:
            try:
                return Session(bd)
            except (BotError, OSError):
                pass
        time.sleep(0.5)
    return None


# == PROGRESS ==
class Progress:
    """`[34.2%] what...` on one updating line (tty), one line per percent otherwise;
    log() prints a normal line without leaving progress debris."""

    def __init__(self, what):
        self.what, self.tty, self.last = what, sys.stderr.isatty(), -1.0

    def update(self, done, total, what=None):
        pct = 100.0 * done / total if total else 100.0
        changed = what is not None and what != self.what
        self.what = what or self.what
        if pct - self.last >= (0.1 if self.tty else 1.0) or done == total or changed:
            self.last = pct
            if self.tty:
                sys.stderr.write(f"\r\033[K[{pct:5.1f}%] {self.what}...")
                sys.stderr.flush()
            else:
                print(f"[{pct:5.1f}%] {self.what}...", flush=True)

    def log(self, msg):
        if self.tty:
            sys.stderr.write("\r\033[K")
        print(msg, flush=True)

    def done(self, msg=None):
        if self.tty:
            sys.stderr.write("\r\033[K")
        print(msg or f"[100.0%] {self.what}: done", flush=True)


# == READ-ONLY QUERIES ==
def read(s, name):
    return s.read(READS[name].cdb, READS[name].length)


def firmware_locked(s):
    """True when the firmware refuses the FA command set (FA 0E -> sense 5/26/00), as one
    that mounted an erased info record does. Recovery is the ROM mode."""
    try:
        read(s, "info")
        return False
    except CommandFailed as e:
        return len(e.sense) >= 14 and e.sense[2] & 0xF == 0x5 and e.sense[12] == 0x26


def probe_controller(s):
    """Read-only compatibility check (9A identify and the 82 51 01 config read only): an
    AlcorMP controller answers 9A with a plausible chip ID and carries a 99 07 config
    block. The first read after a vendor command can be zeros or stale, so retry."""
    def attempt(name, ok):
        last = None
        for _ in range(3):
            try:
                last = read(s, name)
            except (BotError, OSError) as e:
                return None, str(e)
            if ok(last):
                return last, None
        return last, "unexpected response"
    notes, chip = [], None
    ident, err = attempt("ident", any)
    if ident is None or len(ident) < 0x2D:
        notes.append(f"vendor identify (9A) failed: {err or f'{len(ident)} bytes'}")
    else:
        chip = (ident[4] << 8) | ident[5]
        notes.append(f"9A chip ID {chip:04X}" + (" is not plausible" if chip in (0, 0xFFFF) else ""))
        if chip in (0, 0xFFFF):
            chip = None
    cfg, err = attempt("config", lambda d: d[:2] == b"\x99\x07")
    cfg_ok = bool(cfg) and cfg[:2] == b"\x99\x07"
    notes.append("carries a 99 07 AlcorMP config block" if cfg_ok else
                 f"config read (82 51 01) failed: {err}" if cfg is None else
                 f"no 99 07 config signature (got {cfg[:2].hex(' ')})")
    return {"ok": chip is not None and cfg_ok, "chip": chip, "notes": notes}


def flash_id(s, chip=None):
    """The CE0 flash ID (6 B); the 8-B form for chip >= F500."""
    return read(s, "flashid8" if chip is None or chip >= 0xF500 else "flashid")[:6]


def read_code(s, size=0x10000, start=0, log=None):
    """`size` bytes of 8051 CODE space from `start` (FA 10); unreadable chunks are zeros."""
    sectors, image, addr = 0x10, bytearray(), start & 0xFF00
    chunk = sectors * 512
    while len(image) < size and addr < 0x10000:
        try:
            data = s.read(code_cdb(addr, sectors), chunk)
        except (BotError, OSError) as e:
            data = b""
            if log:
                log(f"  @{addr:#06x}  FAILED ({e}); filling with 00")
        image += (data + bytes(chunk))[:chunk]
        addr += chunk
    return bytes(image[:size])


def disk_geometry(s):
    """(sector count, sector size) from READ CAPACITY(10)."""
    cap = read(s, "capacity")
    return int.from_bytes(cap[:4], "big") + 1, int.from_bytes(cap[4:8], "big")


DISK_CHUNK = 512                        # sectors per READ(10)/WRITE(10) (256 KiB)


def _rw10(op, lba, n):
    return bytes([op, 0]) + lba.to_bytes(4, "big") + b"\0" + n.to_bytes(2, "big") + b"\0"


def write_accepted(s, lba):
    """Write sector `lba` back with its own content (no change either way): True when the
    controller accepts it, False when it refuses it as write-protected (sense 7/27)."""
    bs = disk_geometry(s)[1]
    data = s.read(_rw10(0x28, lba, 1), bs)
    try:
        s.write(_rw10(0x2A, lba, 1), data)
        return True
    except CommandFailed as e:
        if len(e.sense) >= 13 and e.sense[2] & 0xF == 0x7 and e.sense[12] == 0x27:
            return False
        raise


def read_disk(s, path):
    """Every LBA into a sparse image; (sectors, sector size, sha256)."""
    import hashlib
    total, bs = disk_geometry(s)
    h, prog = hashlib.sha256(), Progress("reading disk")
    with open(path, "wb") as f:
        for lba in range(0, total, DISK_CHUNK):
            n = min(DISK_CHUNK, total - lba)
            data = s.read(_rw10(0x28, lba, n), n * bs)
            if len(data) != n * bs:
                raise BotError(f"short read at LBA {lba}: {len(data)} of {n * bs} B")
            h.update(data)
            if any(data):
                f.seek(lba * bs)
                f.write(data)
            prog.update(lba + n, total)
        f.truncate(total * bs)
    prog.done()
    return total, bs, h.hexdigest()


def write_disk(s, path, skip_zero=False):
    """An image back to every LBA; skip_zero leaves zero chunks unwritten (a blank target)."""
    total, bs = disk_geometry(s)
    if os.path.getsize(path) != total * bs:
        raise SystemExit(f"{path} is {os.path.getsize(path)} B; this disk is {total * bs} B")
    prog = Progress("writing disk")
    with open(path, "rb") as f:
        for lba in range(0, total, DISK_CHUNK):
            n = min(DISK_CHUNK, total - lba)
            data = f.read(n * bs)
            if any(data) or not skip_zero:
                s.write(_rw10(0x2A, lba, n), data)
            prog.update(lba + n, total)
    prog.done()


def write_and_verify(s, write, data, readback, check, timeout=3000):
    """Write a 512-B block, then read it back until check(reply). Returns (status, reply):
    verified | pending-replug (left the bus, as an identity write can) | mismatch |
    failed (rejected) | unknown (transport error, device still there)."""
    try:
        s.cmd(write, data)
    except CommandFailed as e:
        print(f"write REJECTED by the controller: {e}")
        return "failed", None
    except (BotError, OSError) as e:
        return ("pending-replug", None) if not s.alive() else ("unknown", None)
    reply = None
    with s.timeout_ms(timeout):
        for _ in range(3):                                 # the first read can be stale
            try:
                reply = s.cmd(readback)
            except (BotError, OSError):
                return ("pending-replug", None) if not s.alive() else ("unknown", None)
            if check(reply):
                return "verified", reply
    return "mismatch", reply


# == MP SESSION ==
class MpSession:
    """A session in MP mode, tracking the resident RAM loader: an FA 0B op is sent only
    under a loader that serves it (f700.LOADERS). `with mp.loader(role):` uploads one;
    leaving the block forgets it, so the next FA 0B op needs a loader named again."""

    def __init__(self, s, geo, strict=True):
        self.s, self.geo, self.resident, self.strict, self._noted = s, geo, None, strict, set()

    def enable(self):
        """Write-phase MP enable (RAM only): FA 1D, rwfw loader, FA 12/13/12, the A/B param
        pair, FA 0B 3E. Leaves rwfw resident."""
        from .provision import enable_ops
        for op in enable_ops(self.geo):
            self.s.send(op)
        self.resident = "rwfw"
        return self

    @contextlib.contextmanager
    def loader(self, role, **stage):
        """Re-enable with `role`'s loader (and the stage's param, see provision.reenable_ops)."""
        from .provision import reenable_ops
        for op in reenable_ops(self.geo, role, **stage):
            self.s.send(op)
        self.resident = role
        try:
            yield self
        finally:
            self.resident = None

    def _check(self, sub):
        if self.resident is None or sub not in f700.LOADERS[self.resident][1]:
            msg = (f"FA 0B {sub:02x} ({SUBS[sub].name}) under "
                   f"{self.resident or 'no known'} loader; served by "
                   f"{', '.join(r for r, (_, subs) in f700.LOADERS.items() if sub in subs) or 'none'}")
            if self.strict:
                raise LoaderError(msg)
            if (sub, self.resident) not in self._noted:
                self._noted.add((sub, self.resident))
                print(f"NOTE: {msg} (sent anyway)")

    def nand(self, sub, block=0, page=0, sectors=0, region=b"\0\0\0\0", frame=None, data=None):
        """One FA 0B op and its status poll: (reply or None, verdict)."""
        self._check(sub)
        c = nand_cdb(sub, 0, block, page, sectors, region, frame)
        reply = self.s.write(c, data) if data is not None else self.s.read(c, read_len(c))
        return reply, poll_verdict(self.s.cmd("poll"))

    def erase(self, block):
        """Erase as AlcorMP does (mode 1, then mode 0); an error string or None."""
        for mode in (1, 0):
            reply, verdict = self.nand(0x11, block, mode << 8)
            if reply[0] != 0 or verdict != "ok":
                return f"erase mode {mode} failed: reply {reply[0]:#04x}, status {verdict}"
        return None


# == DRIVER ==
def drive(blockdev, port, ops, session=None, max_failures=200, max_consecutive=15, verbose=0):
    """Send `ops` in order, handling the FA 08 reset re-enumeration by port. A wedged
    endpoint rejects every following op, so a run of `max_consecutive` failures aborts
    too. Returns (sent, failures)."""
    s = session or Session(blockdev, verbose=verbose)
    sent = failures = consec = 0
    reads = {}                                             # Op.keep -> IN data, for Live payloads
    prog = Progress(ops[0].phase if ops else "")

    def fail(detail):
        nonlocal failures, consec
        failures, consec = failures + 1, consec + 1
        if failures <= 30 or failures % 25 == 0:
            prog.log(f"  FAIL #{failures} at op {sent + 1}: {detail}")
        return failures >= max_failures or consec >= max_consecutive

    def send(o):
        data = s.send(o, reads)
        if o.direction == "in" and o.keep:
            reads.setdefault(o.keep, []).append(data)

    try:
        for o in ops:
            prog.update(sent, len(ops), o.phase)
            if verbose:
                prog.log(f"  {sent:5} {o.phase:22} {o.cdb.hex(' ')}  {o.note}")
            abort = False
            if o.cdb[:2] == b"\xfa\x08":                   # reset: the stick re-enumerates
                for f in (lambda: send(o), s.close):
                    try:
                        f()
                    except (BotError, OSError):
                        pass
                time.sleep(2)
                s = reopen_on_port(port)
                if s is None:
                    prog.log("  device did not return after the FA 08 reset; stopping")
                    return sent, failures
                consec, sent = 0, sent + 1
                continue
            try:
                send(o)
                consec = 0
            except CommandFailed as e:
                abort = fail(f"rejected: {e}")
            except (BotError, OSError) as e:
                if s.alive():
                    abort = fail(f"{o.cdb[:4].hex(' ')} error (device alive): {e}")
                else:                                      # left the bus (e.g. after 81 00 FF)
                    prog.log(f"  device left the bus after {o.cdb[:2].hex(' ')}; reopening {port}")
                    try:
                        s.close()
                    except (BotError, OSError):
                        pass
                    s = reopen_on_port(port)
                    if s is None:
                        prog.log("  could not reopen; stopping")
                        break
                    try:
                        send(o)
                        consec = 0
                    except (BotError, OSError) as e2:
                        abort = fail(f"resend after reopen failed: {e2}")
            if abort:
                prog.log(f"  aborting: {failures} failures ({consec} consecutive), endpoint "
                         f"likely wedged at op {sent + 1}")
                break
            sent += 1
        prog.done(f"sent {sent}/{len(ops)} ops, {failures} failures")
    finally:
        if os.environ.get("ALCOR_KEEP_DUMP"):              # dev: the live reads, for re/
            import pickle
            with open(os.environ["ALCOR_KEEP_DUMP"], "wb") as f:
                pickle.dump(reads, f)
        if s is not None:
            try:
                s.close()
            except (BotError, OSError):
                pass
    return sent, failures
