# Structures

The data structures a stick keeps and the controller works with, outside the NAND page
formats ([nand.md](nand.md)) and the command payloads ([commands.md](commands.md)).
Offsets are within the structure; multi-byte values are big-endian unless marked LE.
Addresses for a particular controller are in its file, e.g.
[controllers/F700.md](controllers/F700.md).

| structure | size | lives in |
|---|---|---|
| [Info record page](#info-record-page) | 8 × 512 B | a free-list block, moved on every save |
| [Info record](#info-record) | 512 B | info record page sector 0 |
| [Config block](#config-block) | 512 B | info record page sector 1 |
| [MP info sector](#mp-info-sector) | 512 B | info record page sector 4 |
| [NAND components](#nand-components) | — | the NAND, overview |
| [Boot firmware image](#boot-firmware-image) | 44 KB | NAND block 0, run from the overlay window |
| [RAM loader](#ram-loader) | 9216 B uploaded | controller RAM, in place of the firmware |
| [Loader patch window](#loader-patch-window) | 818 B | the end of an uploaded loader |
| [NAND set-up block](#nand-set-up-block) | 256 B | controller XDATA, built at boot |

## Info record page

The firmware's own page: everything that makes a stick *this* stick. The mount reads its
first 8 sectors of 512 B, each written by its own vendor command. It is never rewritten in
place: a save copies the page to RAM, replaces one sector, programs a fresh block from the
free list and releases the old one. The block is found at power-on by a tag in its spare
area.

| sector | holds | written by |
|---|---|---|
| 0 | [info record](#info-record) | `96` |
| 1 | [config block](#config-block) | `81 00 FF` |
| 2 | [lock block](#lock-blocks): write-protects the flash LUN while set | `81 51 02`, `83`; cleared by `71`, `73` |
| 3 | [lock block](#lock-blocks): refuses an LBA area while set | `81 51 03`, `83`; cleared by `71`, `73` |
| 4 | [MP info sector](#mp-info-sector) | `81 51 04` |
| 5-7 | blank; the mount also reads sector 7 (unknown purpose) | — |

Every sector can be read with `82 51 nn`.

## Info record

What the firmware knows about itself: sizes, options and protection. The firmware builds it
from RAM on `96`; the sizes come from the host's `99` capacity record. No CRC or sequence
number is checked. An erased record (all `FF`) sets every flag at once: capacity
`0xFFFFFFFF`, write-protect on, the lock closed.

Layout: zeros up to `0x08`, then tagged 32-bit values, constants, the flag bytes, the lane
counts and a status copy. The value offsets (each tag sits in the byte before it):

| offset | tag | field |
|---|---|---|
| `0x0A` | `F1` | total capacity in sectors (also `9A` reply bytes 0-3) |
| `0x11` | `F2` | size of a second LUN or area; nonzero clears the single-area flag |
| `0x16` | `F3` | size READ CAPACITY adds on the flash LUN |
| `0x1B` | `F4` | LBA bound of the accessible area; beyond it, status `8A` |
| `0x21` | `F5` | bound of the area refused while sector 3 is set |
| `0x26` | `F6` | flash LUN capacity reported by READ CAPACITY |
| `0x0E` | — | constant `98 F7` |
| `0x2A` | — | constants `F7 00 01` |
| `0x2F`-`0x48` | `F8`-`FC` | tags, then the flag bytes below |
| `0x80`, `0x85` | `E1`, `E2` | zero values; not read at mount |
| `0x100` | — | usable blocks per FTL lane, 16-bit each |
| `0x1FF` | — | copy of the firmware's status byte |

Flags (nonzero = on). The `F5` column is the byte of the [F5 options block](commands.md#f5-options-block)
that sets the flag.

| offset | F5 | meaning |
|---|---|---|
| `0x2E` | — | bit 1 = policy write-protect |
| `0x30` | `0x00` | AutoRun GPI (UFDMode `0x10` only) |
| `0x34` | `0x03` | UFDMode bit 0 |
| `0x36` | — | fixed disk, with `0x38` |
| `0x38` | `0x04` | UFDMode bit 1: fixed disk (INQUIRY removable bit cleared) |
| `0x39` | `0x05` | UFDMode bit 2 |
| `0x3A` | `0x06` | UFDMode bit 3: **closes the lock gate** |
| `0x3B` | `0x07` | UFDMode bit 4 |
| `0x3F` | `0x0B` | LED |
| `0x40` | `0x0F` | AutoRun Win98 (UFDMode `0x10` only) |
| `0x41` | — | single area: no second area, or `99` payload byte 4 = `'Q'` |
| `0x42` | `0x0A` | LED mode |
| `0x43`, `0x45` | `0x0E`, `0x13` | UFDMode `0x10` |
| `0x44` | `0x12` | NoRemovable |
| `0x46` | `0x14` | UFDMode `0x10` variant |
| `0x47` | `0x15` | ignore eject (START STOP UNIT) |
| `0x48` | `0x16` | write-protect the LUN while sector 2 is set |

UFDMode sets the INQUIRY revision, which the ROM rewrites as `8.0` and a digit; the first
match wins, top to bottom:

| condition | UFDMode | revision |
|---|---|---|
| LUN selector = 1 | — | `8.00` |
| `0x38` | 2 | `8.05` |
| `0x3A` | 8 | `8.06` |
| `0x34` | 1 | `8.07` |
| `0x39` | 4 | `8.08` (`8.09` with LUN selector 8) |
| none of them | `0x10` | the config block's own |

## Config block

The stick's USB and SCSI identity and its serial. The ROM validates it at mount (header,
length, byte sum, `3C C3`, serial); if it fails, the stick enumerates with the ROM's
default identity (`Generic USB Flash Disk  7.76`).

| offset | size | field |
|---|---|---|
| `0x00` | 2 | `99 07` |
| `0x02`, `0x03` | 1 each | length of the manufacturer, product string descriptor |
| `0x04` | 18 | USB device descriptor (VID, PID and `bcdDevice` LE) |
| `0x16` | var | USB string descriptors: manufacturer, then product |
| after them | 28 | SCSI vendor (8), product (16), revision (4) |
| after SCSI | 1 + 2 | byte sum of everything before it; `AA 55` |
| `0x7C` | 2 | (unknown), kept |
| `0x7E` | 2 | `3C C3` |
| `0x80` | var | serial string descriptor, then its byte sum |
| `0xC0` | 8 | two 32-bit sizes (`00000002 00000002` by default) |
| `0xD4` | 1 + 3 | a random byte, then `PQI` |

Example: AlcorMP's default block with the serial `12345678`. The string lengths at `0x02`
and `0x03` decide where everything up to `0x7C` lands:

```
000: 99 07 10 1a                      magic; manufacturer descriptor 16 B, product 26 B
004: 12 01 10 01 00 00 00 08          USB device descriptor: USB 1.10, max packet 8,
00c: 8f 05 87 63 00 01 01 02 00 01      VID 058F, PID 6387, bcdDevice 1.00, strings 1 and 2
016: 10 03 47 00 65 00 ...            "Generic" (UTF-16)
026: 1a 03 4d 00 61 00 ...            "Mass Storage"
040: 47 65 6e 65 72 69 63 20          SCSI vendor  "Generic "
048: 46 6c 61 73 68 20 44 69 ...      SCSI product "Flash Disk      "
058: 38 2e 30 30                      SCSI revision "8.00"
05c: eb aa 55                         byte sum of 0x00-0x5B, then AA 55
07c: 64 9b 3c c3                      (unknown); serial marker
080: 12 03 31 00 32 00 ... 38 00      serial descriptor "12345678"
092: b9                               byte sum of the serial descriptor
0c0: 00 00 00 02 00 00 00 02          the two sizes
0d4: 5a 50 51 49                      random byte, "PQI"
```

The SCSI revision here is replaced in INQUIRY unless UFDMode is `0x10`
([info record](#info-record)). AlcorMP's default block: `058F:6387`, "Generic" /
"Mass Storage", SCSI "Generic" / "Flash Disk" / `8.00`, eight random hex digits as serial.

## Lock blocks

Info record page sectors 2 and 3, the records of Alcor's partition-password and secure-area
scheme (?). Neither AlcorMP's format nor the tool writes them, so on a formatted stick they
are blank and the stick is unlocked. Writing them could lock a stick behind a password.

| offset | size | field |
|---|---|---|
| `0x00` | 128 | content (unknown) |
| `0x80` | 2 | state: `FFFF` = blank, anything else = set |
| `0x82` | — | (unknown) |

| sector | while set |
|---|---|
| 2 | the flash LUN is write-protected (record flag `0x48`) and the `99` capacity record is refused |
| 3 | the area up to the record's `F5` bound is refused |

## MP info sector

AlcorMP's note of the last format: `FF` except for a 256-byte state block at `0x32`
(constant (?)), the date and the capacity.

| offset | size | field |
|---|---|---|
| `0x32` | 256 | AlcorMP state block (?) |
| `0x12C` | 4 | format date: year (16-bit), month, day |
| `0x132` | 4 | capacity in sectors |
| `0x140` | 1 | `00` |
| `0x1FE` | 2 | `55 AA` |

## NAND components

What a formatted stick holds on its NAND, in the order the controller needs it. Formats
and block numbers: [nand.md](nand.md); drawing: [nand_map.html](nand_map.html).

| component | where | holds |
|---|---|---|
| FTL anchor | FTL system block, page 0 | the record the mask ROM reads first: geometry, pointers to the firmware |
| lane bitmaps | FTL system block, page 1 | per FTL lane, the blocks that are bad or allocated |
| boot firmware | FTL system block, from page 2 | the [boot firmware image](#boot-firmware-image) |
| ECC table, FTL records | FTL system block, after the firmware | BCH parameters, FTL configuration, frame layout |
| FTL mirror | second FTL system block | a copy of all of the above |
| FTL index and translation tables | FTL blocks | the logical-to-physical map |
| data superblock heads | data superblocks | the free-block list the firmware allocates from |
| info record page | a free-list block | the [info record page](#info-record-page) |
| housekeeping tables | one block | AlcorMP's encrypted bad-block and format records |
| user data | everything else | the logical disk |

## Boot firmware image

The firmware the mask ROM loads from NAND. It runs in an 8-KB window: page 0 is always
resident, one overlay at a time shares the other half and is swapped in when the firmware
needs it. Different overlays serve different commands (one handles mount, config and FTL).

| part | size | holds |
|---|---|---|
| page 0 | 4 KB | hook table (`LJMP` slots) and common code, always resident |
| overlay n | 4 KB each | its first `0x400` bytes common helpers, the rest its own code; index at `0xFFC` |
| trailer | last 4 B of each page | `[idx] [~idx] 55 AA`; page 0 `01 00`, page k `k-1`, `~(k-1)` |
| FTL self-descriptor | 187 B in the first two pages | FTL system block, data superblock, spare blocks, page list; patched in by the format |

## RAM loader

The code a host uploads (`FA 0A`) in place of the boot firmware, to run NAND operations
during a format. The upload's first `0x2000` bytes replace the overlay window; the rest is
the [patch window](#loader-patch-window). Which `FA 0B` subs work depends on the resident
loader.

| part | offset | holds |
|---|---|---|
| hook table | `0x0000` | 31 `LJMP` slots of 3 B; slot n at `3n` |
| code | after the table | the loader's handlers |
| patch window | `0x2000` | [loader patch window](#loader-patch-window) |

The ROM calls fixed slots, so their meaning is shared across loaders while their code differs:

| slot | ROM calls it | role |
|---|---|---|
| 0 | once, at the end of an upload | init (writes the NAND controller registers) |
| 1 | (unknown) | resets the write-protect masks |
| 4 | for every `FA 0B` | sub dispatch: the first switch on CDB byte 2 |
| 10 | every idle-loop pass after an upload; before every command when patch byte `0x121` = `'Q'` | background work |
| 19 | `FA 08` with CDB byte 2 = `0x51` | (unknown) |
| 29 | `FA 1D` with CDB byte 6 = `0x51` | (unknown) |
| others | — | most are a bare `RET` |

A loader needs no more than that: one whose slots are all stubs except its own slot 4 runs
(the tool's probe loader is built this way).

What a loader can rely on when slot 4 runs:

| item | state |
|---|---|
| CDB | in the USB command buffer; the sub at CDB byte 2 |
| CE | CDB byte 3, already checked against the CE limit (patch byte 2) |
| result | returned in `R7`: `0x81` = the loader finished the USB transaction itself, anything else = the ROM sends a passing CSW |
| data in | 512 B placed in the endpoint buffer and sent with the ROM's send routine |
| NAND controller | set up from the patch window by the upload |

## Loader patch window

The last 818 bytes of an upload, which the ROM applies before slot 0 runs. Its first
`0x100` bytes are geometry and switches; `0x100`-`0x1EF` has the same layout as the
[NAND set-up block](#nand-set-up-block), so a patch window can be built from the stick's own.

| offset | field |
|---|---|
| `0x000` | NAND controller channel mode |
| `0x002` | CE limit: an `FA 0B` with a CE at or above it gets a passing CSW and nothing else |
| `0x004`-`0x00F` | geometry fields; `0x006` = CE count at MP enable, unit count on later re-enables |
| `0x011` | NAND controller mode |
| `0x100`-`0x1EF` | NAND set-up, copied to the controller's set-up block |
| `0x121` | `'Q'`: the ROM calls slot 10 before every command |
| `0x19D`, `0x19F` | `'Q'` switches a feature on; `0x19F` with `FA 1D` sets a NAND controller mode bit |
| `0x191` | peripheral register value |
| `0x195` | ECC: t − 2 |
| `0x1B0` | ECC: frame size, low byte |
| `0x1FE` | `JN`: without it the ROM refuses the upload |
| `0x200`-`0x331` | BCH parameter table for the active t ([ECC parameter record](tools.md#ecc_paramstsv)) |

## NAND set-up block

How the NAND controller is configured, built by the firmware at boot and applied by the ROM
the same way an upload applies its patch window. The tool reads it from the stick to make a
patch window for its own loaders.

| offset | field |
|---|---|
| `0x91` | peripheral register value |
| `0x95` | ECC: t − 2 |
| `0xA8` | NAND controller mode (stays after boot) |
| `0xB0` | ECC frame size, low byte |
| `0xF0`-`0xFF` | geometry fields |
| others | (unknown); copied verbatim |

How the firmware builds it is (unknown).
