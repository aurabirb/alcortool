# Tools

The tool itself, the AlcorMP data it reads, and the helpers built along the way.

## alcortool commands

Top-level commands are workflows: they check, back up, write and verify. Single steps live
under `alcortool dev`.

| command | does | writes | process |
|---|---|---|---|
| `list` | the Alcor drives attached | — | [identify](processes.md#identify) |
| `info` | controller, NAND, firmware, identity, write-protect, record | — | [identify](processes.md#identify) |
| `backup` | read-only backup to `firmware/dumps/` | — | [backup](processes.md#backup-and-restore) |
| `restore` | identity and disk from a backup; `--full` formats first | yes | [restore](processes.md#backup-and-restore) |
| `format` | backup, erase, bad-block scan, provision, verify | yes | [llf.md](llf.md) |
| `firmware` | rewrite the boot firmware only, keeping the data | yes | [firmware update](processes.md#firmware-only-update) |
| `identity` | show or change the USB/SCSI identity and serial | yes | [identity](processes.md#identity-change) |
| `unlock` | policy write-protect off (`--lock`: on), keeping the data | yes | [write-protect](processes.md#write-protect-on-and-off) |
| `extract` | the AlcorMP extractor, `alcormp-extract` | — | [extraction](#extraction) |
| `self-test` | offline checks of the builders and the stream | — | — |

`dev` steps:

| step | does | writes |
|---|---|---|
| `map` | print the command map: host commands, `FA 0B` subs, loaders | — |
| `plan` | the format stream offline for a part: phases, op counts, loaders, params | — |
| `probe` | the read-only "is this an Alcor" check | — |
| `reset` | USB bus reset | — |
| `read` | send a read command and hexdump the reply | — |
| `page` | `FA 01` page read | — |
| `code` | read CODE space (`FA 10`) to a file | — |
| `record` | decode the info record | — |
| `mp` | enter MP mode and send raw `FA 0B` reads | RAM only |
| `readflash` | read NAND pages in MP mode | RAM only |
| `housekeeping` | AlcorMP's stored format record: host, user, bad blocks | RAM only |
| `loader` | run the tool's probe loader: RAM and registers | RAM only |
| `nand info` / `read` / `erase` | the NAND through the probe loader: ID, ONFI page, raw pages, erase | `erase` |
| `writetest` | erase, program and verify the scratch block | yes |

## Code map

| path | holds |
|---|---|
| `alcortool` | launcher |
| `alcormp-extract` | standalone extractor: AlcorMP release → `alcormp/<version>/` |
| `alcor/cli.py` | command line |
| `alcor/flows.py` | workflows: info, backup and restore, format, firmware update, identity, dev steps |
| `alcor/provision.py` | the format stream, phase by phase, with RAM loader tracking |
| `alcor/payloads.py` | builders and decoders of every structure |
| `alcor/protocol.py` | the command map |
| `alcor/f700.py` | F700 values; another controller is another such file |
| `alcor/nand.py` | AlcorMP data lookup and the FlashList geometry |
| `alcor/device.py` | USB Bulk-Only over usbfs, sysfs, MP session, op driver |
| `alcor/ftlcrypt.py` | codec of AlcorMP's housekeeping tables |
| `alcor/asm51.py` | 8051 assembler and disassembler for the tool's loaders |
| `alcor/loaders/` | those loaders (`probe.a51`) |
| `alcor/selftest.py` | the offline checks |

## AlcorMP data

The tool needs AlcorMP's part list, ECC tables, test pattern and firmware images, which are
Alcor Micro's and not in the repository. They are extracted once per AlcorMP release.

### Extraction

- **Run** `./alcormp-extract RELEASE` (or `alcortool extract`): an archive, a folder or several.
- **Unpack**: `.zip` and `.tar` natively; `.rar`, `.7z`, `.iso` and self-extracting `.exe` with
  `7z` or `unrar`. Archives inside an unpacked tree are opened when it holds no AlcorMP folder.
  Members that would land outside the work directory are refused; nothing is executed.
- **Find** every AlcorMP folder (one holding `FlashList.afl`).
- **Name** the release by its date so names sort: `AlcorMP(200916.MD)` → `200916.MD`,
  `ALCOR_U2_MP_v20.09.16.00` → `200916.00`.
- **Write** the [data directory](#data-directory). Every controller family in the release is
  copied; each controller's file picks its own tree.

### Data directory

| path | contents |
|---|---|
| `<version>/flashlist.tsv` | flash ID → FlashList record, decrypted from `FlashList.afl` |
| `<version>/ecc_params.tsv` | BCH ECC parameter records, from `DLL/EccParamFile.Dll` |
| `<version>/LLF.dll` | AlcorMP's test pattern, verbatim (1.5 MB of data, not code) |
| `<version>/firmware/<api>/<ctl>/` | AlcorMP's `UfdApi/UfdApi_<api>/CTL/<ctl>/` verbatim |
| `<version>/SOURCE` | the archive or folder it came from, and the date |
| `local/` | hand-made additions; sorts before every release |

Lookup: the directories `$ALCORTOOL_DATA` (`:`-separated), the checkout's `alcormp/`,
`~/.local/share/alcortool/`, `/usr/local/share/alcortool/` and `/usr/share/alcortool/` are all
searched and their releases merged. Each lookup takes the first release that has the item,
newest first, so `local/` wins. The extractor writes to `$ALCORTOOL_DATA`, else `alcormp/`,
else `~/.local/share/alcortool/` (`--to DIR` overrides).

### flashlist.tsv

Columns `flash_id`, `vendor`, `part`, `record`: the full 662-byte FlashList record in hex,
with its vendor and part repeated as labels.

| offset | size | field |
|---|---|---|
| `0x000` | 16 | vendor |
| `0x010` | 32 | part |
| `0x030` | 6 | flash ID |
| `0x036` | 26 | geometry |
| `0x051` | 1 | cell mode and ECC code |
| `0x056` | 32 | module name |
| `0x076` | — | firmware slot names (`*.BIN`) |
| `0x256` | — | boot module token, e.g. `10_L04A` → `*_L04A_CB.BIN` |

`FlashList.afl` is encrypted with an RC4-like stream cipher whose swap is an XOR swap (so it zeroes the cell when i = j), keyed
with `ALCORFLASHCFG_SZ`:

- **header 1** = decrypt(bytes 0-255, the key);
- **header 2** = decrypt(bytes 256-511, header 1); it starts with the key and holds the record
  stride at `0x28` and the count at `0x2C`;
- **record r** = decrypt(its stride bytes after `0x200`, header 1 with byte 0 = r and
  byte 255 = ~r).

### ecc_params.tsv

Columns `key`, `record`. The key is record bytes 0-3: mode, codeword, family, t. A record's
first `0x132` bytes are the loader patch tail for that t; the same record sits on block 0 as
the [ECC parameter table](nand.md#ecc-parameter-table).

### Firmware tree

| path | contents |
|---|---|
| `SCAN_BIN/` | RAM loaders |
| `DGD_BIN/` | boot firmware images (`_CB`) |
| `BIN/` | per-part module images |
| `FlashList.ini` | per-module settings; `[MODULE_FETURE]` gives the lane reserves |
| `ModuleList.ini` | module list |

Images are AlcorMP's hex-text `.BIN` files.

## Backup dump

One directory per backup, `firmware/dumps/<flash id>_<serial>_<YYYYmmdd-HHMMSS>/`, written by
`alcortool backup` and before every write.

| file | contents |
|---|---|
| `replies/<name>.bin` | every read-only command's reply, the config block among them |
| `code.bin` | the 64-KB CODE space |
| `bootfw.bin` | the boot firmware, [captured](processes.md#capturing-the-boot-firmware) |
| `manifest.json` | chip, flash ID, serial, firmware revision, boot image, disk geometry and sha256 |
| `disk.img` | with `--data`: the logical disk, sparse |

## Internal tools

In the repository:

| tool | does |
|---|---|
| `alcortool self-test` | offline checks of the builders and the stream |
| `alcortool dev plan` | the whole format stream for a part, without a stick |
| `alcor/asm51.py` + `alcor/loaders/` | an 8051 assembler and the tool's own RAM loaders |
| `alcor/ftlcrypt.py` | encode and decode AlcorMP's housekeeping tables |

In `re/`, kept locally (they need evidence that is not in the repository, see `re/README.md`):

| tool | does |
|---|---|
| `oracle_selftest.py` | byte-exact check of the stream and builders against an AlcorMP capture |
| `stream_oracle.py`, `full_order_trace.py` | compare and align the generated stream with a capture |
| `pcap_scsi.py` | reconstruct Bulk-Only transactions from a usbmon capture |
| `diff_captures.py` | diff two AlcorMP captures to find where a setting lands |
| `extract_settings.py` | every AlcorMP setting's INI key and the structure it reaches |
| `sweep_probe.py`, `sweep_captures.py` | run single sweep ops on a stick; sweep facts from a capture |
| Ghidra scripts (`*.java`) | prepare and decompile 8051 CODE images: ROM, loaders, firmware |
| `cmdmap.py`, `triage.py` | vendor command call sites and function triage in AlcorMP's decompiled DLLs |
| `launch-vm.sh` | a Windows VM with AlcorMP, for captures and recovery |

## Adding a controller

- **Values file**: `alcor/<chip>.py` like `alcor/f700.py`: USB IDs, chip ID, firmware tree,
  loaders and the subs each serves, patch and param bytes, layout blocks, record offsets.
- **Reference file**: `docs/controllers/<chip>.md` with the same sections as
  [F700](controllers/F700.md).
- **NAND layout**: a column for the chip in [nand.md](nand.md#block-layout).
- **Commands**: any new ones in [commands.md](commands.md) and `alcor/protocol.py`.
- **Checks**: extend `alcor/selftest.py`.
