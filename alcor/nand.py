"""AlcorMP data (tables and images) and the NAND geometry a FlashList record describes.

The data lives in releases, <data dir>/<version>/, written by alcormp-extract (run it first):
  flashlist.tsv           flash ID -> AlcorMP's 662-B FlashList record
  ecc_params.tsv          BCH ECC parameter records
  LLF.dll                 AlcorMP's test pattern (every test/scan/fill page is a slice of it)
  firmware/<api>/<ctl>/   AlcorMP's image tree: RAM loaders, boot (_CB) images, INIs
Every lookup takes the first release that has the item, newest version name first, across
all of DATA_DIRS. Formats: docs/tools.md.
"""
import glob
import os
import re

from . import f700

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
XDG_DATA = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "alcortool")
DATA_DIRS = [*filter(None, os.environ.get("ALCORTOOL_DATA", "").split(os.pathsep)),
             os.path.join(REPO, "alcormp"), XDG_DATA, "/usr/local/share/alcortool", "/usr/share/alcortool"]
LLF_PATTERN_LEN = 0x180000


class DataError(Exception):
    """Missing AlcorMP data, or none for this part; never fall back to another part's."""

    def __str__(self):
        return str(self.args[0]) if self.args else ""


class DataMissing(DataError):
    pass


class UnknownPart(DataError, KeyError):
    pass


class ImageNotBundled(DataError):
    pass


# == RELEASES ==
def releases():
    """Imported release directories, newest version name first."""
    found = {}
    for d in DATA_DIRS:
        for name in sorted(os.listdir(d)) if os.path.isdir(d) else ():
            path = os.path.join(d, name)
            if os.path.isdir(path) and not name.startswith(".") and not name.endswith(".importing"):
                found.setdefault(name, path)                # an earlier data dir wins a tie
    if not found:
        raise DataMissing("no AlcorMP data found (looked in " + ", ".join(DATA_DIRS) + "): "
                          "extract an AlcorMP release first: alcortool extract <AlcorMP archive or folder>")
    return [found[n] for n in sorted(found, reverse=True)]


def data_file(name):
    """The newest release's copy of a top-level data file."""
    for r in releases():
        if os.path.exists(os.path.join(r, name)):
            return os.path.join(r, name)
    raise DataMissing(f"no imported release has {name}")


# == TABLES ==
_tables = {}


def _table(path):
    if path not in _tables:
        with open(path) as f:
            lines = [ln.rstrip("\n").split("\t") for ln in f if not ln.startswith("#")]
        _tables[path] = [dict(zip(lines[0], row)) for row in lines[1:]]
    return _tables[path]


def _rows(name):
    """Rows of a table in every release that has it, newest first."""
    for r in releases():
        if os.path.exists(os.path.join(r, name)):
            yield from _table(os.path.join(r, name))


def norm_fid(flash_id):
    """'2c 84 44 32 aa 04' from bytes or any spacing/case of hex."""
    if isinstance(flash_id, (bytes, bytearray)):
        return " ".join(f"{b:02x}" for b in flash_id)
    h = re.sub(r"[^0-9a-fA-F]", "", flash_id).lower()
    return " ".join(h[i:i + 2] for i in range(0, len(h), 2))


def flashlist_record(flash_id):
    """{flash_id, vendor, part, record (bytes), bins, boot_module} or None."""
    fid = norm_fid(flash_id)
    for row in _rows("flashlist.tsv"):
        if row["flash_id"] == fid:
            rec = bytes.fromhex(row["record"])
            bins = [x.decode() for x in re.findall(rb"[\x20-\x7e]{3,}", rec[0x30:])
                    if x.upper().endswith(b".BIN")]
            return {"flash_id": fid, "vendor": row["vendor"], "part": row["part"], "record": rec,
                    "bins": bins, "boot_module": rec[0x256:0x276].split(b"\0")[0].decode("latin1")}
    return None


def ecc_param_record(t, family=f700.ECC_FAMILY):
    """The BCH parameter record for strength `t` (key = mode, codeword, family, t)."""
    key = bytes((*family, t)).hex(" ")
    for row in _rows("ecc_params.tsv"):
        if row["key"] == key:
            return bytes.fromhex(row["record"])
    raise DataError(f"no ECC parameter record {key} in the imported data")


_llf = {}


def llf_pattern(off, length, complement=False):
    """`length` bytes of the LLF test pattern (or its complement) from `off`."""
    if not _llf:
        path = data_file("LLF.dll")
        with open(path, "rb") as f:
            p = f.read(LLF_PATTERN_LEN)
        if len(p) != LLF_PATTERN_LEN:
            raise DataError(f"{path}: {len(p)} B, expected {LLF_PATTERN_LEN}")
        _llf[False], _llf[True] = p, bytes(b ^ 0xFF for b in p)
    if off + length > LLF_PATTERN_LEN:
        raise ValueError(f"LLF pattern slice {off:#x}+{length:#x} out of range")
    return _llf[bool(complement)][off:off + length]


# == IMAGES ==
_images = {}


def images():
    """{lower-case basename: path} of every file in the controller's image trees, the
    newest release's copy winning."""
    if not _images:
        for r in reversed(releases()):
            root = os.path.join(r, "firmware", *f700.FIRMWARE_TREE)
            for p in glob.glob(os.path.join(root, "**", "*"), recursive=True):
                if os.path.isfile(p):
                    _images[os.path.basename(p).lower()] = p
        if not _images:
            raise DataMissing(f"no imported release has a firmware/{'/'.join(f700.FIRMWARE_TREE)}/ image tree")
    return _images


def find_image(name):
    """Path of an imported image by basename (case-insensitive), or None."""
    return images().get(name.lower())


def hex_decode(raw):
    """AlcorMP's hex-text image (ASCII hex, any whitespace) as bytes."""
    return bytes.fromhex(re.sub(rb"[^0-9A-Fa-f]", b"", raw).decode())


def load_image(name):
    path = find_image(name)
    if not path:
        raise ImageNotBundled(f"image {name} is not in the imported data")
    return hex_decode(open(path, "rb").read())


def part_record(flash_id):
    rec = flashlist_record(flash_id)
    if not rec:
        raise UnknownPart(f"flash ID {norm_fid(flash_id)} is not in the imported FlashList "
                          "(import a newer AlcorMP release, or add the part: docs/tools.md)")
    return rec


def boot_fw_image(flash_id):
    """Name of the boot (_CB) image for a part: the one *_<token>_CB.BIN matching its
    record's boot-module token (e.g. 10_L04A -> 36_L04A_CB.BIN)."""
    rec = part_record(flash_id)
    if not rec["boot_module"]:
        raise ImageNotBundled(f"{rec['vendor']} {rec['part']}: its FlashList record names no boot module, "
                              "so there is no boot firmware to pair it with (not supported)")
    token = rec["boot_module"].split("_", 1)[-1]
    hits = sorted(os.path.basename(p) for n, p in images().items()
                  if re.fullmatch(r"\d+_%s_cb\.bin" % re.escape(token.lower()), n))
    if len(hits) != 1:
        raise ImageNotBundled(f"{rec['vendor']} {rec['part']} (boot module {rec['boot_module']}): "
                              f"{len(hits)} imported *_{token}_CB.BIN images {hits or ''}".rstrip())
    return hits[0]


def device_bootfw(flash_id):
    return load_image(boot_fw_image(flash_id))


def identify_boot_fw(code, window=16):
    """(imported _CB image, fraction of `code`'s non-blank 16-B windows found in it);
    (None, 0.0) without imported data."""
    wins = {code[i:i + window] for i in range(0, len(code) - window, window)
            if len(set(code[i:i + window])) > 2}
    best = (None, 0.0)
    try:
        cbs = [(n, p) for n, p in images().items() if n.endswith("_cb.bin")]
    except DataMissing:
        return best
    for _, path in cbs:
        img = hex_decode(open(path, "rb").read())
        frac = sum(1 for w in wins if w in img) / max(len(wins), 1)
        best = max(best, (os.path.basename(path), frac), key=lambda x: x[1])
    return best


def module_name(flash_id):
    """The part's device-firmware module: the first of its record's .BIN names on disk
    (not the boot image; it keys the [MODULE_FETURE] line)."""
    rec = part_record(flash_id)
    for name in rec["bins"]:
        if find_image(name):
            return name
    raise ImageNotBundled(f"none of {rec['bins']} is in the imported data")


def module_feature(name):
    """The [MODULE_FETURE] fields of a module in the imported FlashList.ini (strings)."""
    path = find_image("FlashList.ini")
    section = None
    for line in open(path, encoding="latin1"):
        line = line.split(";")[0].strip()
        if line.startswith("["):
            section = line.strip("[]")
        elif section == "MODULE_FETURE" and "=" in line:
            key, val = (s.strip() for s in line.split("=", 1))
            if key.upper() == name.upper():
                return [v.strip() for v in val.split(",")]
    raise DataError(f"{name}: no [MODULE_FETURE] line in {path}")


def loader_image(role, patch):
    """A RAM loader as uploaded: the role's image, first LOADER_UPLOAD bytes, the patch
    at LOADER_PATCH_AT."""
    img = bytearray(load_image(f700.LOADERS[role][0]))
    img[f700.LOADER_PATCH_AT:f700.LOADER_PATCH_AT + len(patch)] = patch
    return bytes(img[:f700.LOADER_UPLOAD])


# == GEOMETRY ==
class Geometry:
    """A part's geometry from its FlashList record (record 0x36, 26 B). CE count and
    plane loop count are the controller's runtime auto-detect; their defaults are what
    AlcorMP detects on F700 (planes is its loop count, not the die's planes)."""

    def __init__(self, flash_id, ce=1, planes=2):
        rec = part_record(flash_id)
        full = rec["record"]
        g = self.raw = full[0x36:0x36 + 26]
        self.fid, self.part, self.vendor = rec["flash_id"], rec["part"], rec["vendor"]
        self.cell_type = g[0]                                   # 0/1/2 = SLC/MLC/TLC
        self.capacity_mib = int.from_bytes(g[4:8], "little")
        self.spare_per_page = int.from_bytes(g[9:11], "little")
        self.planes_static = g[12]
        self.blocks = int.from_bytes(g[13:15], "little")        # blocks per CE (0x890)
        self.block_span = int.from_bytes(g[15:17], "little")    # next power of two (0x1000)
        self.pages_per_block = int.from_bytes(g[19:21], "little")
        self.sectors_per_page = g[21]
        self.interleave_class = g[23]
        self.ecc_code = full[0x51]
        self.page_bytes = self.sectors_per_page * 512
        self.raw_page_span = self.page_bytes + self.spare_per_page
        self.program_passes = 2 if self.ecc_code == 0xA2 else 1
        self.ce_count, self.planes = ce, planes
        # lanes (FTL interleave), FTL slots, superblock stride
        il = {1: 2, 2: 1, 3: 4 if self.blocks < 0x1000 else 2}.get(self.interleave_class, 1)
        while il > 1 and ce * planes * il * ((self.blocks // il + 0x7FF) >> 11) >= 0x41:
            il //= 2
        self.lanes = il
        self.ftl_slots = ce * planes * il * self.program_passes
        self.cell_slices = {0: 1, 1: 2, 2: planes << 1, 3: 4}.get(self.interleave_class, 1)
        self.superblock_stride = ((self.blocks // il // 9) | 1) * il if il else 0
        self.lane_blocks = self.blocks * ce // il
        self.nand_attr = f700.NAND_ATTR if self.ecc_code in (0x16, 0x17) else 0x00

    def warnings(self):
        """Fields outside what the F700 parser was checked on."""
        checks = [(self.sectors_per_page in (16, 32, 64), f"sectors/page {self.sectors_per_page}"),
                  (self.pages_per_block and not self.pages_per_block & (self.pages_per_block - 1),
                   f"pages/block {self.pages_per_block}"),
                  (0 < self.spare_per_page <= 4096, f"spare {self.spare_per_page}"),
                  (self.cell_type in (0, 1, 2), f"cell type {self.cell_type}"),
                  (self.ecc_code in (0x16, 0x17, 0xA2), f"ECC code {self.ecc_code:#x}")]
        return [f"unchecked {what}" for ok, what in checks if not ok]

    def describe(self):
        return (f"{self.fid}  {self.vendor} {self.part}: {self.page_bytes // 1024} KB pages "
                f"+ {self.spare_per_page} spare, {self.pages_per_block} pages/block, "
                f"{self.blocks:#x} blocks, {self.lanes} lanes, ECC code {self.ecc_code:#x}"
                + "".join(f"\n  NOTE {w}" for w in self.warnings()))
