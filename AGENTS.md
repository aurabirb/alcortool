# Working on alcortool

A Linux tool for Alcor USB flash controllers (identify, back up, low-level format), and the
reverse engineering of AlcorMP and the F700 controller behind it.

## Map

| path | what | read first |
|---|---|---|
| `alcortool`, `alcor/` | the tool: it only reads extracted AlcorMP data | `README.md` (layout) |
| `alcormp-extract` | standalone extractor: AlcorMP release -> `alcormp/<version>/` | `docs/tools.md` |
| `docs/` | the settled reference: processes, commands, structures, NAND, format, tools | `README.md` (Alcor USB flash controllers) |
| `re/` | research status (open, ruled out), the AlcorMP code map, evidence notes, analysis tools; local only, gitignored | `re/RESEARCH.md`, `re/README.md` |

The command map is `alcor/protocol.py`; controller values are `alcor/f700.py`;
`alcortool dev map` prints both.

## Setup

```
./alcortool extract ALCOR_U2_MP_v20.09.16.00.rar   # usbdev.ru; needs 7z with RAR (7zip-rar) or unrar
./alcortool self-test                              # offline, no stick
```

The research evidence (decompiled DLLs, 8051 listings, captures, the reference trace) is
not in the repo: `re/README.md#regenerate-the-evidence` says how to make each piece and
where it goes (gitignored). `python3 re/oracle_selftest.py` checks the tool against
whatever of it exists and skips the rest.

## Checks before a commit

- `./alcortool self-test` passes.
- If the format stream, builders or loaders changed: `python3 re/oracle_selftest.py`
  (a byte-exact check against AlcorMP's capture, when it is present) and, with a stick the
  owner has set aside, a real `format` followed by a replug.

## Conventions

- One focused commit per tested change, made right away.
- `docs/` holds everything settled, `re/RESEARCH.md` what is open and what is ruled out:
  when you learn something, write it into the right one in the same change, and rewrite
  stale statements in place rather than appending. No per-session logs; git history is not
  where information lives.
- Code reads as a flow, abstracted from bytes: command bytes live in dense maps
  (`alcor/protocol.py`), controller values in `alcor/f700.py`, so another controller is a
  new datum file. Data structures should explain themselves; comments are short.
- Cite reference docs by name and topic ("per the lock gate in processes.md"),
  never by number ("per step 4").
- Generic code: nothing may depend on particular sticks, serials or this machine.
- Keep the code and docs DRY; extend the existing self-test checks rather than adding tests.
- CLI: top-level commands are workflows that do checks, backup, write and verify by
  default; single steps live under `alcortool dev`. Long operations show
  `[34.2%] what...` on one line.

## Hardware safety

- Ask before any write to a stick (`format`, `firmware`, `restore`, `identity` changes,
  `dev writetest`): it erases or rewrites the stick. Use only sticks the owner has set
  aside, selected by USB port and serial (`sdX` names move).
- A format takes about an hour (most of it the controller's sweeps). Run it detached and
  monitored, never under a kill timeout: a run killed mid-stream leaves the stick in ROM
  mode. ROM mode plus a completed format, ours or AlcorMP's in the VM (`re/launch-vm.sh`),
  recovers it; a locked stick needs the ROM-mode pin short (`docs/processes.md`).
- No root is needed with the udev rule in `README.md`; USB captures (usbmon) need root.
