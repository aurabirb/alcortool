"""F700 (CTL-28, UfdApi_Gen) datums: every controller-specific constant the tool sends.

A new controller is a copy of this file with its own values (and an image tree, docs/tools.md).
Sections are headed `# == NAME ==` so they can be searched. The meaning of each table is
in docs/ (docs/llf.md for the stream, docs/controllers/F700.md for the values);
values are DERIVED/VERIFIED against AlcorMP's two F700 captures unless marked INFERRED.
"""

# == CONTROLLER ==
CHIP_ID = 0xF700
USB_VID = "058f"
ROM_MODE_ID = "058f:1234"              # boot ROM, no firmware loaded from NAND
FIRMWARE_TREE = ("Gen", "28")           # <release>/firmware/<api>/<ctl> (docs/tools.md)

# == RAM LOADERS ==
# role: (image under SCAN_BIN, FA 0B subs it serves). The subs are the ones AlcorMP sends
# under each loader; the stream builder refuses any other (provision.Stream.nand).
LOADERS = {
    "rwfw":    ("28_LB_RWFW.BIN",
                {0x01, 0x06, 0x08, 0x09, 0x11, 0x18, 0x1E, 0x1F, 0x3E, 0x45, 0x46, 0x94, 0x96}),
    "sort":    ("28_LB_SORT_3D.BIN",
                {0x01, 0x03, 0x06, 0x11, 0x18, 0x19, 0x3A, 0x42, 0x43, 0x47, 0x94, 0x96, 0x99, 0x9B}),
    "bct":     ("28_LB_BCT.BIN",
                {0x01, 0x03, 0x06, 0x11, 0x90, 0x91, 0x92, 0x94, 0x96, 0x99, 0x9A, 0xAE, 0xAF}),
    "scan":    ("28_LB_SCAN_TOS_3D.BIN",
                {0x01, 0x03, 0x04, 0x05, 0x06, 0x11, 0x94, 0x96, 0x97, 0x99, 0xA0, 0xA1, 0xA2,
                 0xB0, 0xB1}),
    "scan_rd": ("28_LB_SCAN_TOS_READ_N.BIN", {0x01, 0x03, 0x04, 0x06, 0x94, 0x96, 0x99, 0xC0, 0xC1}),
    "scan_n":  ("28_LB_SCAN_N.BIN", {0x01, 0x04, 0x06, 0xA7}),
    "hlf":     ("28_LB_HLF.BIN", {0x01, 0x06, 0x21, 0x22}),
}
LOADER_UPLOAD = 9216                    # FA 0A 00 10 sends the image's first 9216 B
LOADER_PATCH_AT = 0x2000                # ... with the 818-B patch here (zero in the image)
LOADER_PATCH_LEN = 818
LOADER_BASE = 0xC000                    # where the upload's first 0x2000 bytes run (hook slots)

# == OWN LOADERS (alcor/loaders/, assembled by alcor/asm51.py) ==
# role: source. They get the patch constants and these fields only: the ROM stores the
# geometry fields in registers for the NAND code, which these loaders do not have.
OWN_LOADERS = {"probe": "probe.a51"}
CAPTURE_LOADER = "capture.a51"          # stub over the stick's own page0 (BOOT FIRMWARE CAPTURE)
OWN_PATCH = {0x06: 0x01}                # one CE; used when the stick's own set-up is not read

# The patch window from the stick itself. The ROM loads at boot (CODE:45F1) the NAND set-up an
# upload loads from its patch window (CODE:2E15), from XDATA the firmware keeps: patch offset ->
# XDATA address (FA 13 reads it in normal mode). 0x100-0x1EF is copied as a block; 0x00 is the
# live mode register; the rest are the internal-RAM fields (INTMEM AD, 12/13, AE, 14, 9D/9E,
# 9F/A0, 10/11) and the 0x800B mode (0x11; boot applies 0x71A9, then 0x71A8, which stays).
PATCH_FROM_STICK = {0x00: 0x800A, 0x02: 0x71F2, 0x04: 0x71F4, 0x05: 0x71F5, 0x06: 0x71F6,
                    0x08: 0x71F8, 0x0A: 0x71FA, 0x0B: 0x71FB, 0x0C: 0x71FC, 0x0D: 0x71FD,
                    0x0E: 0x71F0, 0x0F: 0x71F1, 0x11: 0x71A8,
                    **{off: 0x7000 + off for off in range(0x100, 0x1F0)}}
REG_PAGES = (0x80, 0x90)                # XDATA register pages: NAND controller, peripherals

# == LOADER PATCH (FA 0A, the 0x2000 window) ==
# Constant header bytes; the geometry and ECC fields are set by payloads.build_loader_patch.
LOADER_PATCH_CONST = {
    0x00: 0x10, 0x02: 0x10, 0x03: 0x02, 0x0D: 0xF6, 0x0E: 0x40, 0x10: 0x50, 0x11: 0x10,
    0x100: 0xA0, 0x105: 0xFF, 0x10F: 0x38, 0x111: 0x20, 0x113: 0x21, 0x115: 0x22, 0x117: 0x23,
    0x194: 0x33, 0x1A8: 0x10, 0x1EE: 0x01, 0x1EF: 0x0F, 0x1FE: 0x4A, 0x1FF: 0x4E,
}
# Edits per MP step on top of geometry + ECC mode; meanings unresolved, names say when.
LOADER_EDITS = {
    "boundary":     {0x1EE: 0x04, 0x1EF: 0x00},                   # opens a boundary-scan group
    "housekeeping": {0x1EF: 0xFF},                                # housekeeping sweeps .. ae/af
    "fw":           {0x0A: 0x10, 0x0C: 0x0F, 0x105: 0x00,         # tail: firmware/data writes
                     0x119: 0x24, 0x11B: 0x25, 0x11D: 0x26, 0x11F: 0x27},
    "fw_data":      {0x1ED: 0x3F, 0x1EE: 0x08, 0x1EF: 0xFF},       # sysrec / data-superblock groups
    "final":        {0x105: 0x00},                                # the closing upload
}

# == ECC ==
# BCH strength t: param 0x2e row / 0x4c, loader 0x195 = t-2 and 0x1b0 = row low byte.
# AlcorMP drops to t=0x48 for the address march and boundary loop B.
ECC_DEFAULT = 0x4E
ECC_MODES = {
    0x4E: {"row": 0x048A, "p4c": 0x27},
    0x48: {"row": 0x0480, "p4c": 0x24},
}
ECC_FAMILY = (0x00, 0x04, 0x02)         # ecc_params.tsv key: mode, codeword, family (+ t)
ECC_TABLE_LEN = 0x132                   # loader patch tail = the record's first 0x132 B
ECC_ROW_512 = 0x28A                     # frame of t=0x4e over 512-B sectors
# raw ECC frame bytes -> data bytes in it (the rest is BCH parity); system pages use 512-B
# frames (VERIFIED: block 0 page 2 = the boot firmware, data at 0, 650, 1300, ...)
ECC_FRAMES = {ECC_ROW_512: 512, ECC_MODES[0x4E]["row"]: 1024, ECC_MODES[0x48]["row"]: 1024}

# == PARAM BLOCK (FA 0B 01, 2048 B) ==
# Byte tables are {offset: "hex bytes"}. The block is built in this order
# (payloads.build_param): geometry fields, PARAM_BASE, the FTL page map, the stage's
# bytes (STAGES), the call's tokens (PARAM_TOKENS), then the slack rule.
#
# Geometry fields: 0x00 CE count, 0x01 block span, 0x03/0x13/0x25/0x41 pages per block,
# 0x05/0x15 sectors per page, 0x06 planes, 0x20 log2(pages per block), 0x21/0x30/0x32
# geometry record bytes 22/8/24, 0x80 the page-quarter table, 0xD0 the sector index map,
# 0x2E/0x31/0x4C the ECC mode (ECC_MODES), 0x10/0x22 the unit count after MP enable.
PARAM_BASE = {
    0x0E: "38", 0x11: "0400", 0x1D: "80", 0x1F: "01", 0x22: "01", 0x2C: "40", 0x2D: "02",
    0x37: "02", 0x38: "0d", 0x3A: "0e", 0x3B: "0f", 0x3E: "13", 0x40: "01", 0x43: "01",
    0x47: "01", 0x4F: "18", 0x53: "01", 0x5B: "0f", 0x5D: "01", 0x62: "01", 0x63: "06",
    0x68: "ff", 0x6B: "01", 0x6F: "01", 0x70: "01", 0x71: "40",
    0x90: "0080 0091 00a0 00b1 00c0 00d1 00e0 00f1",           # page table of the B member
}
PARAM_NAND_ATTR = {0x59: "da", 0x5C: "df", 0x7A: "01"}          # for cell/ECC codes 0x16/0x17
# FTL page map: an identity map of n entries, n at 0x41 (BE16), low bytes from 0x1A0, the
# high 2 bits packed 4 per byte from 0x5A0 (payloads.ftl_map). n by the call's `map`:
#   None     pages per block                  (F700 512: 0x1A0-0x39F ramps, 0x5E0-0x61F 55)
#   "slc"    pages per block // 2, 0x13 = n   (F700 256: SLC uses half of each block)
#   "nomap"  0                                (the screen's param)
# The slack rule (`slack`): 0x17 = the raw page bytes the ECC frames leave over (BE16); a
# boundary slot's param subtracts the slot's measured bad columns and puts the slack at 0x66.
#
# Tokens a param call names (Stream.param("A", "scan", ...)):
PARAM_TOKENS = {
    "A":    {0x5D: "00", 0x90: "0036 00be 0146 0011 004f 0089 00c7 0101"},   # A member of the pair
    "B":    {},                                                               # B member (the base)
    "scan": {0x6B: "04"},                                                     # housekeeping sweeps
    "fw28": {0x15: "1c", 0x40: "00", 0xEC: "ffffffff"},                       # 28-sector page groups
    "rows": {0x2C: "200102"},                                                 # 512-B ECC frames (row 0x28a)
}

# == STAGES ==
# The controller state the format moves through, in order (provision.Format.stage). On top
# of the base block every param carries its stage's bytes; every re-enable's loader patch
# its loader edits (LOADER_EDITS); ECC is the BCH strength t of both (ECC_MODES); units:
# the param carries the post-enable unit count (cell slices) at 0x10 and 0x22.
STAGES = {  # name        param bytes                                           loader edits       ECC  units
    "enable":    ({},                                                          (),                0x4E, False),
    "scratch":   ({0x7A: "00"},                                                (),                0x4E, False),
    "ftl":       ({0x7A: "00"},                                                (),                0x4E, True),
    "march":     ({0x7A: "00", 0x3A: "11", 0x4F: "1e"},                        (),                0x48, True),
    "march_end": ({0x7A: "00", 0x3A: "11", 0x4F: "1e"},                        (),                0x4E, True),
    "sweeps":    ({0x7A: "00", 0x3A: "11", 0x4F: "1e"},                        ("housekeeping",), 0x4E, True),
    "tables":    ({0x7A: "00", 0x3A: "11", 0x4F: "1e", 0x6F: "00"},            ("housekeeping",), 0x4E, True),
    "tail":      ({0x7A: "00", 0x3A: "11", 0x4F: "1e", 0x6F: "00", 0x11: "10"}, ("fw",),           0x4E, True),
    "final":     ({0x7A: "00", 0x3A: "11", 0x4F: "1e", 0x6F: "00"},            (),                0x4E, True),
}

# == REGISTER BLOCKS (FA 12) ==
# Sent at every enable/re-enable; field builder not reversed, so verbatim. The two differ
# in their first 4 bytes only.
_FA12_BODY = bytes.fromhex(
    "0000000040405d0040405d00160000c00000000038405d0094cfdf04"
    "88ff6b76000000000000000040405d00d83c5d00000000000000000000000000"
    "2e08000000000000ffff0001ffffffff00000000d8cf0101d0cedf04d83c5d00"
    "ecd4df04fd433d77898a7c00feffffff7d303977752c3977000000007d146d76"
    "0001000000000000fccfdf04cf643977d4643977a58c9b730000000000010000"
    "7d146d76d4cfdf047d146d76ecd4df04fd433d7761937c00feffffff0cd0df04"
    "e3166b76020000000001000024d0df04ba136d76340000c00000000000000000"
    "000000004cd1df044e146d7638cbf10328cbf103340000c048d0df0400010000"
    "6f146d7602000000000000008cd0df0400000000b00e8b02e34a383140d1df04"
    "4803000000000000000000004803000000000000010000000000000000000000"
    "0000000000000000500000002c0000002c000000010008000000000000240000"
    "2c010000b00e8b0230000000fa0a001000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0a000000ecd4df04d985ef03ffffffff30d1df049157ee03d002000008000000"
    "fa0a00100000000000000000000000000000000000240000b00e8b022c010000"
    "fa0a0010000000000000000000000000f8d4df043ad4d903d0020000fa000000")
FA12_REGS = (bytes.fromhex("72020077") + _FA12_BODY, bytes.fromhex("9002a177") + _FA12_BODY)
FA12_CLOSE = bytes((0x00, 0x02, 0x00, 0x00, 0x05, 0x11, 0x00, 0x02, 0x01)).ljust(512, b"\x00")

# == NAND LAYOUT (blocks) ==
SYS_BLOCK, MIRROR_BLOCK = 0x000, 0x0B2  # boot firmware + FTL system copies
MIRROR_ANCHOR_PAGE = 0x88
SCRATCH_BLOCK = 0x58                    # scratch self-test (and `dev writetest`)
HK_BLOCK = 0x400                        # housekeeping tables (21/22)
BOUNDARY_BASES = (0x4C, 0x6D, 0x4E, 0x6F)   # boundary-scan base per FTL slot
BOUNDARY_SWEPT = (0x64, 0x85, 0x66, 0x87)   # loop B swept block per slot
SCAN_STRIDE = 0xC4                      # boundary groups: base + k*stride, 10 per slot
DATA_SUPERBLOCKS = (0x2EC, 0x1EA)       # one valid choice; AlcorMP's varies per run
DATA_FIRST = 0x100                      # lowest initial data block (INFERRED)
DS_HEAD_ENTRIES = (45, 40, 40, 40)      # initial data blocks per lane (INFERRED)
DS_HEAD_PAGES = 4                       # data-superblock pages carrying the FTL header

# == SCREEN ==
SCREEN_BLOCKS = 0x90                    # blocks 0x00-0x8f, page 1 write/verify

# == REGIONS (FA 0B CDB bytes 10-13) ==
R_ZERO = b"\x00\x00\x00\x00"
R_ONE = b"\x01\x00\x00\x00"             # FTL-unit build A columns
R_SYS = b"\xff\xff\x00\x00"             # system / firmware area
R_DATA = b"\x01\x5a\x00\x00"            # data / FTL pre-pass
R_FWCOPY = b"\xff\xff\x01\x00"          # firmware-copy pages
R_SCRATCH = b"\x00\x00\x01\x00"         # scratch block
R_SYSREC = b"\xff\xff\x00\x01"          # 45 system records / anchors
R_ANCHOR_PH = b"\xff\xff\x20\x02"       # 45 anchor placeholder (frame 8a)
R_HK = b"\x04\x00\x00\x00"              # 22 housekeeping read-back
R_C1 = b"\x00\x00\x01\x5a"              # c1 read sweep
FTL_PAGE = 0x48A0                       # page field of the 91/92/9a FTL-unit ops

# == FA 05 STATUS READS ==
# Counts in the tail, by position (both captures; caller not traced, INFERRED).
TAIL_STATUS_READS = {"housekeeping": 1, "walk": 1, "prepass": 3, "mirror": 9,
                     "data": 7, "data_mid": 1, "data_next": 1}

# == FTL ANCHOR (block 0 page 0, 0x80 B) ==
ANCHOR_MAGIC = bytes.fromhex("0d06050638")
ANCHOR_CONST = {0x0B: 0x2A, 0x0D: 0x33, 0x12: 0x38, 0x13: 0x10, 0x15: 0x37, 0x16: 0x01,
                0x1F: 0x24, 0x22: 0x01, 0x2A: 0x0C, 0x2B: 0x01, 0x2C: 0x08, 0x2D: 0x83,
                0x35: 0x30, 0x36: 0xAA, 0x37: 0xFF, 0x38: 0xFF}
ANCHOR_SELF = {False: (0x0F, 0x00, 0x00), True: (0x0E, 0x64, 0x01)}   # 0x32-0x34, by mirror
NAND_ATTR = 0xDA                        # anchor 0x2e for cell/ECC codes 0x16/0x17

# == FIRMWARE DESCRIPTOR (boot image @0xf14) ==
FW_DESC_HEAD = bytes.fromhex("080101000002000981000200")
FW_READ = (0x0E, b"\xff\xff\x20\x02", 0x8A)   # FA 0B 46 system-area read: sectors, region, frame

# == HOUSEKEEPING TABLES (FA 0B 21) ==
HK_RESERVE_BLOCKS = 12                  # planned-capacity reserve (INFERRED)
HK_HEADER_STATE = {0x80: 0x01, 0x81: 0xFF, 0x82: 0xFF, 0x85: 0x01, 0x8F: 0x80, 0x90: 0x05,
                   0x91: 0x01, 0x92: 0x01, 0x93: 0x20, 0x94: 0x18, 0x95: 0x02, 0x99: 0xFF,
                   0x9A: 0xFF, 0x9B: 0xFF, 0x9C: 0xFF, 0xA0: 0x60, 0xA4: 0x01, 0xA6: 0x12,
                   0xA9: 0xFF, 0xAF: 0x80, 0xB2: 0x14, 0xB3: 0x01, 0xB4: 0x01, 0xC4: 0x18,
                   0xC6: 0x10, 0xCB: 0x04, 0xCD: 0xA0, 0xE2: 0x33, 0xE3: 0x13, 0xE6: 0x01}
HK_INFO_HEAD = bytes.fromhex("aa0100000005041901")

# == SWEEP VERDICTS ==
SWEEP_FAILED = 0x9F                     # c0/c1 page value above this: the block failed
SWEEP_FAIL_MARK = 0xE0                  # ORed into a block a 05 report flagged
COLUMN_CAP = 0xFE                       # bad-column budget cap

# == CLOSING RECORDS ==
# Sector 4 of the record page (81 51 04): AlcorMP's 256-B state block at 0x32 (INFERRED
# constant), the format date and the capacity.
MP_INFO_BLOCK = {
    0x0B2: "01ffff", 0x0B7: "01", 0x0C1: "80050101201802", 0x0CB: "ffffffff", 0x0D2: "60",
    0x0D5: "01010012", 0x0DB: "ff", 0x0E1: "80f700140101", 0x0F6: "180010",
    0x0FB: "02000400a0", 0x112: "10003313", 0x118: "01", 0x11F: "40", 0x122: "ff" * 10,
}
MP_INFO_DATE, MP_INFO_CAPACITY = 0x12C, 0x132

# == CONFIG BLOCK (82 51 01 / 81 00 FF) ==
# AlcorMP's block with its default INI; written when the stick has none.
CONFIG_DEFAULT = {
    "device": bytes.fromhex("12011001000000088f058763000101020001"),   # USB device descriptor
    "usb": ("Generic", "Mass Storage"),                                # manufacturer, product
    "scsi": ("Generic", "Flash Disk", "8.00"),                         # INQUIRY vendor/product/rev
    "7c": b"\x64\x9b",
    "sizes": bytes.fromhex("0000000200000002"),                        # @0xC0
    "tag": b"PQI",                                                     # @0xD5, after a random byte
}

# == INFO RECORD (82 51 00 / 96 reply) ==
# offset: (tag, RAM source, meaning); values set by the host's 99 record.
RECORD_VALUES = {
    0x0A: (0xF1, "7268", "total capacity in sectors (9A bytes 0-3)"),
    0x11: (0xF2, "726C", "size of a second LUN/area (9A bytes 7-10); nonzero clears bit 5C"),
    0x16: (0xF3, "7270", "size of the area READ CAPACITY adds on the flash LUN unless 721C bit 0"),
    0x1B: (0xF4, "7278", "LBA bound of the accessible area (9A bytes 12-15; beyond: status 8A)"),
    0x21: (0xF5, "727C", "bound of the area refused (89) while record sector 3 is set (721A = B0)"),
    0x26: (0xF6, "7274", "flash LUN capacity (READ CAPACITY)"),
}
# offset: (firmware bit, F5 option byte that sets it, meaning)
RECORD_FLAGS = {
    0x2E: ("INTMEM 9A & 16", None, "bit 1 = policy write-protect; bit 0 of 9A is the lock gate's second input"),
    0x30: ("5F", 0x00, "[AutoRun] GPI (UFDMode 0x10 only)"),
    0x34: ("64", 0x03, "UFDMode bit 0"),
    0x36: ("55", None, "fixed disk with 56 (set from CDB[2] by vendor handler 6998)"),
    0x38: ("66", 0x04, "UFDMode bit 1: fixed disk (INQUIRY removable bit cleared)"),
    0x39: ("63", 0x05, "UFDMode bit 2"),
    0x3A: ("65", 0x06, "UFDMode bit 3: closes the vendor-command lock gate"),
    0x3B: ("67", 0x07, "UFDMode bit 4"),
    0x3F: ("5A", 0x0B, "[Other] LED"),
    0x40: ("60", 0x0F, "[AutoRun] Win98 == 0 (UFDMode 0x10 only)"),
    0x41: ("5C", None, "single area: 99 payload [4] = 'Q', or no second area (726C = 0)"),
    0x42: ("7203", 0x0A, "LED mode byte (F5[9] = 'Q')"),
    0x43: ("5E", 0x0E, "UFDMode 0x10"),
    0x44: ("61", 0x12, "[UFD] NoRemovable"),
    0x45: ("5D", 0x13, "UFDMode 0x10"),
    0x46: ("58", 0x14, "UFDMode 0x10 variant (alternative to 5D)"),
    0x47: ("50", 0x15, "ignore eject (START STOP UNIT LOEJ/START)"),
    0x48: ("70", 0x16, "write-protect the LUN while record sector 2 is set (7218 = B0)"),
}
RECORD_LANES = 0x100                    # BE16 usable blocks per FTL lane (from the 99 record)
RECORD_WP = 0x2E                        # its policy-WP byte (INTMEM 9A & 16: the WP_POLICY bit)
WP_POLICY, WP_SWITCH = 0x02, 0x80       # 9A reply byte 0x10A: policy WP (INTMEM 9A.1), hardware switch
RECORD_UFDMODE = (0x34, 0x38, 0x39, 0x3A, 0x3B)   # the flags holding UFDMode bits 0-4

# == CODE SPACE ==
BOOT_FW_WINDOW = (0x8000, 0x4000)       # where the running boot firmware shows through FA 10

# == BOOT FIRMWARE CAPTURE (docs/processes.md, capturing the boot firmware) ==
# Overlays are stored scrambled; the ROM's overlay loader descrambles one into OVERLAY_AT when
# page0's select routine asks. The capture uploads the stick's own page0 as a RAM loader: slot
# FW_BLOCK_SLOT (read firmware block n, which the ROM's overlay loader calls) stays, the
# ROM-facing slots get the probe loader's constants, and a stub over an unused slot handler
# calls the select routine (protocol.CAPTURE_SUB).
BOOT_OVERLAYS = 10                      # page0 + overlays 0-9: the 44-KB image
PAGE_SIZE_FW = 0x1000                   # page0 and each overlay
OVERLAY_AT = 0xD000                     # trailer [n] [~n] 55 AA at +0xFFC
PAGE0_TAG = b"\x01\x00"                 # page0's trailer index bytes (+0xFFC)
OVERLAY_SELECT = bytes.fromhex("8f6490dffce493")   # head: MOV 0x64,R7; MOV DPTR,#0xDFFC; CLR A; MOVC
FW_BLOCK_SLOT = 13
LOADER_SLOTS = 31
CONST_SLOTS = {2: 0x80, 3: 0x01, 7: 0x01, 19: 0x00, 22: 0x01}   # slot: R7 the ROM expects (MOV R7,#v; RET)
