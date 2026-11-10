# alcortool

A native Linux tool for Alcor USB flash controllers: identify, back up, and
low-level format (re-provision) a stick the way the vendor's Windows tool AlcorMP
does, and switch its write-protect off or on without touching the data.

> **A low-level format erases the stick and can leave it unusable.** It is checked
> against one controller (F700) and the NAND parts in [docs/controllers/F700.md](docs/controllers/F700.md).
> `info` and `backup` are read-only; run them first, and `format --dry-run` before formatting.

## Setup

Linux and Python 3.9+. Give your user access to the stick's USB node, then replug it:

```
echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="058f", TAG+="uaccess"' | sudo tee /etc/udev/rules.d/60-alcor.rules
sudo udevadm control --reload
```

A rebranded stick has another vendor ID; add the same rule for it.

## Use

These need nothing else:

```
./alcortool list                 # the Alcor drives attached
./alcortool info                 # controller, NAND, firmware, identity, write-protect, info record
./alcortool -v info              # + every record field, UFDMode, config, last format, NAND set-up
./alcortool backup --data        # read-only backup (identity, firmware, disk) to firmware/dumps/
./alcortool unlock               # remove the write-protect, keeping the data (--lock sets it)
./alcortool identity --serial S  # change the USB/SCSI strings or serial
./alcortool restore DUMP         # a backup's identity and disk
./alcortool help dev             # single steps: reads, the probe loader, the NAND through it, ...
```

Formatting and rewriting the firmware need AlcorMP's data (part list, ECC tables, firmware
images). Download `ALCOR_U2_MP_v20.09.16.00.rar` from usbdev.ru, install `unrar`, and run once:

```
./alcortool extract ALCOR_U2_MP_v20.09.16.00.rar
```

Then:

```
./alcortool format               # backup, erase, bad-block scan, provision, verify (destructive)
./alcortool firmware             # rewrite the boot firmware only, keeping the data
./alcortool restore --full DUMP  # format, then a backup's firmware, identity and disk
```

`-d sdX` picks the stick when more than one is attached. Destructive commands ask first
(`-y` to skip) and take `--dry-run`. A locked stick (`058f:9380`) is formatted from ROM
mode: `format` explains the pin short and waits for it. If a format fails, AlcorMP on
Windows (or in a VM with USB passthrough) can still recover the stick.

## Layout

```
alcortool          launcher (python3 -m alcor)
alcormp-extract    standalone extractor: AlcorMP release -> alcormp/<version>/ (run it first)
alcor/cli.py       command line
alcor/flows.py     workflows: info, backup/restore, format, firmware update, identity, dev steps
alcor/provision.py the format stream, phase by phase, with RAM-loader tracking
alcor/payloads.py  builders and decoders of every structure written or read
alcor/protocol.py  the command map: host commands, FA 0B NAND ops, Op
alcor/f700.py      controller datums (loaders, constants, layout); a new controller is a new file
alcor/nand.py      AlcorMP data lookup (releases) and the FlashList geometry
alcor/device.py    USB Bulk-Only over usbfs, sysfs, MP session, op driver
alcor/ftlcrypt.py  AlcorMP's FTL-table record codec
alcor/asm51.py     8051 assembler/disassembler for the tool's own RAM loaders
alcor/loaders/     those loaders (`probe.a51`: RAM and register access, no NAND)
alcor/selftest.py  offline checks (`alcortool self-test`)
docs/              reference: processes, commands, structures, NAND layout, format, tools
re/                research: the reverse engineering behind it (re/README.md)
alcormp/           extracted AlcorMP releases (gitignored)
firmware/dumps/    per-stick backups
```

## Alcor USB flash controllers

How an Alcor USB stick works inside, and how alcortool talks to it.

### How it works

1. **Mask ROM**: fixed code in the controller, an 8051. It runs USB, answers the vendor
   commands and reads the FTL anchor on NAND block 0.
2. **Boot firmware**: loaded from block 0 into an 8-KB RAM window, one page always resident
   and overlays swapped in on demand. The ROM reads the first two firmware pages into that
   window and jumps into them; from then on the firmware runs the stick, calling back into the
   ROM for USB and the vendor commands.
3. **Mount**: the firmware's FTL maps the disk's sectors onto NAND pages. It finds its info
   record page, with the capacity, identity, write-protect and lock. An erased record leaves
   the stick **locked** (`058f:9380`): vendor writes are refused until a format from ROM mode.
4. **USB**: the stick enumerates with the identity from its config block.

A format replaces the firmware with RAM loaders that do the NAND work, builds the FTL's
tables, and ends by saving the info record with `96`; without it the stick boots once and is
locked at the next power-on. Details: [docs/processes.md](docs/processes.md#power-on-and-boot).

### Reference

- [processes.md](docs/processes.md): how things happen: boot, mount, commands, identify,
  write-protect, identity, firmware update, recovery, backup; troubleshooting.
- [llf.md](docs/llf.md): the low-level format, its command sequences and payloads.
- [commands.md](docs/commands.md): every command, by who answers it, and their payloads.
- [structures.md](docs/structures.md): the info record page, info record, config block, firmware image,
  RAM loader and patch window.
- [nand.md](docs/nand.md): the NAND page formats and block layout; drawn in
  [nand_map.html](docs/nand_map.html).
- [tools.md](docs/tools.md): alcortool's commands and code, the AlcorMP data, backups, helpers.
- [controllers/F700.md](docs/controllers/F700.md): the F700's addresses, registers, loaders and values.

### Terms

| term | meaning |
|---|---|
| AlcorMP | Alcor's Windows mass-production tool, whose data and command sequences alcortool reuses |
| boot firmware | the controller's program, stored on NAND block 0 and run from RAM |
| CDB | the command bytes of a SCSI command; vendor commands are CDBs with Alcor's own opcodes |
| CE | chip enable: one NAND die selected |
| config block | the info record page sector with the stick's USB and SCSI identity and serial |
| FTL | flash translation layer: the firmware's map from disk sectors to NAND pages, with its tables on the NAND |
| FTL lane | a group of blocks the FTL allocates from (block mod 4 on F700) |
| info record page | the firmware's own page: capacity, flags, identity, write-protect, lock |
| LLF | low-level format: AlcorMP's full provisioning of a stick |
| locked | the lock gate closed by the info record: vendor writes refused |
| LUN | a logical unit the stick presents; the flash disk is one |
| mask ROM | the controller's fixed code: USB, vendor commands, boot |
| MP mode | AlcorMP's "mass production" mode: write-phase enabled and a RAM loader resident |
| NAND | the flash chip: pages are written once and erased a block at a time; blocks wear out or are bad from the start |
| overlay | a 4-KB part of the firmware loaded into the window on demand |
| param block | the 2-KB `FA 0B 01` block that tells a loader the geometry and ECC of what follows |
| patch window | the last 818 bytes of a loader upload: geometry, ECC table, NAND set-up |
| RAM loader | code a host uploads in place of the firmware to run NAND operations |
| ROM mode | only the mask ROM running, because block 0 could not be read (`058f:1234` on F700); a format can run from it |
| SLC, MLC | one or two bits per NAND cell; FTL system blocks use pseudo-SLC |
| sub | the operation byte of an `FA 0B` NAND command |
| superblock | a group of blocks across the lanes written together |
| UFDMode | AlcorMP's disk-mode setting, stored as flags in the info record |

### Not yet known

- The randomizer's seed rule, and how to read scrambled pages with ECC.
- The lock blocks' content format, and why the mount reads info record page sector 7.
- Why the firmware misreads its record after `FA 08` following the probe loader.
- How the RAM loaders reach most `FA 0B` subs (only nine go through the known switch).
- How the firmware builds its NAND set-up block.
- The translation table format.
- Commands `72`, `74`, `9D` and `FA 11`; the exact use of `71`, `73` and `83`.
- Other Alcor controller families: only the F700 is mapped.

## Licence

GPL-3.0 ([LICENSE](LICENSE)). AlcorMP and the data extracted from it are Alcor Micro's;
they are not part of this repository.
