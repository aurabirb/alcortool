# Processes

How the controller and the tool do things, step by step. Commands are in
[commands.md](commands.md), structures in [structures.md](structures.md) and
[nand.md](nand.md), the format in [llf.md](llf.md). Addresses of the routines involved are per
controller ([F700](controllers/F700.md#firmware-routines)).

## Power-on and boot

The mask ROM brings up USB and the NAND, loads the boot firmware from block 0, and the
firmware mounts the FTL. Each step can fail in a way the host can see.

- **ROM reset**: internal RAM cleared, the lock gate opened, write-protect masks reset.
- **NAND init**: the ROM resets the NAND, reads its ID and builds its read descriptors and
  the [NAND set-up block](structures.md#nand-set-up-block).
- **Anchor**: the [FTL anchor](nand.md#ftl-anchor) on block 0 page 0, or the mirror's.
  - Nothing readable: the controller stays in **ROM mode** (`058f:1234`), where only the
    mask ROM answers. A format runs from here.
- **Firmware load**: block 0 pages 2 and 3 are read in SLC mode into the overlay window:
  firmware page 0 and overlay 0. The ROM jumps into the firmware.
- **[Mount](#mount)**: the firmware finds the info record page and loads the info record and
  config block.
  - No info record page: defaults, with the capacity computed from the lane counts.
  - An erased record: capacity `0xFFFFFFFF`, write-protect on and the lock closed, so
    the stick comes up **locked**, and without a valid config block as `058f:9380`.
- **Lanes**: the first FTL lane with usable blocks is selected.
- **USB enumeration**: VID, PID and strings from the config block.

A controller reset (`FA 08`) reloads the firmware from NAND and re-enumerates, but does not
clear internal RAM. After the tool's probe loader has run, the reloaded firmware misreads its
record and identity; a USB bus reset brings it back cleanly. The cause is (unknown).

## Overlay loading

The firmware is larger than its 8-KB window, so it swaps overlays in on demand.

- **Request**: code asks for overlay n through the ROM's overlay loader.
- **Cache check**: if the index stored at the end of the window is already n, nothing happens.
- **Load**: otherwise the ROM starts a hardware page read of the overlay's NAND page, from
  one of its redundant copies; a copy that fails to read is dropped.
- Overlays 1-9 are stored scrambled; the firmware reads them through the transfer engine
  with a per-page seed.

## Mount

- **Find the info record page**: at a cold boot the firmware searches the free-list blocks for the
  info record page tag. The free list comes from the data superblock head page.
- **Read sector 0**, the [info record](structures.md#info-record). Uncorrectable: defaults.
- **Copy out** the sizes and turn the option bytes into flag bits. One of them, from byte
  `0x3A`, closes the lock gate; byte `0x2E` carries policy write-protect.
- **Read sectors 1, 2, 3 and 7**: the [config block](structures.md#config-block), validated by
  the ROM, and the lock blocks.
- No check of a CRC or sequence number is made.

## Vendor command dispatch and the lock gate

- **CBW**: the command lands in the USB command buffer.
- **SCSI switch**: standard opcodes go to their handlers; vendor opcodes go on.
- **Lock gate**: if the lock bit and the firmware's status bit are both set, the
  [gated commands](commands.md#vendor-commands) are refused with `5/26/00`.
  - The lock bit is cleared only by the power-on reset or `F5`, and `F5`, `96` and `FA 08` are
    gated themselves: **no command reopens a closed gate**. The way out is ROM mode and a
    format.
- **Vendor switch**: each opcode to its ROM handler; `FA` has a second switch on CDB byte 1.
- **`FA 0B`**: the CE is checked against the limit from the patch window, then the resident
  loader's slot 4 handles the sub, through to the NAND controller registers.
- **Completion**: the handler's result decides whether the ROM sends a passing or failing CSW,
  or the loader has already finished the transaction.

## Identify

What `alcortool info` does; everything here is read-only.

- **Is this an Alcor?** A stick with Alcor's vendor ID (`058f`) is taken as one. Any other
  USB disk is probed: it must answer `9A` with a plausible chip ID (not `0000` or `FFFF`) and
  `82 51 01` with a block starting `99 07`. A rebranded stick passes; another make of
  controller fails. Its INQUIRY revision can hint at the make (Phison sticks often report
  `PMAP` (?)).
- **Controller**: chip ID and write-protect from `9A`; the lock from whether `FA 0E` is
  refused.
- **NAND**: flash ID (`FA 00`), matched to AlcorMP's FlashList for the part and geometry.
- **Firmware**: the boot firmware in the CODE window (`FA 10`), matched to the known images.
- **Identity**: the config block (`82 51 01`) and INQUIRY.
- **Record**: the info record (`82 51 00`), decoded: sizes, flags, UFDMode, lane counts;
  with `-v` also the MP info sector (date and capacity of the last format).

## Reading the NAND

AlcorMP's way, used by the format and `alcortool dev readflash`.

- **MP mode**: `FA 1D`, a loader upload, the register blocks.
- **Param**: the `FA 0B 01` param block of the group that wrote the page.
- **Read**: `FA 0B 19`, `1F` or `46`, then the status poll.

A page decodes only under the param it was programmed with. Under any other the read does
not fail: the poll says ok, the first 512 bytes can even be right, and the rest is stable
noise (status tail `42 b0` where a good page ends `40 00`). To read a page the format wrote,
send the param its group used (`alcortool dev plan --ops N` shows it). Only the param matters:
neither the loader's edits nor the column-skip table change the result, and changing the
region or sector count of the read only changes the noise. `dev readflash` sets the context
with `--loader` and `--param`; guesses with `--region` or `--sectors` give noise, not an error. Block 0's system area
has a fixed recipe: `FA 0B 46`, frame `20 02 8A`, 14 sectors.

MP mode lives in RAM: a replug returns the stick to normal.

## Reading the NAND without AlcorMP

The tool's probe loader reads the NAND with no AlcorMP code or data (`alcortool dev loader`,
`dev nand`).

- **Patch window from the stick**: `FA 13` reads the firmware's NAND set-up block and
  geometry, which become the patch window, so the NAND controller ends up configured as the
  firmware had it.
- **Upload** the probe loader (`FA 0A`).
- **Probe subs** (`E0`-`E5`): internal RAM and XDATA, NAND ID and parameter page, raw pages
  (with the `DA` prefix for FTL system blocks), block erase ([commands](commands.md#probe-loader-commands)).
- **USB bus reset** at the end, so the firmware restarts cleanly.

Raw reads return data, parity and spare as stored, without ECC correction; scrambled pages
stay scrambled, so a raw block-0 read is not a firmware backup.

## Saving the info record page

- **`96`**: the ROM builds the info record from RAM in its reply buffer and calls the
  firmware, which needs a vendor command to have passed the gate.
- **`81 00 FF`** or **`81 51 nn`**: the host's 512 bytes become sector 1 or sector nn.
- **Write**: the firmware loads the current info record page (or `FF` when there is none),
  replaces the sector, takes a fresh block from the free list, erases it, tags it, programs
  the page and releases the old block.

Starting from the current page is why each command changes only its own sector, and why a
format that sends `81 00 FF` but not `96` leaves an erased sector 0.

## Write-protect on and off

`alcortool unlock` (`--lock` to set it), keeping the data:

- **Check**: refuses on a locked stick (that needs a format) or an unreadable record; notes a
  hardware switch, which overrides the policy flag.
- **Back up**, after confirmation.
- **`9B`**: policy write-protect changes in RAM, at once.
- **`96`** twice: the info record is saved with the new flag.
- **USB reset**, then **verify**: the identify flag, the record byte and a test sector write
  agree, and nothing else in the record changed.

## Identity change

`alcortool identity` (`--vendor`, `--product`, `--serial`, or `--load FILE`):

- **Read** the current config block (`82 51 01`).
- **Build** the new one from it: strings and checksum change, everything else is kept.
- **Back up**, show the changed bytes, confirm.
- **Write** (`81 00 FF`) and read back.

## Firmware-only update

`alcortool firmware` rewrites the two FTL system blocks with a boot image and keeps the FTL,
the housekeeping block and the data.

- **Back up**, then enter MP mode without a chip erase.
- **Read back** each FTL system block's anchor, system info record page and the first two firmware
  blocks (`FA 0B 46`).
- **Check**: anchor magic, LLF tail, the self-descriptor, a byte-exact rebuild of it, and the
  same lane bitmaps in both copies. Any mismatch: refuse.
- **Write** the format's firmware pass with those values, then the closing set-info with the
  current capacity (or `--capacity`), identity and boot sector.

It needs a working stick: a locked one has no `FA` access, a blank one has nothing to keep.
`--dry-run` stops after the read-back (MP mode is RAM only: replug afterwards). `--blind` skips the read-back and writes the
clean-die layout with a generated identity, for a stick in ROM mode; whether it then mounts is
untested.

## ROM mode and recovering a locked stick

A locked stick refuses every write, including the ones that would unlock it. The way back
goes around the firmware.

- **Detect**: `alcortool format` sees the lock (`FA 0E` refused) and explains the replug.
- **Force ROM mode**: with the stick unplugged, short two data pins of one NAND channel,
  plug in, release after 1-3 s. On a TSOP48 NAND these are pins 29 and 30 (I/O0 and I/O1);
  on a BGA NAND short two of the controller's `FMDATL` (NAND data) pins instead. The ROM cannot read
  block 0 and stays in ROM mode (`058f:1234`).
- **Wait**: the tool waits for `058f:1234` on the same port (`--rom-wait`, 10 minutes by
  default). Nothing is written before.
- **Format**: the full [low-level format](llf.md) runs from ROM mode.

In ROM mode, INQUIRY answers with the ROM's default identity, `9A` with the chip ID and empty
fields, and `FA 0E`, `FA 00`, `FA 10` and `FA 13` work. An interrupted format also leaves the
stick here; running the format again recovers it.

## Backup and restore

`alcortool backup` (read-only; also automatic before every write):

- **Replies**: every read-only command's reply, the config block among them.
- **CODE**: the 64-KB CODE space (`FA 10`).
- **Manifest**: chip, flash ID, serial, firmware revision and image, disk geometry.
- **Disk** (with `--data`): every sector of the logical disk.

`alcortool restore DUMP`:

- `--identity`: the config block only.
- `--data`: the disk image only.
- `--full`: a format that writes the dump's boot image and identity, then the disk image.

The FTL is not restored byte for byte: the format rebuilds it and the disk content goes back
on top. A raw NAND dump cannot serve as a backup (scrambled pages, device-made tables).

## Troubleshooting

- **`058f:9380`, capacity `0xFFFFFFFF`, vendor commands refused**: locked; the info record is
  erased or has the lock flag. Recover through [ROM mode](#rom-mode-and-recovering-a-locked-stick).
- **`058f:1234`, "Generic USB Flash Disk 7.76"**: ROM mode; the NAND or block 0 is unreadable,
  or a format was interrupted. Run `alcortool format`.
- **Writes refused with `7/27`**: write-protected. `alcortool unlock`, unless the hardware
  switch is on.
- **Garbled INQUIRY or an "empty" record after `dev nand`**: the firmware reloaded after the
  probe loader; replug or reset the USB port.
- **"not an Alcor controller"**: the stick fails the [identify probe](#identify); another make
  of controller.
- **Not found, "mounted, or no permission"**: unmount its partitions; install the udev rule
  for its vendor ID (README).
