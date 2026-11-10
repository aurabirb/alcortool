# Commands

Every command the host sends to an Alcor controller, and what the controller sends to its
NAND. The same CDB can be answered by different code depending on the state of the stick:

| answered by | when | serves |
|---|---|---|
| mask ROM | always | standard SCSI before the firmware loads, `9A`, the vendor switch, most `FA` subcommands |
| boot firmware | after a successful mount | standard SCSI with the stick's identity, the info record page commands |
| RAM loader | after an `FA 0A` upload, until reset | the `FA 0B` NAND operations; which subs work depends on the loader |
| probe loader | after uploading the tool's own loader | its own `FA 0B` subs (`E0`-`E5`) |

CDBs are written as their bytes in hex, trailing zeros left out (`82 51 01`); lowercase
letters stand for fields (`FA 13 00 pp`: `pp` = an XDATA page). Payload values are
big-endian unless marked LE.

Which AlcorMP loader serves which sub is per controller ([F700](controllers/F700.md)).
The command map in code is `alcor/protocol.py`; `alcortool dev map` prints it.

## Transport

| item | value |
|---|---|
| framing | USB Bulk-Only: 31-byte CBW, data, 13-byte CSW |
| access | usbfs, bypassing the kernel SCSI layer, which rejects short CDBs and rewrites the LUN bits of `82 51` / `81 51` |
| CDB length | ignored by the controller: zero padding is harmless |
| residue | the CSW reports the full length as residue on vendor commands even when data was sent: trust the bytes received |
| first CBW | its tag is compared with `05 8F 93 84`; a match changes a state flag for the session (unknown effect) |
| disk content | ordinary READ(10) / WRITE(10) through the kernel block device |

## SCSI commands

| CDB | data | use |
|---|---|---|
| `12 00 00 00 24 00` | 36 B in | INQUIRY: vendor, product, revision ([who answers](structures.md#config-block)) |
| `1A 00 3F 00 C0 00` | 192 B in | MODE SENSE(6), all pages |
| `25` | 8 B in | READ CAPACITY(10) |

## Vendor commands

Gated commands are refused while the lock gate is closed ([lock gate](processes.md#vendor-command-dispatch-and-the-lock-gate)).

| CDB | data | gated | effect |
|---|---|---|---|
| `9A` (1- or 10-byte) | 512 B in | — | identify: chip ID, sizes, write-protect ([reply](#9a-identify-reply)) |
| `82 51 00` | 512 B in | — | read the info record as mounted |
| `82 51 01` | 512 B in | — | read the config block |
| `82 51 04` | 512 B in | — | read the MP info sector |
| `82 51 nn` | 512 B in | — | read info record page sector `nn` (`02`, `03`: the lock blocks) |
| `81 00 FF` | 512 B out | yes | save the config block as info record page sector 1 |
| `81 51 nn` | 512 B out | yes | save the block as sector `nn` (`81 51 00` would overwrite the record) |
| `96` | 512 B in | yes | build the info record from RAM and save it as sector 0 |
| `99` (10-byte) | 512 B out | yes | [capacity record](#99-capacity-record): capacity and usable blocks per lane |
| `9B` | 512 B out | yes | [write-protect block](#9b-write-protect-block): policy write-protect in RAM |
| `9C 51 a b` | — | yes | set the two write-protect masks for this session |
| `F5` (10-byte) | 512 B out | yes | [options block](#f5-options-block): sets record flags; byte 6 = `'Q'` closes the lock gate |
| `F8 51 00 FF` | 512 B in | — | write-enable and hardware write-protect check |
| `F8 51 FF 00` | 512 B in | — | the same check, after the closing reset of a format |
| `83` | 512 B out | yes | write a lock block (partition password) (?) |
| `71`, `73` | (unknown) | yes | clear a lock block (?) |
| `72`, `74`, `9D` | (unknown) | yes | (unknown) |
| `FA …` | below | yes | the `FA` family |

## FA subcommands

| CDB | data | effect |
|---|---|---|
| `FA 00` | 512 B in | flash ID per CE, 16-byte slots |
| `FA 00 00 00 00 00 00 08` | 512 B in | flash ID, form for chips from F500 on |
| `FA 01 ss cc bb bb pp pp` | `ss` × 512 B in | NAND page read outside MP mode: sectors, CE, block, page |
| `FA 05` | 512 B in | status read |
| `FA 08 00` | — | controller reset: the firmware reloads from NAND and the stick re-enumerates; `FA 08 00 51` also calls loader slot 19 |
| `FA 0A 00 ss` | `ss` × 512 B + 1 KB out | upload a [RAM loader](structures.md#ram-loader): `ss` sectors of code (`10` = 8 KB), then the patch window |
| `FA 0B …` | per sub | [NAND operation](#fa-0b-nand-operations) through the resident loader |
| `FA 0C 00 33` | — | erase the whole chip (minutes) |
| `FA 0E` | 512 B in | controller info; refused when locked, which is how the lock is detected |
| `FA 10 ss 00 aa` | `ss` × 512 B in | read CODE space from `aa00` |
| `FA 11` | 512 B in | CODE read, second form (?) |
| `FA 12 00 00 00 03` | 512 B out | register block write |
| `FA 12 00 90 00 09` | 512 B out | closing register block of a format |
| `FA 13 00 pp` | 512 B in | read XDATA page `pp00`, in normal mode too |
| `FA 1D` | — | enter MP write mode; also opens every write-phase re-enable; `FA 1D … 51` at byte 6 calls loader slot 29 |
| `FA 1D 01` | — | leave MP write mode |
| `FA 1F` (10-byte) | — | sent after the closing reset of a format (unknown effect) |
| `FA 80 00 80` | 512 B in | geometry echo, read at the end of a format |

## FA 0B NAND operations

`FA 0B` hands the CDB to the resident RAM loader. A sub the loader does not serve times out.
Each operation the controller must finish is followed by an `FA 0B 06` status poll.

CDB fields (8-byte forms stop at byte 7):

| byte | field |
|---|---|
| 2 | sub |
| 3 | CE; must be below the CE limit of the patch window |
| 4-5 | block |
| 6-7 | page |
| 9 | sectors (512 B each) |
| 10-13 | region: selects the area and options such as the randomizer |
| 14 | ECC frame, or the page's low byte echoed |

Some subs move fields: `03`, `04`, `05` take the sector count at byte 8; `91`, `92`, `9A` the
frame at byte 15; `94` its sector count at byte 3; the sweeps a count at byte 7 (`B0`), 8
(`C0`, `C1`) or 9 (`B1`).

Example: reading the ECC table on block 0 (page `0x0D`) with the fixed system-area recipe:

```
FA 0B 46                FA 0B, sub 46: system page read by ECC frame
00                      CE 0
00 00                   block 0
00 0D                   page 0x0D
00                      —
0E                      14 sectors (512 B each)
FF FF 20 02             region: the system area
8A                      ECC frame: 650-B frames (0x28A)
00
```

Example: the column-skip table, 2048 B, so 4 sectors, given at byte 3:

```
FA 0B 94 04 00 00 00 00 00 00 00 00 00 00 00 00
```

Subs of AlcorMP's loaders. "Poll" marks the subs followed by `06`; sweeps run for seconds.

| sub | CDB | reply | poll | operation |
|---|---|---|---|---|
| `01` | 8 | — | — | param block, 2048 B out ([format](llf.md#param-block)) |
| `03` | 16 | — | — | load the pattern buffer (sectors at byte 8) |
| `04` | 16 | — | yes | sweep skip bitmap, 512 B out |
| `05` | 16 | 512 B | yes | sweep failure report |
| `06` | 8 | 512 B | — | status poll: byte 0 = `01` error, `02` fail |
| `08` | 8 | — | — | close a commit burst |
| `09` | 8 | — | — | open a commit burst (`FA 0B 09 00 20 02 8A`) |
| `11` | 8 | 512 B | yes | block erase; page high byte = mode |
| `18` | 16 | — | yes | page write |
| `19` | 16 | sectors | yes | page read |
| `1E` | 16 | — | yes | page program, 28 or 32 sectors |
| `1F` | 16 | sectors | yes | page read-back |
| `21` | 16 | — | yes | housekeeping table write, 2048 B out |
| `22` | 16 | 2048 B | yes | housekeeping table read |
| `3A` | 16 | — | — | FTL marker |
| `3E` | 16 | — | — | end of the MP write-phase enable (`FA 0B 3E 00 00 01`) |
| `42`, `47` | 8 | 512 B | yes | scan marker read |
| `43` | 8 | ECC map | yes | ECC bit-error map |
| `45` | 16 | — | yes | system page write |
| `46` | 16 | sectors | yes | system page read, by ECC frame |
| `90` | 16 | 512 B | yes | FTL spare and marker scan |
| `91` | 16 | 512 B | yes | FTL activate |
| `92` | 16 | 19456 B | — | FTL map read-back |
| `94` | 16 | — | — | column-skip table, 2048 B out ([format](llf.md#column-skip-table)) |
| `96` | 16 | — | — | housekeeping marker |
| `97` | 16 | — | yes | boundary base write |
| `99` | 16 | — | — | FTL index page, 1024 B out |
| `9A` | 16 | 512 B | yes | FTL commit marker |
| `9B` | 16 | 512 B | yes | FTL marker read |
| `A0` | 8 | — | yes | erase sweep |
| `A1` | 16 | 512 B | yes | read-check sweep |
| `A2` | 8 | — | yes | multi-plane erase sweep |
| `A7` | 8 | — | yes | die-wide erase sweep |
| `AE` | 16 | — | yes | translation table write |
| `AF` | 16 | 512 B | yes | translation table verify |
| `B0` | 8 | — | yes | erase and program sweep (count at byte 7) |
| `B1` | 16 | — | yes | copyback sweep (count at byte 9) |
| `C0` | 16 | 2048 B | — | SLC read-back sweep (count at byte 8) |
| `C1` | 16 | 2048 B | — | read-back sweep (count at byte 8) |

What each sweep does: [llf.md](llf.md#housekeeping-sweeps).

## Probe loader commands

`FA 0B` subs of the tool's own loader (`alcor/loaders/probe.a51`). It needs no AlcorMP code
or data: its patch window comes from the stick's own NAND set-up.

| sub | CDB fields | data | effect |
|---|---|---|---|
| `E0` | — | 512 B in | signature `alcortool probe 5`, then internal RAM at `0x100` |
| `E1` | 4-5 address, 9 sectors | sectors in | read XDATA |
| `E2` | 4-5 address, 7 byte | — | write one XDATA byte |
| `E3` | 4 command, 5 address cycle, 6-7 count, 10 wait | count in | one NAND command and address cycle, then read bytes |
| `E4` | 4-8 address cycles, 9 sectors, 10 prefix | sectors in | raw page read: data, parity and spare as stored; optional prefix command (e.g. `DA`) |
| `E5` | 4-6 row | 512 B in | block erase, returns the status |

Under the [firmware capture](processes.md#capturing-the-boot-firmware) loader:

| sub | CDB fields | data | effect |
|---|---|---|---|
| `E6` | 4 overlay | — | load the overlay into the window |

## NAND commands

What the controller sends to the NAND chip: standard ONFI command and address cycles. Every
operation starts by selecting the CE and waiting until the chip is ready. An address is 2
column cycles (byte offset in the page) and 3 row cycles (`block << 9 | page` on a 512-page
part), each low byte first.

| command | cycles | data |
|---|---|---|
| reset | `FF` | — |
| READ ID | `90`, `00` | the flash ID; with address `20`, the signature `ONFI` |
| parameter page | `EC`, `00`, wait | 256-B copies of the parameter page, each with a CRC |
| status | `70` | one byte: bit 6 ready, bit 7 not write-protected, bit 0 failed |
| page read | `00`, column, row, `30`, wait, `00` | the page as stored: data, ECC parity, spare |
| change read column | `05`, column, `E0` | continue reading from another offset |
| block erase | `60`, row, `D0`, wait | — (status bit 0 = failed) |
| page program | `80`, column, row, data, `10`, wait | — |
| change write column | `85`, column | continue writing at another offset |
| SLC mode | `DA`, then the operation | the next operation uses one bit per cell (Micron) |

"Wait" means: send `70` and read the status byte until bit 6 is set. The `00` after a page
read's wait switches the chip back from status to data output.

Example: reading block 0 page 2 (the boot firmware) of an F700 stick, which is stored in SLC
mode:

```
DA                      SLC mode: the system blocks are pseudo-SLC
00                      page read
00 00                   column 0: from the start of the page
02 00 00                row 2: block 0 << 9 | page 2, low byte first
30                      confirm: the chip loads the page (tR, about 78 µs)
70 ... until bit 6      wait for ready
00                      back to data output
<18592 bytes>           16384 B data + 2208 B spare, ECC parity in place
```

## Sense codes

| sense | meaning |
|---|---|
| `5/26/00` | refused by the lock gate |
| `7/27` | the stick is write-protected |

## By mode

What answers in each state of the stick. MP mode (`FA 1D` and a loader upload) can be
entered from normal or ROM mode; `FA 0B` works only in it.

| command | normal | locked | ROM mode |
|---|---|---|---|
| INQUIRY | config identity | ROM default | ROM default |
| `9A` | yes | yes, fields `FF` | chip ID, empty fields |
| `82 51` reads | yes | yes | (unknown) |
| `FA 00`, `0E`, `10`, `13` | yes | no | yes |
| `FA 1D`, `0A`, `0B`, `0C` | yes | no | yes |
| `81`, `96`, `99`, `9B`, `F5` | yes | no | (unknown) |

## Payloads

### 9A identify reply

| offset | size | field |
|---|---|---|
| `0x000` | 4 | total capacity in sectors (info record `F1`) |
| `0x004` | 2 | chip ID, e.g. `F7 00` |
| `0x006` | 1 | `A1` |
| `0x007` | 4 | info record `F2` |
| `0x00B` | 1 | `A2` |
| `0x00C` | 4 | info record `F4` |
| `0x010` | 1 | `A3` |
| `0x02C` | 1 | `FA 0E` flag |
| `0x038` | 1 | `'P'` once options are set (the tool's `F5` checks it) |
| `0x10A` | 1 | write-protect: bit 7 hardware switch, bit 1 policy |
| `0x117`, `0x125` | 1 | write-protect mask 1 |
| `0x118`, `0x126` | 1 | write-protect mask 2 (`FF` = query again) |

Bytes 0-3 and 8 are also written from other state in some paths (unknown order).

### 99 capacity record

| offset | size | field |
|---|---|---|
| `0x004` | 1 | `'Q'` = single area |
| `0x014` | 1 | `01` |
| `0x040` | 4 | capacity in sectors, LE |
| `0x044` | 1 | `51` |
| `0x0FE` | 2 | `JN` |
| `0x100` | 2 per lane | usable blocks per FTL lane |
| `0x1FF` | 1 | `51` |

### F5 options block

Byte n = `0x51` sets the info record flag whose F5 byte is n
([table](structures.md#info-record)). The tool sends bytes 1, 3, `0x0B` and `0x0F` as `51`,
and byte 9 = `51`, byte `0x0A` = `50` unless the identify reply already has `'P'` at `0x38`.

### 9B write-protect block

| offset | bit | effect |
|---|---|---|
| 1 | 1 | policy write-protect |
| 1 | 2 | the firmware's second protection bit (unknown) |
| 1 | 6 | both |

The change applies at once; `96` makes it survive the next mount.

### Format payloads

The param block, column-skip table, sweep skip bitmaps, pattern and housekeeping writes:
[llf.md](llf.md#payloads). The loader upload: [structures.md](structures.md#ram-loader).
