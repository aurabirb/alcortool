"""The low-level format as a stream of Ops, in AlcorMP's order.

Phases, loaders, params and every payload: docs/llf.md. The stream is built
from geometry, the controller datums (f700), the shipped data and live reads of the run
(Live payloads); nothing is replayed from a capture.

Which RAM loader is resident decides what an FA 0B op does, so the builder tracks it:
`Stream.load(role)` emits the write-phase re-enable that uploads a loader, and every
FA 0B op is checked against the loader resident at that point (LoaderError).
"""
import collections
import datetime

from . import f700
from .f700 import (BOUNDARY_BASES, BOUNDARY_SWEPT, FTL_PAGE, HK_BLOCK, MIRROR_ANCHOR_PAGE,
                   MIRROR_BLOCK, R_ANCHOR_PH, R_C1, R_DATA, R_FWCOPY, R_HK, R_ONE, R_SCRATCH, R_SYS,
                   R_SYSREC, R_ZERO, SCAN_STRIDE, SCRATCH_BLOCK, SYS_BLOCK, TAIL_STATUS_READS)
from .nand import device_bootfw, llf_pattern, loader_image
from .payloads import (COMMIT_HEADERS, INDEX_HEAD, bad_columns, block_bitmap, build_anchor,
                       build_capacity_record, build_column_table, build_ds_header, build_index_page,
                       build_lane_bitmaps, build_loader_patch, build_mp_info, build_options, build_param,
                       default_config, failed_blocks, hk_page_list, hk_pages, hk_tables, lane_counts,
                       llf, llf_pair, marks_from_bitmaps, patch_bootfw, screen_bad_blocks, sweep_bad_blocks, tile)
from .protocol import SUBS, Live, LoaderError, Op, nand_cdb

# == SKIP GROUPS ==
# --skip drops ops by role; a dropped op takes its status poll along. "polls" drops the
# polls after FA 0B reads (the data is already back). None of these feeds a later payload.
SKIP_GROUPS = {
    "verify": "read-back checks",
    "screen": "bad-block screen, scratch self-test, address march",
    "fill": "LLF-pattern filler pages and the pre-pass",
    "hk": "housekeeping tables (bad-block table store)",
    "polls": "status polls after reads",
}


class Layout:
    """Where the stream works on the die: geometry-scaled counts and F700's block sets.
    The F700 values reproduce the captures; extents marked INFERRED are clean-die
    representatives of an unobserved full-capacity walk."""

    def __init__(self, geo):
        self.geo = geo
        self.units = geo.ftl_slots                         # FTL units (F700 8)
        self.cell_slices = geo.cell_slices                 # blocks per unit (F700 4)
        self.unit_block_count = self.units * self.cell_slices
        self.sys_pages = geo.pages_per_block // 2          # SLC half of a system block
        self.xlate_blocks = self.cell_slices               # ae/af blocks 0..3
        self.map_span = 0xC                                # 92 read-backs per unit block

    def ftl_unit_blocks(self):
        s = self.geo.superblock_stride
        return [s * u + i for u in range(1, self.units + 1) for i in range(self.cell_slices)]

    def data_superblocks(self):
        """F700's two (one valid choice); more stride-spaced for more planes (INFERRED)."""
        blks = list(f700.DATA_SUPERBLOCKS)
        for k in range(1, max(2, self.geo.planes) - len(blks) + 1):
            blks.append(max(f700.DATA_SUPERBLOCKS) + k * self.geo.superblock_stride)
        return blks[:max(2, self.geo.planes)]

    def data_walk_blocks(self):
        """The tail's data-superblock erase walk: a doubling-step family from geometry,
        then the F700 clean-die remainder (INFERRED)."""
        U, CS, P, N = self.units, self.cell_slices, self.geo.planes, self.geo.blocks
        main = []
        for n in range(4):
            bh = U * CS * 2 ** n + 1
            ah = U * bh - (U - 1 - n)
            k = 0
            while k < 12 and (ah + k * bh) // P < N:
                main.append((ah + k * bh) // P)
                k += 1
        pass_a = [0x8, 0x11, 0x19, 0x2A, 0x33, 0x3B, 0x44, 0x55, 0x5D]
        x3 = [(0x608 + k * 0xC1) // P for k in range(12)] + [(0xC09 + k * 0x181) // P for k in range(3)]
        return [0x0] + pass_a + main + [0x802] + x3


def address_march(blocks):
    """(sub, block, page) of the walking-ones address-line test, from the block count:
    erase pairs (11, page 0x100 = mode 1), page-1 write/read probes (18/19), and the
    three opener params it carries (01)."""
    ntop = max(2, blocks.bit_length())
    up = [1 << i for i in range(ntop - 1)]
    ups = [(1 << i) | 1 for i in range(1, ntop - 1)]
    down = [1 << i for i in range(ntop)][::-1]
    downs = [(1 << i) | 1 for i in range(1, ntop)][::-1]
    E = lambda b: [(0x11, b, 0x100), (0x11, b, 0)]
    W = lambda b: [(0x18, b, 1)]
    R = lambda b: [(0x19, b, 1)]
    P = [(0x01, 0, 0)]
    o = E(0) + W(0) + R(0)
    for b in up:
        o += E(b)
    o += E(0) + E(1) + W(1) + R(1) + E(0)
    for b in ups:
        o += E(b)
    o += E(1) + P + E(0) + W(0) + R(0)
    for b in up:
        o += R(b)
    o += E(0) + E(1) + W(1) + R(1) + R(0)
    for b in ups:
        o += R(b)
    o += E(1) + E(0) + W(0) + R(0)
    for b in down[:-1]:
        o += E(b) + R(0)
    o += E(1) + W(1) + R(1)
    for b in downs:
        o += E(b) + R(1)
    return o + P + P


class Stream:
    """An op list under construction, with the controller state its payloads follow: the
    resident loader and the stage (f700.STAGES)."""

    def __init__(self, geo, loader="rwfw", stage="enable"):
        self.geo, self.ops, self.loader, self.phase = geo, [], loader, ""
        self.last_param = self.last_pattern = self.last_block = None
        self._loaders = {}
        self.stage(stage)

    def stage(self, name):
        """Enter a stage: its param bytes, loader edits, ECC strength and unit count apply
        to every param and re-enable from here."""
        self.stage_name = name
        self.stage_bytes, self.loader_edits, self.ecc, units = f700.STAGES[name]
        self.units = self.geo.cell_slices if units else None

    def op(self, op, group=None):
        op.phase, op.group = self.phase, group
        self.ops.append(op)
        return op

    def check(self, sub):
        if sub not in f700.LOADERS[self.loader][1]:
            raise LoaderError(f"FA 0B {sub:02x} ({SUBS[sub].name}) is not served by the "
                              f"{self.loader} loader ({self.phase})")

    def cmd(self, name, payload=None, note=None, keep=None, group=None):
        op = Op.of(name, payload, note, keep=keep)
        if op.cdb[:2] == b"\xfa\x0b":
            self.check(op.cdb[2])
        return self.op(op, group)

    def nand(self, sub, direction, block=0, page=0, sectors=0, region=R_ZERO, payload=None,
             note=None, frame=None, count=None, keep=None, group=None):
        """One FA 0B op under the resident loader, then its status poll if it has one."""
        self.check(sub)
        self.op(Op(nand_cdb(sub, 0, block, page, sectors, region, frame, count), direction,
                   payload, note or SUBS[sub].name, keep), group)
        if SUBS[sub].polled:
            self.poll()

    def read(self, sub, block, note=None, region=R_ZERO, page=0, sectors=0, **kw):
        self.nand(sub, "in", block, page, sectors, region, note=note, **kw)

    def mark(self, sub, block=0, note=None, region=R_ZERO, page=0, count=None, group=None):
        self.nand(sub, "none", block, page, 0, region, note=note, count=count, group=group)

    def poll(self):
        self.cmd("poll", note="status poll")

    def erase(self, block, modes=(1, 0), group=None, keep=None):
        for mode in modes:
            self.nand(0x11, "in", block, mode << 8, note=f"erase {block:#x} mode {mode}", group=group, keep=keep)

    def program(self, sub, block, page, sectors, region, payload, note, frame=None, group=None):
        """A page write, erasing the block first when it starts a new block's burst."""
        if block != self.last_block:
            self.erase(block, group=group)
            self.last_block = block
        self.nand(sub, "none" if sectors == 0 or payload is None else "out", block, page, sectors,
                  region, payload, note, frame, group=group)

    def fresh(self, block):
        self.erase(block)
        self.last_block = block

    def status_reads(self, n):
        for _ in range(n):
            self.cmd("status")

    # -- write-phase re-enable and params --
    def load(self, role, *edits):
        """Write-phase re-enable uploading `role`'s RAM loader (FA 1D, FA 0A, FA 12/13/12);
        the patch carries the post-enable unit count (cell slices), ECC mode and stage edits."""
        edits = (*self.loader_edits, *edits)
        key = (role, self.ecc, edits)
        if key not in self._loaders:
            patch = build_loader_patch(self.geo, self.geo.cell_slices, self.ecc, edits)
            self._loaders[key] = loader_image(role, patch)
        self.cmd("mp_write")
        self.cmd("loader", self._loaders[key], note=f"loader {'+'.join((role, *edits))}")
        self.loader = role
        self.cmd("regs_write", f700.FA12_REGS[0])
        self.cmd("regs_read")
        self.cmd("regs_write", f700.FA12_REGS[1])

    def param(self, *tokens, map=None, slack=False, columns=None):
        """FA 0B 01: the stage's block plus the named PARAM_TOKENS ("A" or "B" first), the
        page map (None, "slc", "nomap") and the slack rule. columns(row, frames) -> reads ->
        [bad columns]: a boundary slot's measured bad columns cut the slack (Live)."""
        kw = dict(tables=[self.stage_bytes, *(f700.PARAM_TOKENS[t] for t in tokens)], units=self.units,
                  ecc_t=self.ecc, map=map, slack=slack)
        self.last_param = p = build_param(self.geo, slot_cols=None if columns is None else 0, **kw)
        if columns is not None:
            cols = columns(int.from_bytes(p[0x2E:0x30], "big"), p[0x15] // p[0x2D])
            p = Live(2048, lambda reads: build_param(self.geo, slot_cols=len(cols(reads)), **kw))
        self.cmd("param", p)


class Format(Stream):
    """The LLF stream. Each method is one phase, in order (docs/llf.md)."""

    def __init__(self, geo, identity=None, capacity_limit=None, fw_update=None, sweeps="full",
                 kept_status=None, bootfw=None):
        super().__init__(geo)
        self.L = L = Layout(geo)
        self.identity, self.capacity_limit, self.sweep_mode = identity, capacity_limit, sweeps
        self.fw_update, self.kept_status = fw_update, kept_status
        self.stored = bool(fw_update and fw_update.get("blocks"))
        self.bootfw = fw_update["image"] if fw_update else bootfw or device_bootfw(geo.fid)
        if fw_update:
            self.identity, self.capacity_limit = fw_update["identity"], fw_update["capacity"]
        self.anchors = ([fw_update["blocks"][b]["anchor"] for b in (SYS_BLOCK, MIRROR_BLOCK)] if self.stored
                        else [build_anchor(geo), build_anchor(geo, mirror=True)])
        self.SP, self.CS = L.sys_pages, L.cell_slices
        self.screen_pattern = llf("screen", geo.page_bytes)
        self.col_patterns = collections.defaultdict(list)    # per slot: the 03 pattern of each 92
        self._col_memo, self._hk = {}, {}
        self.boundary_used = set()                           # blocks the 04 bitmaps always skip
        self.known_bad = ({b for b in range(geo.blocks) if kept_status[b] > f700.SWEEP_FAILED}
                          if kept_status else set())
        self.used = {SYS_BLOCK, MIRROR_BLOCK, SCRATCH_BLOCK, HK_BLOCK, *range(L.xlate_blocks),
                     *L.ftl_unit_blocks(), *BOUNDARY_BASES, *L.data_superblocks()}
        self.used |= {b + k * SCAN_STRIDE for b in (*BOUNDARY_BASES, *BOUNDARY_SWEPT) for k in range(10)}

    # -- helpers --
    def index_page(self, target=None, flag=0):
        self.nand(0x99, "out", payload=build_index_page(target, flag), note="FTL index page")

    def pattern(self, k=0, sectors=2):
        """03 pattern buffer: the k-th 1 KB pair member, or 4 KB of the pattern."""
        self.last_pattern = pay = llf_pair(k) if sectors == 2 else llf("meta", sectors * 512)
        self.nand(0x03, "out", sectors=sectors, payload=pay)

    def slot_columns(self, slot):
        """columns(row, frames) for Stream.param: the slot's bad columns, measured from its
        raw 92 read-backs against the 03 pattern each was programmed with (Live)."""
        return lambda row, frames: self._slot_columns(slot, row, frames)

    def _slot_columns(self, slot, row, frames):
        def cols(reads):
            rds = reads.get(("cols", slot), ())
            k = (slot, len(rds), id(rds[0]) if rds else None, row, frames)
            if k not in self._col_memo:
                self._col_memo[k] = bad_columns(rds, self.col_patterns[slot], row, frames, self.geo.raw_page_span)
            return self._col_memo[k]
        return cols

    def columns(self, slot=None):
        """94 column-skip table for the current param's ECC frames; on boundary loop B,
        the slot's measured bad columns (Live)."""
        p = self.last_param
        row, fsec, sect = int.from_bytes(p[0x2E:0x30], "big"), p[0x2D], p[0x15]
        tbl = build_column_table(self.geo, row, fsec, sect)
        if slot is not None:
            cols = self._slot_columns(slot, row, sect // fsec)
            tbl = Live(2048, lambda reads: build_column_table(self.geo, row, fsec, sect, {0: cols(reads)}))
        self.nand(0x94, "out", 0x1C0, 0, 4, payload=tbl)

    def fw_param(self):
        """The param of the 28-sector system and data page groups."""
        self.param("B", "fw28", "rows", map="slc", slack=True)

    def skip_bitmap(self, first, n, stride=4, limit=None):
        known = frozenset(self.boundary_used)
        sp = self.SP
        self.nand(0x04, "out", sectors=1, payload=Live(512, lambda reads: block_bitmap(
            known | sweep_bad_blocks(reads, sp) | failed_blocks(reads), first, n, stride, limit)))

    def failure_report(self, first, n, stride=4):
        self.read(0x05, 0, sectors=1, keep=("05", first, n, stride))

    def lane_chunks(self, span):
        return [(o, min(span, self.geo.blocks - o) // 4) for o in range(0, self.geo.blocks, span)]

    def erase_sweep(self, sub, g):
        for o, n in self.lane_chunks(0x400):
            self.skip_bitmap(o | g, n)
            self.mark(sub, o | g, page=n)
            self.failure_report(o | g, n)

    def ftl_unit(self, col, index_pages, with_9a, base=None):
        """One FTL-unit column: index pages, then per cell slice a pattern buffer, the 90
        spare scans, 91 activate (+ 9a commit markers) and the 92 map read-backs; on a 9a
        column the 92s are the raw reads the bad columns are measured from."""
        cols = self.L.ftl_unit_blocks()[col::self.CS]
        for target, flag in index_pages:
            self.index_page(target, flag)
        for k in range(self.CS):
            self.pattern(k)
            for b in cols:
                self.read(0x90, b)
            if with_9a:
                for pg in (0x100, 0):
                    self.read(0x90, base, page=pg)
                self.read(0x91, base, region=R_ZERO, page=FTL_PAGE, sectors=1, frame=0x01)
                for b in cols:
                    self.read(0x9A, b, region=R_ZERO, page=FTL_PAGE, frame=0x01)
            else:
                for b in cols:
                    self.read(0x91, b, region=R_ONE, page=FTL_PAGE, frame=0x01)
            half = self.L.map_span // 2
            frames = ([*range(half), *(0x11 + 2 * j for j in range(self.L.map_span - half))]
                      if with_9a else range(self.L.map_span))
            for b in cols:
                for f in frames:
                    if with_9a:
                        self.col_patterns[col].append(self.last_pattern)
                    self.read(0x92, b, region=R_ZERO if with_9a else R_ONE, page=FTL_PAGE, frame=f,
                              keep=("cols", col) if with_9a else None)

    # -- phases --
    def enable(self):
        self.phase = "chip erase"
        self.cmd("chip_erase")
        self.cmd("loader", loader_image("rwfw", build_loader_patch(self.geo)), note="loader rwfw")
        self.param("A")
        self.param("B")
        for reg in f700.FA12_REGS:
            self.cmd("regs_write", reg)

    def scratch_test(self):
        self.phase = "scratch self-test"
        sb, nsect, scr = SCRATCH_BLOCK, self.geo.sectors_per_page, ("screen",)
        pat = llf("scratch", self.geo.page_bytes)
        for pg in range(32):
            self.program(0x1E, sb, pg, nsect, R_SCRATCH, pat, "scratch page", group=scr)
        for pg in range(32):
            self.read(0x1F, sb, "scratch verify", R_SCRATCH, pg, nsect, group=("verify", "screen"))
        self.param("A")
        self.mark(0x3E, group=scr)
        self.stage("scratch")
        self.param("B")
        for pg in range(32):
            self.read(0x1F, sb, "scratch verify", R_SCRATCH, pg, nsect, group=("verify", "screen"))
        self.erase(sb, group=scr)

    def screen(self):
        """Per block of the first SCREEN_BLOCKS: erase, write page 1 with the LLF pattern, read
        it back, erase. A block whose read-back differs or whose erase fails is bad: the lane
        marks, spares, capacity and stored table treat it as sweep-bad (bad_blocks).
        AlcorMP also reads the spare column and runs a retention march on such blocks; neither
        result feeds anything, so both are left out (docs/llf.md, bad-block screen)."""
        self.phase = "bad-block screen"
        self.param("A")
        self.load("sort")
        self.stage("ftl")
        self.param("A", map="nomap")
        g, scr, pat = self.geo, ("screen",), self.screen_pattern
        for blk in range(f700.SCREEN_BLOCKS):
            self.erase(blk, group=scr, keep=("screen erase", blk))
            self.nand(0x18, "out", blk, 1, g.sectors_per_page, payload=pat, frame=0, group=scr)
            self.read(0x19, blk, "screen verify", page=1, sectors=g.sectors_per_page, frame=0, group=scr,
                      keep=("screen", blk))
            self.erase(blk, group=scr)

    def ftl_build_a(self):
        self.phase = "FTL build"
        for c in range(self.CS):
            if c == 0:
                self.load("bct")
            self.param("A")
            self.param("B")
            self.ftl_unit(c, [(INDEX_HEAD, 0), (None, 0)], with_9a=False)

    def boundary_loop_a(self):
        """Per slot, 10 boundary blocks: 42 marker + 43 ECC map reads around erases."""
        self.phase = "boundary scan"
        self.param("A")
        self.load("sort")
        self.param("A")
        for g in range(self.CS):
            self.load("sort", "boundary")
            self.param("B", map="slc")
            for k in range(10):
                bk = BOUNDARY_BASES[g] + k * SCAN_STRIDE
                self.columns()
                self.index_page()
                self.erase(bk)
                self.pattern(0)
                self.read(0x42, bk)
                self.read(0x43, bk)
                self.erase(bk)
                self.erase(bk)
                self.pattern(1)
                self.read(0x42, bk)
                self.read(0x43, bk)
                self.erase(bk)
                self.mark(0x96)

    def ftl_build_b(self):
        self.phase = "FTL build"
        U = self.L.units
        for g in range(U):
            if g == 0:
                self.cmd("mp_write", note="write-phase toggle")
                self.load("bct")
            self.param("A")
            self.param("A" if g & 1 else "B")
            base = BOUNDARY_BASES[g // (U // self.CS)]
            self.ftl_unit(g // (U // self.CS), [(base, g & 1)], with_9a=bool(g & 1), base=base)

    def address_march_and_loop_b(self):
        """The address-line march at t=0x48, then boundary loop B: per slot 10 groups of
        a base block (47 marker) and a swept block (9b marker + 43 ECC map), under the
        slot's measured bad columns."""
        g = self.geo
        self.phase = "address march"
        self.param("A")
        self.stage("march")
        self.load("rwfw")
        self.param("A")
        self.load("sort")
        self.param("A", slack=True)
        opener = 0                                         # the march carries 3 opener params
        prev = None
        for sub, blk, pg in address_march(g.blocks):
            if sub == 0x11:
                self.erase(blk, modes=(1 if pg == 0x100 else 0,), group=("screen",))
            elif sub == 0x18:
                self.nand(0x18, "out", blk, pg, g.sectors_per_page, payload=llf("screen", 16384), frame=0,
                          group=("screen",))
            elif sub == 0x19:
                self.read(0x19, blk, "address-march read", page=pg, frame=0, group=("screen",),
                          sectors=g.sectors_per_page if prev == 0x18 else 2)
            elif opener < 2:
                self.load("sort")
                self.param("A", slack=True)
                opener += 1
            else:                                          # the last opens loop B's first group
                self.load("sort", "boundary")
                self.param("A", columns=self.slot_columns(0))
            prev = sub
        self.phase = "boundary scan"
        for grp in range(40):
            slot = grp // 10
            if grp and grp % 10 == 0:
                self.load("sort", "boundary")
                self.param("A", columns=self.slot_columns(slot))
            base, swept = BOUNDARY_BASES[slot], BOUNDARY_SWEPT[slot] + (grp % 10) * SCAN_STRIDE
            self.columns(slot)
            for k in range(2):
                self.index_page(base, 1)
                self.erase(base, modes=(1, 1, 0))
                self.pattern(k)
                self.mark(0x3A, 0x01)
                self.read(0x47, base)
                self.mark(0x3A, 0x00)
                self.erase(swept)
                self.read(0x9B, swept)
                self.read(0x43, swept, page=0x100)
                self.erase(swept)
                self.erase(base)
            self.mark(0x96)
        self.phase = "housekeeping sweeps"
        self.stage("march_end")
        self.load("rwfw")
        self.param("A")
        self.stage("sweeps")
        self.load("scan")
        self.param("A", "scan")

    def sweeps(self):
        """Die-wide erase/program/read test (docs/llf.md, housekeeping sweeps):
        a0, then with sweeps=full b0, c0, a0, 97+b1, c1; then a2/a7 erases; a1 check."""
        self.phase = "housekeeping sweeps"
        g, SP = self.geo, self.SP

        def chunks(step):
            return [(o, min(step, g.blocks - o) // 4) for o in range(0, g.blocks, step)]
        for i in range(8):
            self.erase_sweep(0xA0, i % 4)
            self.load("scan")
            self.param("A" if i < 3 else "B", "scan", map="slc" if i == 7 else None)
        if self.sweep_mode == "full":
            for i in range(4):                                  # b0: erase + SLC program
                self.pattern(0, 8), self.columns(), self.index_page()
                for o, n in chunks(0x40):
                    self.skip_bitmap(o + i, n)
                    self.mark(0xB0, o + i, count=n)
                    self.failure_report(o + i, n)
                self.mark(0x96)
                self.load("scan" if i < 3 else "scan_rd")
                self.param("B", "scan", map="slc")
            for i in range(4):                                  # c0: SLC read-back
                self.columns(), self.pattern(0, 8)
                for o, n in chunks(0x20):
                    self.skip_bitmap(o + i, n)
                    self.read(0xC0, o + i, region=R_SCRATCH, sectors=4, count=n, keep=("c0", o + i))
                self.mark(0x96)
                if i < 3:
                    self.load("scan_rd")
                    self.param("B", "scan", map="slc")
                else:
                    self.param("A", map="slc")
            self.load("scan")
            self.param("A", "scan")
            for i in range(4):                                  # a0 again
                self.erase_sweep(0xA0, i)
                self.load("scan")
                self.param("A", "scan")
            for i in range(4):                                  # 97 boundary base, b1 copyback
                base = BOUNDARY_BASES[i]
                self.columns(), self.index_page(base), self.pattern(0, 8)
                self.erase(base, modes=(1,))
                for pg in range(SP):
                    self.nand(0x97, "none", base, pg, g.sectors_per_page,
                              bytes((0x01, 0x5A, pg & 0xFF, pg >> 8)))
                self.boundary_used.add(base)
                for o, n in chunks(0x40):
                    self.skip_bitmap(o + i, n)
                    self.mark(0xB1, o + i, count=n)
                    self.failure_report(o + i, n)
                self.mark(0x96)
                self.load("scan" if i < 3 else "scan_rd")
                self.param("A", "scan")
            for i in range(4):                                  # c1: read-back
                self.index_page(BOUNDARY_BASES[i]), self.pattern(0, 8), self.columns()
                for o, n in chunks(0x10):
                    self.skip_bitmap(o + i, n)
                    self.read(0xC1, o + i, region=R_C1, sectors=4, count=n, keep=("c1", o + i))
                self.mark(0x96)
                if i < 3:
                    self.load("scan_rd")
                    self.param("A", "scan")
        if self.sweep_mode in ("full", "erase"):                    # die-wide erases
            if self.sweep_mode == "erase":
                self.boundary_used.update(BOUNDARY_BASES)
            for i in range(4):
                self.load("scan")
                self.param("B", "scan")
                self.erase_sweep(0xA2, i)
            self.load("scan_n")
            self.param("A")
            self.skip_bitmap(0, g.block_span, 1, g.blocks)
            for b in range(0, g.block_span, 0x100):
                self.mark(0xA7, b, page=0x100)
        if self.sweep_mode == "full":                               # read check
            for i in range(4):
                self.load("scan")
                self.param("A", "scan")
                self.columns()
                for o, n in self.lane_chunks(0x800):
                    self.skip_bitmap(o | i, n)
                    self.read(0xA1, o | i, page=n, sectors=1)
                self.mark(0x96)

    def translation_tables(self):
        self.phase = "translation tables"
        self.load("sort")
        self.param("A")
        self.load("bct")
        self.param("A")
        self.param("B", "scan")
        for blk in range(self.L.xlate_blocks):
            if blk:
                self.param("B", "scan")
            self.pattern(0, 8), self.columns()
            for pg in range(self.SP):
                self.program(0xAE, blk, pg, 0, R_DATA, None, "ae table")
                self.read(0xAF, blk, "ae table verify", R_DATA, pg, group=("verify",))
            self.erase(blk)
            self.mark(0x96)
        self.stage("tables")

    # -- per-stick state the tail derives from the screen and the sweeps --
    def bad_blocks(self, reads):
        """Screen-bad (page-1 read-back or erase failed), c0 sweep-bad, 05-flagged, and the
        kept table's bad blocks."""
        return (screen_bad_blocks(reads, self.screen_pattern) | sweep_bad_blocks(reads, self.SP)
                | failed_blocks(reads) | self.known_bad)

    def fw_spares(self, ds, reads):
        """Four good unused blocks of the data superblock's lane just below it."""
        if self.stored:
            return next(b["spares"] for b in self.fw_update["blocks"].values() if b["ds"] == ds)
        bad = self.bad_blocks(reads)
        lane = range(ds - self.CS, -1, -self.CS)
        return sorted([b for b in lane if b not in self.used and b not in bad][:4])

    def system_marks(self, reads):
        """Blocks the lane bitmaps mark: sweep-bad, and the ones the LLF allocates."""
        if self.stored:
            return marks_from_bitmaps(self.geo, self.fw_update["blocks"][SYS_BLOCK]["bitmaps"])
        marked = self.bad_blocks(reads) | sweep_bad_blocks(reads, self.geo.pages_per_block, "c1")
        marked |= {SYS_BLOCK, MIRROR_BLOCK, HK_BLOCK, *BOUNDARY_BASES}
        for ds in self.L.data_superblocks():
            marked |= {ds, *self.fw_spares(ds, reads)}
        return marked

    def capacity(self, reads):
        lanes = lane_counts(self.geo, self.system_marks(reads), self.L.unit_block_count // self.geo.lanes)
        return build_capacity_record(self.geo, lanes, self.capacity_limit)

    def hk_page(self, reads, pg):
        if "pages" not in self._hk:
            counts = lane_counts(self.geo, self.system_marks(reads), self.L.unit_block_count // self.geo.lanes)
            planned = (sum(counts) - f700.HK_RESERVE_BLOCKS) * self.geo.pages_per_block * self.geo.sectors_per_page
            if self.capacity_limit is not None:
                planned = min(planned, self.capacity_limit)
            self._hk["pages"] = hk_pages(self.geo, hk_tables(self.geo, reads, planned, datetime.date.today(),
                                                             self.kept_status,
                                                             screen_bad_blocks(reads, self.screen_pattern)))
        return self._hk["pages"][pg]

    def housekeeping(self):
        """The bad-block table store: 25 encoded pages written, then read back."""
        self.phase = "housekeeping tables"
        hk = ("hk",)
        self.load("hlf")
        self.param("A")
        self.load("rwfw")
        self.param("A")
        self.status_reads(TAIL_STATUS_READS["housekeeping"])
        self.erase(HK_BLOCK)
        self.load("hlf")
        self.param("B", map="slc")
        n = len(hk_page_list(self.geo).rstrip(b"\xff")) // 2
        for pg in range(n):
            self.nand(0x21, "out", HK_BLOCK, pg, 4, payload=Live(2048, lambda reads, pg=pg: self.hk_page(reads, pg)),
                      group=hk)
        for pg in range(n):
            self.read(0x22, HK_BLOCK, region=R_HK, page=pg, group=("verify", "hk"))

    def system_blocks(self):
        """Data-superblock walk, system fill, pre-pass, system records, data superblocks."""
        self.phase = "data superblocks"
        g, SP = self.geo, self.SP
        fill, verify = ("fill",), ("verify",)
        self.stage("tail")
        self.load("rwfw")
        self.param("B")
        self.load("rwfw")
        self.status_reads(TAIL_STATUS_READS["walk"])
        self.param("A")
        for blk in self.L.data_walk_blocks():
            self.erase(blk)
        self.param("B")
        self.param("B")
        for blk, apg in ((SYS_BLOCK, 0), (MIRROR_BLOCK, MIRROR_ANCHOR_PAGE)):   # system fill
            if blk != SYS_BLOCK:
                self.param("B")
            self.fresh(blk)
            for pg in range(SP):
                if pg == apg:
                    self.program(0x45, blk, pg, 14, R_ANCHOR_PH, llf("fill", 7168, pg), "anchor placeholder",
                                 frame=0x8A)
                else:
                    self.program(0x18, blk, pg, g.sectors_per_page, R_SYS, llf("fill", g.page_bytes, pg),
                                 "system fill", frame=0, group=fill)
            self.read(0x46, blk, "anchor placeholder verify", R_ANCHOR_PH, apg, 14, frame=0x8A, group=verify)
            self.erase(blk)
        self.status_reads(TAIL_STATUS_READS["prepass"])
        self.load("rwfw", "fw_data")
        self.param("B", "fw28", "rows", "scan", map="slc", slack=True)
        for blk, last in ((SYS_BLOCK, False), (MIRROR_BLOCK, True)):    # pre-pass + system records
            self.columns()
            self.fresh(blk)
            for pg in range(SP):
                self.program(0x1E, blk, pg, 28, R_DATA, llf("prepass", 14336, pg), "pre-pass", frame=0, group=fill)
            for pg in range(SP):
                self.read(0x1F, blk, "pre-pass verify", R_DATA, pg, 28, frame=0, group=("verify", "fill"))
            self.erase(blk)
            self.load("rwfw", "fw_data")
            self.param("B", "fw28", "rows", "scan", map="slc", slack=True)
            for pg in range(SP):
                self.program(0x45, blk, pg, 17, R_SYSREC, bytes(8704), "FTL system record (zeros)",
                             frame=0x0A, group=fill)
            for pg in range(8):
                self.read(0x46, blk, "system record verify", R_SYSREC, pg, 17, frame=0x0A, group=verify)
            self.erase(blk)
            if not last:
                self.mark(0x96)
                self.status_reads(TAIL_STATUS_READS["mirror"])
                self.load("rwfw", "fw_data")
                self.param("B", "fw28", "rows", "scan", map="slc", slack=True)
        for i, ds in enumerate(self.L.data_superblocks()):
            self.mark(0x96)
            self.status_reads(TAIL_STATUS_READS["data" if i == 0 else "data_next"])
            self.load("rwfw", "fw_data")
            self.param("B", "fw28", map="slc")
            self.status_reads(TAIL_STATUS_READS["data_mid"])
            self.load("rwfw", "fw_data")
            self.fw_param()
            self.columns()
            for pg in range(SP):
                if pg < f700.DS_HEAD_PAGES:
                    self.program(0x1E, ds, pg, 28, R_FWCOPY, build_ds_header(g, pg) + llf("fill", 14336, pg)[0x800:],
                                 "data superblock head")
                else:
                    self.program(0x1E, ds, pg, 28, R_SYS, llf("sys", 14336, pg), "data superblock page", group=fill)
            self.mark(0x96)
            self.load("rwfw", "fw_data")
            self.fw_param()
            self.columns()
            for pg in range(f700.DS_HEAD_PAGES):
                self.read(0x1F, ds, "data superblock head verify", R_FWCOPY, pg, 4, group=verify)

    def firmware(self):
        """Both system blocks: anchor, lane bitmaps, boot image with its descriptor, three
        commit pages in 09/08 bursts, system pages; then their read-back."""
        self.phase = "firmware"
        g, SP, verify = self.geo, self.SP, ("verify",)
        self.mark(0x96)
        for k in range(2):                                 # register reads for the commit records
            self.cmd("regs_read", keep=("fa13", k))
        self.load("rwfw")
        self.fw_param()
        nfw = -(-len(self.bootfw) // 0x1000)

        def fw_pages(lead):
            first = 1 if lead else 0                       # block 0's page 0 is the anchor
            return ([(first, "sysrec")] + [(first + 1 + k, k) for k in range(nfw)]
                    + [(first + 1 + nfw + k, f"commit{k}") for k in range(len(COMMIT_HEADERS))])

        def region(kind):
            return R_FWCOPY if isinstance(kind, int) and kind >= 2 else R_SYS

        ds_blocks = ([self.fw_update["blocks"][b]["ds"] for b in (SYS_BLOCK, MIRROR_BLOCK)] if self.stored
                     else self.L.data_superblocks())
        for (blk, lead), ds in zip(((SYS_BLOCK, True), (MIRROR_BLOCK, False)), ds_blocks):
            self.columns()
            self.fresh(blk)
            apg = 0 if lead else MIRROR_ANCHOR_PAGE
            if lead:
                self.program(0x45, blk, apg, 14, R_SYSREC, tile(self.anchors[0], 7168), "FTL anchor", frame=0x0A)
            for pg, kind in fw_pages(lead):
                if kind == "sysrec":
                    self.program(0x1E, blk, pg, 8, R_SYS, Live(4096, lambda reads, blk=blk: (
                        self.fw_update["blocks"][blk]["bitmaps"] if self.stored
                        else build_lane_bitmaps(g, self.system_marks(reads))) + llf_pattern(0, 0x800)),
                        "system record page (lane bitmaps)")
                elif isinstance(kind, int):
                    def fw_page(reads, blk=blk, lead=lead, kind=kind, pg=pg, ds=ds):
                        fw = patch_bootfw(g, self.bootfw, [p for p, k in fw_pages(lead) if isinstance(k, int) and k >= 1],
                                          blk, ds, self.fw_spares(ds, reads))
                        return fw[kind * 0x1000:(kind + 1) * 0x1000].ljust(0x1000, b"\x00") + llf("fill", 14336 - 0x1000, pg)
                    self.program(0x1E, blk, pg, 28, region(kind), Live(14336, fw_page) if kind < 2 else fw_page({}),
                                 "firmware page")
                else:
                    def commit_page(reads, blk=blk, lead=lead, pg=pg, k=int(kind[-1])):
                        fa13 = (reads.get(("fa13", 0 if lead else 1)) or [None])[-1]
                        hdr = COMMIT_HEADERS[k][0](g, blk, lead, (1 if lead else 0) + 1, nfw, fa13)
                        if k == 1:                         # the FTL record precedes the fill
                            return hdr + llf("fill", 14336 - len(hdr), pg)
                        return hdr + llf("fill", 14336, pg)[len(hdr):]
                    self.cmd("burst_open")
                    self.program(0x1E, blk, pg, 28, R_SYS, Live(14336, commit_page), "firmware commit page")
                    self.cmd("burst_close")
            for pg in range(fw_pages(lead)[-1][0] + 1, SP):
                if pg == apg:
                    self.program(0x45, blk, pg, 14, R_SYSREC, tile(self.anchors[1], 7168), "FTL mirror anchor",
                                 frame=0x0A)
                else:
                    self.program(0x1E, blk, pg, 28, R_SYS, llf("sys", 14336, pg), "system page", group=("fill",))
            self.mark(0x96)
            if lead:
                self.fw_param()
            else:
                self.load("rwfw")
                self.fw_param()
        for blk, lead in ((SYS_BLOCK, True), (MIRROR_BLOCK, False)):
            if not lead:
                self.mark(0x96)
                self.fw_param()
            self.columns()
            apg = 0 if lead else MIRROR_ANCHOR_PAGE
            if lead:
                self.read(0x46, blk, "anchor verify", R_SYSREC, apg, 14, frame=0x0A, group=verify)
            for pg, kind in fw_pages(lead):
                if isinstance(kind, str) and kind.startswith("commit"):
                    self.cmd("burst_open")
                    self.read(0x1F, blk, "commit page verify", R_SYS, pg, COMMIT_HEADERS[int(kind[-1])][1],
                              group=verify)
                    self.cmd("burst_close")
                else:
                    self.read(0x1F, blk, "firmware page verify", region(kind), pg, 8, group=verify)
            if not lead:
                self.read(0x46, blk, "mirror anchor verify", R_SYSREC, apg, 14, frame=0x0A, group=verify)
        self.mark(0x96)
        self.param("A", map="slc")
        self.stage("final")
        self.load("rwfw", "final")
        self.param("A", map="slc")

    def closing(self):
        """Leave MP mode, set info (F5 options, 99 capacity), reset, then the firmware
        saves its info record (96), the identity and the MP info sector."""
        self.phase = "closing"
        self.cmd("mp_leave")
        self.cmd("regs_close", f700.FA12_CLOSE)
        self.cmd("ident10", keep=("9a",))
        self.cmd("options", Live(512, lambda reads: build_options((reads.get(("9a",)) or [b""])[-1])))
        self.cmd("geometry_echo")
        self.cmd("capacity_rec", Live(512, self.capacity))
        self.cmd("reset")
        self.cmd("ident")
        self.cmd("after_reset")
        self.cmd("wp_check_end")
        self.cmd("save_record")                            # AlcorMP sends it twice
        self.cmd("save_record")
        self.cmd("write_config", self.identity or default_config())
        self.cmd("record", keep=("record",))
        mp_info = (self.fw_update["boot"] if self.fw_update and self.fw_update["boot"] else Live(
            512, lambda reads: build_mp_info(int.from_bytes(self.capacity(reads)[0x40:0x44], "little"),
                                             datetime.date.today())))
        self.cmd("write_mp_info", mp_info)


def trim(ops, skip):
    """`ops` without the SKIP_GROUPS named in `skip`; a dropped op takes its poll along."""
    drop, last, poll_too = set(skip) - {"polls"}, None, False
    for o in ops:
        poll = o.note == "status poll"
        if poll and (poll_too or ("polls" in skip and last is not None and last.direction == "in"
                                  and last.cdb[:2] == b"\xfa\x0b")):
            poll_too = False
            continue
        poll_too = False
        if o.group and drop & set(o.group):
            poll_too = True
            continue
        last = o
        yield o


def generate(geo, with_enable=True, identity=None, capacity_limit=None, fw_update=None,
             sweeps="full", skip=(), kept_status=None, bootfw=None):
    """The LLF stream. with_enable=False: the session is already MP-enabled (rwfw).
    sweeps: full | erase (die-wide erases only) | none. fw_update: a read_fw_layout result
    (or blocks=None for blind); the stream is then the firmware pass and closing only.
    kept_status: a stored block-status table that replaces the sweeps' verdict. bootfw: the
    boot image to write (default: the bundled one for the part)."""
    f = Format(geo, identity, capacity_limit, fw_update, sweeps, kept_status, bootfw)
    if with_enable:
        f.enable()
    for phase in (f.scratch_test, f.screen, f.ftl_build_a, f.boundary_loop_a, f.ftl_build_b,
                  f.address_march_and_loop_b, f.sweeps, f.translation_tables, f.housekeeping,
                  f.system_blocks):
        phase()
    fw_start = len(f.ops)
    f.firmware()
    f.closing()
    return list(trim(f.ops[fw_start:] if fw_update else f.ops, skip))


def phases(ops):
    """[(phase, op count)] in stream order."""
    out = []
    for o in ops:
        if out and out[-1][0] == o.phase:
            out[-1][1] += 1
        else:
            out.append([o.phase, 1])
    return [tuple(p) for p in out]


# == FIRMWARE-ONLY UPDATE: system-block read-back ==
class FwLayoutError(Exception):
    """The read-back does not match the model; nothing may be erased."""


def fw_layout_pages(lead):
    """{role: page} read back from a system block (block 0 leads with its anchor)."""
    first = 1 if lead else 0
    return {"anchor": 0 if lead else MIRROR_ANCHOR_PAGE, "sysrec": first,
            "chunk0": first + 1, "chunk1": first + 2}


def parse_fw_block(geo, blk, lead, pages, image):
    """A system block's per-stick records, checked against the builders: anchor magic,
    the system-record page's LLF tail, descriptor head and owner, and a byte-exact
    rebuild of the descriptor."""
    from .payloads import descriptor_mask
    be = lambda b, off: int.from_bytes(b[off:off + 2], "big")
    anchor, sysrec = pages["anchor"][:0x80], pages["sysrec"][:0x1000]
    fw = pages["chunk0"][:0x1000] + pages["chunk1"][:0x1000]
    if anchor[:5] != f700.ANCHOR_MAGIC:
        raise FwLayoutError(f"block {blk:#x}: anchor magic {anchor[:5].hex(' ')}")
    if sysrec[0x800:] != llf_pattern(0, 0x800):
        raise FwLayoutError(f"block {blk:#x}: system-record page tail is not the LLF pattern")
    if fw[0xF14:0xF20] != f700.FW_DESC_HEAD:
        raise FwLayoutError(f"block {blk:#x}: no firmware descriptor head at 0xf14")
    sys_blk, ds, nfw = be(fw, 0xF70), be(fw, 0xF72), be(fw, 0xFFE)
    spares = [be(fw, 0xF78), be(fw, 0xF7A), be(fw, 0xF80), be(fw, 0xF82)]
    if sys_blk != blk:
        raise FwLayoutError(f"block {blk:#x}: descriptor names system block {sys_blk:#x}")
    first = 2 if lead else 1
    rebuilt = patch_bootfw(geo, fw.ljust(nfw * 0x1000, b"\x00"), [first + k for k in range(1, nfw)],
                           sys_blk, ds, spares)
    bad = [i for i in descriptor_mask(geo, len(rebuilt)) if rebuilt[i] != fw[i]]
    if bad:
        raise FwLayoutError(f"block {blk:#x}: descriptor rebuild differs at {', '.join(hex(i) for i in bad[:8])}")
    same = sum(1 for i in range(len(fw)) if fw[i] == image[i]) / len(fw)
    return {"anchor": anchor, "bitmaps": sysrec[:0x800], "ds": ds, "spares": spares, "nfw": nfw,
            "image_match": same}


# == LIVE MP SESSIONS (device.MpSession) ==
# The controller state the housekeeping tables are written and read back under.
HOUSEKEEPING_STAGE = {"stage": "tables", "map": "slc"}


def enable_ops(geo):
    """Write-phase MP enable on a running stick, as AlcorMP before its first erase:
    FA 1D, the rwfw loader, the register blocks, the A/B param pair, FA 0B 3E."""
    st = Stream(geo)
    st.cmd("mp_write")
    st.cmd("wp_check")
    st.cmd("loader", loader_image("rwfw", build_loader_patch(geo)))
    st.cmd("regs_write", f700.FA12_REGS[0])
    st.cmd("regs_read")
    st.cmd("regs_write", f700.FA12_REGS[1])
    st.cmd("wp_check")
    st.param("A")
    st.param("B")
    st.cmd("mp_enabled")
    return st.ops


def reenable_ops(geo, role, stage="ftl", edits=(), tokens=("B",), map=None, slack=False):
    """A write-phase re-enable in `stage`: `role`'s loader (+ loader `edits`) and a param
    with `tokens`, `map` and `slack` (Stream.load, Stream.param)."""
    st = Stream(geo, stage=stage)
    st.load(role, *edits)
    st.param(*tokens, map=map, slack=slack)
    return st.ops


def housekeeping_pages(geo):
    return len(hk_page_list(geo).rstrip(b"\xff")) // 2
