# Low-level format

What AlcorMP does to a stick, and what `alcortool format` reproduces: erase the die, find
the bad blocks, build the FTL, write the boot firmware and the system records, and leave a
stick that mounts with the right capacity and identity. The stream is built phase by phase
in `alcor/provision.py`; `alcortool dev plan` prints it. Payload values are big-endian
unless marked LE.

Things worth knowing first:

- A full format takes about an hour, almost all of it in the controller's own die-wide
  test sweeps. Run it detached and never kill it: after the chip erase, an interrupted
  format leaves the stick in ROM mode (recoverable by running the format again). Start it
  with `setsid nohup alcortool format ...` so it survives the terminal.
- The host does not write the FTL itself. It uploads RAM loaders, tells them what to build
  with param blocks, and the controller does the NAND work.
- The last steps matter most: without saving the info record (`96`) the stick boots once
  and comes up locked at the next power-on.

## Phases

| phase | commands | result |
|---|---|---|
| backup (tool only) | the read-only commands, `FA 10` | a [backup dump](tools.md#backup-dump) |
| detect | `9A`, `FA 0E`, `FA 00`, `FA 13`, `FA 10`/`FA 11` | chip, flash ID, register state |
| erase and MP enable | `FA 0C 00 33`, then a [re-enable](#write-phase-re-enable) and `FA 0B 3E` | an erased die, MP write mode |
| scan and program | ~30k `FA 0B` ops in groups | bad blocks found, FTL, tables, firmware and system pages written |
| set info and reset | `FA 1D 01`, `FA 12`, `9A`, `F5`, `FA 80`, `99`, `FA 08`, `9A`, `FA 1F`, `F8 51 FF 00` | the firmware boots from block 0 and mounts with the new capacity |
| record and identity | `96` twice, `81 00 FF`, `82 51 00` | info record and config block saved |
| finalize | `81 51 04` | MP info sector written |

## Phase details

### Detect

- **Identify** (`9A`, `FA 0E`): chip ID, and whether the firmware is locked (`FA 0E` refused).
- **Flash ID** (`FA 00`): selects the part's FlashList record, hence geometry, ECC and the
  boot firmware image.
- **Registers and code** (`FA 13`, `FA 10`): the NAND trim values that go into the FTL
  configuration record.

### Erase and MP enable

- **Chip erase** (`FA 0C 00 33`): takes minutes.
- **Write-phase enable**: a [re-enable](#write-phase-re-enable) with the `rwfw` loader and
  two param blocks (A and B), closed by `FA 0B 3E`.

### Scan and program

Each group opens with a re-enable that loads the loader and params of its stage. In order:

- **Scratch self-test**: 32 pattern pages written to the scratch block, verified, erased.
- **Bad-block screen**: the first `0x90` blocks erased, test-written, read back
  ([screen](#bad-block-screen)).
- **FTL build A**: the FTL units activated and their map read back (`91`, `92`, `9a`), index
  pages (`99`) written.
- **Boundary loop A**: per FTL slot, a base block and ten blocks at a fixed stride, with
  marker reads and ECC bit-error maps (`42`, `43`, `47`).
- **FTL build B**, then the **address march and boundary loop B**: the same at a lower ECC
  strength; loop B's read-backs give the bad columns of the
  [column-skip table](#column-skip-table).
- **Housekeeping sweeps**: the die-wide erase, program and read-back test
  ([sweeps](#housekeeping-sweeps)).
- **Translation tables**: headers only (`ae`, `af`); the controller computes the content.
- **Housekeeping tables**: the encrypted format record (`21`), built from the sweep results
  ([format](nand.md#housekeeping-tables)).
- **Data superblocks**: a walk of the data blocks, the system fill and pre-pass, then the head
  pages with the free-block list and the pattern pages.
- **Firmware pass**: on both FTL system blocks, the anchor, lane bitmaps, boot firmware with its
  self-descriptor, ECC table, configuration and frame records in
  [program bursts](#program-bursts), then their read-back ([pages](nand.md#block-0-page-map)).

### Set info and reset

- **Leave MP mode** (`FA 1D 01`) and send the closing register block (`FA 12 00 90 00 09`).
- **Options** (`9A`, `F5`): the [options block](commands.md#f5-options-block) sets the record
  flags (UFDMode and friends).
- **Capacity** (`99`): capacity and usable blocks per lane ([how it is computed](#capacity)).
- **Reset** (`FA 08`): the firmware boots from the new block 0 and mounts with that capacity.
- **Check** (`9A`, `FA 1F`, `F8 51 FF 00`).

### Record and identity

- **Save the record** (`96`, twice): the firmware writes its info record. Without this, the
  next power-on reads an erased record and the stick comes up locked.
- **Identity** (`81 00 FF`): the config block from the backup, or AlcorMP's default block
  with a random serial when the stick had none (as from ROM mode).
- **Read back** (`82 51 00`).

### Finalize

- **MP info sector** (`81 51 04`): `FF` with AlcorMP's state block, the date, the capacity and
  `55 AA` ([format](structures.md#mp-info-sector)).

## Write-phase re-enable

Every programming group starts with the same sequence; only the loader and params change.

- **Enter** (`FA 1D`).
- **Upload the loader** (`FA 0A 00 10`): the stage's loader with its
  [patch window](structures.md#loader-patch-window) and the stage's edits.
- **Register blocks** (`FA 12`, `FA 13`, `FA 12`).
- **Params** (`FA 0B 01`): one or two [param blocks](#param-block).

AlcorMP's loaders by role (image names per controller, e.g. [F700](controllers/F700.md#ram-loaders)):

| role | groups |
|---|---|
| `rwfw` | MP enable, transitions, firmware pass and data tail |
| `sort` | screen, boundary loops, address march |
| `bct` | FTL builds (`99`, `03`), translation tables |
| `scan` | erase, program and copyback sweeps (`a0`, `a1`, `a2`, `b0`, `97`, `b1`) |
| `scan_rd` | read-back sweeps (`c0`, `c1`) |
| `scan_n` | die-wide erase (`a7`) |
| `hlf` | housekeeping tables (`21`, `22`) |

## Bad-block screen

The tool, per block in `0x00`-`0x8F`: erase, write page 1 with the test pattern, read it
back, erase. A block whose read-back differs or whose erase fails is **screen-bad** and
counts like a sweep-bad block in the lane bitmaps, spare choice, capacity and stored
block status.

AlcorMP does more per failing block: a walking-page retention march (pages 2, 4 … 256) and
extra probes of the next blocks. Their results feed nothing it writes.

Bad blocks are dropped by skip-and-compact: good physical blocks fill the logical slots in
order and the tail becomes `FFFF`.

## Housekeeping sweeps

After the FTL is built, the controller tests the whole die, one lane chunk per op, each
followed by a status poll. In order:

- **Erase** (`a0`), **SLC program** (`b0`), **SLC read-back** (`c0`), **erase** (`a0`).
- **Per lane**: write the boundary base (`97`), copy it into every block (`b1`), read back
  (`c1`).
- **Erase** everything not bad or system (`a2`, `a7`), then a **read check** (`a1`).

| sweep | blocks per op | does | returns |
|---|---|---|---|
| `a0`, `a2` | 1024 | erase (`a2`: multi-plane, two `60h` address cycles) | failures in a bitmap, read by `05` |
| `b0` | 64 | erase and program every page from the pattern buffer, SLC | failures (`05`) |
| `c0` | 32 | read every SLC page | 1 B per page |
| `97` | one page | program the boundary base page | — |
| `b1` | 64 | copy back the last boundary base into every page | failures (`05`) |
| `c1` | 16 | read every page with the copyback source's seed | 1 B per page |
| `a7` | the whole block space, one bit each | erase | — |
| `a1` | 2048 | read one page per block | 1 B per block: `0` ok, `CC`-`CE` flags |

Read values:

| value | meaning |
|---|---|
| `00`-`9F` | corrected bits after read-retry |
| `A0` | over the read-retry threshold |
| `B0` | uncorrectable |
| `E2` | block abandoned: too many failing pages |
| `FA` | skipped by the bitmap |

**Block status** is each block's worst `c0`/`c1` value, with `E0` ORed in when a `05` report
flagged it and `ED` on each boundary base. Above `9F` the block is **sweep-bad**. Block status
feeds the lane bitmaps, the lane counts and so the capacity, the housekeeping tables and the
spare-block choice. A block can pass the screen and still be sweep-bad.

Each read-back measures the program before it: without `b1`, `c1` reports `E2` on every page
and every block would come out bad.

Without the sweeps (`--bad-blocks quick` or `none`) the status table is zeros except for the
screen-bad blocks (`E0`).

## Capacity

The `99` record tells the firmware its size. Per FTL lane:

- **usable blocks** = lane blocks − marked blocks (bad, system) − 8 FTL map blocks − the
  module's reserve, and the first lane with room also gives up the system extra.
- **capacity** = sum of usable blocks × pages per block × sectors per page, capped by
  `--capacity`.

The reserve and system extra come from the part's line in AlcorMP's `FlashList.ini`
(`[MODULE_FETURE]`). The same marks become the lane bitmaps on block 0.

## Payloads

### Param block

`FA 0B 01`, 2048 B: tells the loader the geometry, ECC and page map of the group that
follows. A page can only be read back under the param it was written with. Built in a
fixed order (`payloads.build_param`; byte tables in `alcor/f700.py`):

| part | contents |
|---|---|
| base | geometry fields and the base block |
| page map | identity map of the block's pages; half for SLC groups, none for the screen |
| stage | bytes that grow as the format proceeds, with the stage's ECC mode |
| tokens | per-call switches: `A` (alternate page table), `scan`, `fw28`, `rows` |
| slack | spare bytes the frames leave over, less the bad columns, at `0x17` and `0x66` |

Example: the param of a firmware-pass group, as the stream builder asks for it:

```
param("B", "fw28", "rows", map="slc", slack=True)

base                    geometry fields and the base block
stage "tail"            7A=00, 3A=11, 4F=1e, 6F=00, 11=10; ECC t = 78 at 2E, 31, 4C;
                        the unit count at 10 and 22
"B"                     the B member of a pair: the base page table at 0x90
"fw28"                  15=1c: 28 sectors per page; 40=00; EC=ff ff ff ff
"rows"                  2C=20 01 02: 512-B ECC frames (650 B with parity)
map="slc"               page map of 256 entries (half the block: pseudo-SLC), count at 0x41
slack=True              0x17: the raw page bytes the frames leave over
```

`alcortool dev plan --params` prints every param of the stream as bytes.

### Column-skip table

`FA 0B 94`, 2048 B, sent before every boundary group and housekeeping step: the ECC frame
layout of the current param with the page's bad columns skipped. A frame grows by one byte
per bad column it spans.

| offset | field |
|---|---|
| `0x000` | entry count (1) |
| `0x010` | bad-column count (16-bit) |
| `0x020` | frame starts (16-bit each) |
| `0x0A0` | bad columns before each frame (16-bit each) |
| `0x0E0` | bad columns inside each frame |
| `0x200` | bad-column list (16-bit LE each), then `FF` |

Only boundary loop B lists columns. They are measured live: each column is weighted by its
flipped bits in the loop's read-backs, and the heaviest column of the heaviest frame is taken
until the page slack is used up or no error is left.

### Sweep skip bitmaps

`FA 0B 04`, 512 B, before every sweep op: bit i = block `first + 4i` of the op's lane chunk
(block i for `a7`, which also marks every block past the die). Skipped: blocks `c0` reported
bad, blocks a `05` report flagged, and each boundary base once its `97` group has run.

### Test pattern

Every test, scan and fill page is a slice of AlcorMP's 1.5-MB pattern (`LLF.dll`) or its
bitwise complement:

| payload | slice |
|---|---|
| `1e` scratch self-test | complement from 0 |
| `18` screen and address-march pages | complement from `0x80` |
| `03` 1-KB pattern buffer, k-th of a run | from `k // 2 × 0x800`; pattern for even k, complement for odd |
| `03` 4 KB | pattern from 0 |
| `1e` pre-pass, page p | pattern from p |
| `18` system fill, `45` placeholder, page p | pattern from `(p mod 0x173) × 0x1000` |
| `1e` system pages, page p | complement from `(p mod 0x173) × 0x1000` |

### Program bursts

The final system writes go one page at a time: `FA 0B 09` opens a burst at the reserved row `0x2002`,
the program follows, `FA 0B 08` closes it at the real block. After every programmed page a
status poll follows, and on verify passes a read-back (`1f`, `46`).

## Options

| option | effect |
|---|---|
| `--bad-blocks full` | screen and all sweeps (about an hour) |
| `--bad-blocks quick` (default) | screen only; skips the `b0`/`c0` and `97`/`b1`/`c1` sweeps |
| `--bad-blocks none` | no screen either: assumes a clean die |
| `--bad-blocks keep` | reuses the stick's stored block status; exits if there is none |
| `--verify none` (default) / `full` | drops / keeps the read-back checks |
| `--fill full` (default) / `minimal` | keeps / drops the pattern filler |
| `--erase blocks` (default) / `chip` | `chip` also drops the die-wide `a2`/`a7` erases and the `a1` check |
| `--skip GROUPS` | drops op groups by role (`provision.SKIP_GROUPS`) |
| `--capacity SIZE` | caps the reported capacity |

## Where alcortool differs from AlcorMP

| step | AlcorMP | alcortool |
|---|---|---|
| backup | none | read-only backup before any write |
| locked stick | not detected; drops the stick | detects the lock, waits for ROM mode on the same port |
| screen | retention march on failing blocks, result unused | marks them screen-bad |
| identity | from its INI | the backed-up config block, else AlcorMP's default |
| housekeeping host and user name | the machine's | `alcortool` |
| defaults | full screen, sweeps, verify | `quick`, no verify |
