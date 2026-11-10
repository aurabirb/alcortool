# NAND structures

What a formatted stick holds on its NAND, page by page. This file is the source for
[nand_map.html](nand_map.html), the drawing of the same layout. The overview is in
[structures.md](structures.md#nand-components); how each region is written is in [llf.md](llf.md).
Block numbers are per controller (an F700 column for now); offsets are within the page or
record, big-endian unless marked LE.

## Pages and ECC

A raw page is cut into ECC frames, each data followed by its BCH parity; the spare bytes
left over after the last frame are slack. The controller computes and checks ECC in
hardware: a host never sends or reads parity through the normal commands.

| frame | data | parity | used for |
|---|---|---|---|
| 650 B (`0x28A`) | 512 B | 138 B | system pages (pseudo-SLC): 28 frames = 14336 B of data |
| 1162 B (`0x48A`) | 1024 B | 138 B | data pages, t = 78: 16 frames fill the page |
| 1152 B (`0x480`) | 1024 B | 128 B | data pages, t = 72, leaving slack for bad columns |

The spare bytes a host sends with a page are not stored: the controller derives its own,
and reading the host spare back returns `20 C0`.

## SLC and MLC

| mode | pages per block | used for | read with |
|---|---|---|---|
| MLC (2 bits per cell) | 512 | user data, FTL and data blocks | plain `00`/`30` |
| pseudo-SLC (1 bit per cell) | 256 | the two FTL system blocks | the `DAh` prefix, then `00`/`30` |

A page written in SLC mode must be read in SLC mode: an MLC read of the same page number
returns other bytes.

## Randomizer

Some pages are scrambled when written, so raw reads of them look random. The firmware reads
them through the transfer engine with a per-page seed.

| item | value |
|---|---|
| scrambled | block 0 pages `0x04`-`0x0C` (firmware overlays 1-9) |
| plain | block 0 pages `0x00`-`0x03` and `0x0D` |
| switch | the `FA 0B` region bytes: `ff ff 01 00` on scrambled pages, `ff ff 00 00` on plain (?) |
| seed rule | (unknown) |

## Block layout

Physical blocks, low to high, after a format. "Written by" names the `FA 0B` subs
([commands.md](commands.md#fa-0b-nand-operations)).

| region | F700 blocks | contents | written by |
|---|---|---|---|
| FTL system block | `0x000` | [block 0 page map](#block-0-page-map) | `45`, `1e`, `18` |
| translation tables | `0x001`-`0x003` | [translation tables](#translation-tables), 256 pages each | `ae` |
| bad-block screen | `0x000`-`0x08F` | erased; each block test-written once | `11`, `18`, `19` |
| scratch | `0x058` | erased after a 32-page self-test | `1e`, `1f`, `11` |
| boundary-scan bases | `0x04C`, `0x04E`, `0x06D`, `0x06F` (+ k·`0xC4`) | erased; the bases of the boundary scans | `11`, `42`, `43`, `47`, `9b` |
| FTL mirror | `0x0B2` | block 0's records one page lower; anchor at page `0x88` | `45`, `1e`, `18` |
| FTL units | `0x0F4`-`0x0F7` + n·`0xF4` | [FTL index pages](#ftl-index-pages) | `91`, `92`, `9a`, `99` |
| data superblocks | two, in the lanes of the FTL system blocks; the choice varies per format | [head pages](#data-superblock-head) + 252 pattern pages each | `1e` |
| initial data blocks | `0x100 + 4i + lane` (45 on lane 0, 40 on the others) (?) | free; listed by the data-superblock heads | — |
| info record page | a free-list block | the [info record page](structures.md#info-record-page), moved on every save | firmware |
| spare blocks | 4 per system copy, in its lane | blank; named in the firmware self-descriptor | — |
| housekeeping | `0x400` | [housekeeping tables](#housekeeping-tables) | `21` |
| user data | everything else | the logical disk | — |

FTL lanes: block mod 4 on F700. The lane bitmaps and the capacity count blocks per lane.

## Block 0 page map

The primary FTL system block, in pseudo-SLC page numbers. The FTL mirror holds the same pages
one lower, with its anchor at page `0x88`. Read at power-on: pages `0x00`, `0x02`, `0x03`, then `0x01`.

| page | mirror | contents | stored |
|---|---|---|---|
| `0x00` | `0x88` | [FTL anchor](#ftl-anchor) | plain |
| `0x01` | `0x00` | [lane bitmaps](#lane-bitmaps), then pattern | plain |
| `0x02` | `0x01` | boot firmware page 0, with the [self-descriptor](#firmware-self-descriptor) | plain |
| `0x03` | `0x02` | firmware overlay 0 | plain |
| `0x04`-`0x0C` | `0x03`-`0x0B` | firmware overlays 1-9 | scrambled |
| `0x0D` | `0x0C` | [ECC parameter table](#ecc-parameter-table) | plain |
| `0x0E` | `0x0D` | [FTL configuration record](#ftl-configuration-record), twice | — |
| `0x0F` | `0x0E` | [frame record](#frame-record), twice | — |
| `0x10`-`0xFF` | — | pattern pages: the complement of the LLF pattern from `page · 0x1000` | — |

Each firmware page holds 4 KB of the image followed by its slice of the LLF test pattern.
The records chain: the anchor points to the frame record, the frame record to the
configuration record, the configuration record to the ECC table.

## FTL page pointer

The 12-byte pointer the system records use to name a page.

| offset | size | field |
|---|---|---|
| `0x0` | 4 | `0C`, attribute (`08` = firmware page, `01` = record), `08 83` |
| `0x4` | 1 | NAND attribute (`DA` on F700) |
| `0x5` | 3 | `00 00 00` |
| `0x8` | 1 | page |
| `0x9` | 2 | block × planes, LE |
| `0xB` | 1 | `30` |

## FTL anchor

The `0x80`-byte record the mask ROM reads first, tiled across the page. Unreadable on both
copies, the controller stays in ROM mode.

| offset | size | field |
|---|---|---|
| `0x00` | 5 | magic `0D 06 05 06 38` |
| `0x08` | 1 | planes |
| `0x0B`-`0x22` | — | constants: `0B`=`2A`, `0D`=`33`, `12`=`38`, `13`=`10`, `15`=`37`, `16`=`01`, `1F`=`24`, `22`=`01` |
| `0x2A` | 12 | [FTL page pointer](#ftl-page-pointer) to the frame record; its byte `0x2E` is the NAND attribute, not a write-protect flag |
| `0x36` | 3 | `AA FF FF` |

## Lane bitmaps

One bitmap per FTL lane, `0x200` bytes each on F700: bit i marks lane block `lane + 4i` as
unavailable to the free-block allocator. Marked: bad blocks, system-allocated blocks, the
lane's initial data blocks, and every slot past the lane's last block.

## Firmware self-descriptor

Patched by the format into the tails of the image's first two 4-KB blocks. Offsets are
within the image.

| offset | field |
|---|---|
| `0xF14` | header `08 01 01 00 00 02 00 09 81 00 02 00` |
| `0xF30` | cell slice indices, 16-bit each |
| `0xF50` | page size per cell slice, 16-bit each |
| `0xF70` | FTL system block, data superblock, `FFFF FFFF` |
| `0xF78`, `0xF80` | the four spare blocks, in pairs, each followed by `FFFF FFFF` |
| `0xF89`, `0xF8F` | cell slices; `01` |
| `0xFFE` | image length in 4-KB blocks |
| `0x1F50` | 8 ECC frame offsets (multiples of 650) |
| `0x1F60` | the NAND pages of firmware blocks 1 on, `FF`-padded |
| `0x1FB0` | `0x1000 + i` per cell slice, `FF`-padded |

## ECC parameter table

The BCH parameter record of the active strength, the same record AlcorMP keeps in its
ECC parameter file ([tools.md](tools.md#ecc_paramstsv)): bytes 0-3 are mode, codeword,
family and t (`00 04 02 4E` on F700). Its first `0x132` bytes are also the loader patch
tail.

## FTL configuration record

`0x200` bytes, stored twice in the page. Its second half has the layout of the
[NAND set-up block](structures.md#nand-set-up-block) (?).

| offset | field |
|---|---|
| `0x000` | header `0D 06 05 06 38 01 12 …` |
| `0x014`, `0x020` | [page pointers](#ftl-page-pointer) to firmware pages 0 and 1 |
| `0x100` | `A0 00`, lane, block index in the lane (16-bit), `00 00` |
| `0x111` | 1 on the primary FTL system block |
| `0x14B`, `0x14F`, `0x150` | NAND trim values, from the controller's register page |
| `0x160` | page pointer to the ECC parameter table |
| `0x194` | `33`, t − 2 |
| `0x1B0` | frame size, low byte |
| `0x1EC` | constants, with the lane count at `0x1F2` and `0x1F6` |
| `0x1FC` | block budget (16-bit), block index (16-bit) |

## Frame record

`0x100` bytes, stored twice: the ECC frame layout of a 4-KB firmware block.

| offset | field |
|---|---|
| `0x00` | header `0D 06 05 06 38 …` |
| `0x2B` | 8 entries of 5 B: `00 00 k`, offset of frame k (16-bit, multiples of 650) |
| `0x5B` | page pointer to the FTL configuration record |
| `0x67` | `AA FF FF` |

## Data superblock head

The first four pages of each data superblock carry a 2-KB FTL header. At boot the firmware
takes its free-block list from it. Field meanings beyond the list are guesses.

| offset | field |
|---|---|
| `0x000`, `0x240` | `00 01 … FF` ramps (?) |
| `0x360` | parameters: cell slices at `0x367`, `0x368`; entry count at `0x36E` |
| `0x37A` | read by the firmware at boot (unknown) |
| `0x3A0` | frame offsets, 16-bit each |
| `0x700` | initial data blocks, 16-bit each: the free list |
| `0x7E0` | `20 1F … 01` (?) |

## FTL index pages

`1024` bytes each, written by `FA 0B 99`.

| offset | field |
|---|---|
| `0x000` | logical-to-physical map, low bytes (an identity ramp after a format) |
| `0x200` | the high 2 bits, 4 entries per byte |
| `0x280` | trailer: flag, has-target, target (16-bit), `01 00`, has-target |

The target is the boundary base block the page belongs to; `FFFF` marks the head page of
an FTL column.

## Translation tables

256 pages per block on blocks `0x001`-`0x003`. The host sends only headers; the controller
computes the content. Format (unknown).

## Housekeeping tables

AlcorMP's own record of a format, which it decodes on a later run: seven tables in one
block, `0x400` bytes of plaintext per page.

| page | table | type |
|---|---|---|
| 0 | header: where the tables are, chip ID, format date | `2000` |
| 1 | page list: per page, type \| page, LE | `4000` |
| 2 | system record: planned capacity (LE) | `C000` |
| 3 | slot map | `6000` |
| 4-7 | block status: one byte per block, its worst read value | `8000` |
| 8-23 | one 4-KB record per FTL lane, with its good-block count | `E000` |
| 24 | MP info: host and user name | `0000` |

Encoding, per page: each `0x100`-byte chunk is AES-256-CBC encrypted with a key per chunk
index; each `0x80` bytes of ciphertext become a `0x100`-byte record: the ciphertext, `AA`, a
random byte and 126 bytes of RS(255,129) parity. Codec: `alcor/ftlcrypt.py`.

Fields:

| table | offset | field |
|---|---|---|
| header (`0x100` B, 4 times) | `0xA2`, `0xC8` | page of the page list, of the system record |
| | `0xB0` | chip ID (`F700`) |
| | `0xE0` | block span, high byte |
| | `0xED`, `0xFE` | size of the lane records |
| | `0xFA` | format date: year (16-bit), month, day |
| | others | MP state, constants (?) |
| page list | `0x000` | one LE word per page: type \| page; `FF` after the last |
| system record | `0x00` | `AA` |
| | `0x02` | AlcorMP's INI name, 32 B |
| | `0x27` | planned capacity in sectors, LE |
| | `0x2B` | `80` |
| slot map | `0x000` | `0x100` LE entries, entry i = i; then `FF` |
| block status | `0x000` | one byte per block over the block-address span; `FF` past the die |
| lane record (4 KB) | `0x12`, `0x29` | lane number |
| | `0x18` | blocks in the lane |
| | `0x24`, `0x26`, `0x28` | pages per block, sectors per page, lane count |
| | `0x2A` | capacity in MiB |
| | `0x30` | good blocks: the lane's blocks less the bad ones |
| | others | constants (?) |
| MP info (`0x100` B, 4 times) | `0x00` | `AA 01 00 00 00 05 04 19 01` |
| | `0x40`, `0x60` | host name and user name of the machine that ran the format (`alcortool` from the tool) |

Block status values: below `0xA0` a corrected-bit count; `0xA0` and up means failed, with
`E0` ORed in for a block a `05` report or the screen flagged
([sweeps](llf.md#housekeeping-sweeps)). Without the sweeps: `00`, and `E0` for screen-bad
blocks. `alcortool format --bad-blocks keep` reuses the table; `alcortool dev housekeeping`
lists it.
