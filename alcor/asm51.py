"""A small 8051 assembler and disassembler for the tool's own RAM loaders (alcor/loaders/).

One opcode table serves both directions. Source syntax: `label:`, `NAME EQU expr`,
`ORG expr`, `DB expr|"text",...`, `DW expr,...`, `; comment`; numbers as 0x12, 12h or 18;
bits as `ACC.7` or `0x20.3`; expressions are Python arithmetic over numbers and symbols.
Jumps are written as LJMP / LCALL / SJMP (AJMP / ACALL only disassemble)."""
import re

# == OPCODE TABLE ==
# operand kinds: literals (A, C, AB, DPTR, @DPTR, @A+DPTR, @A+PC, Rn, @Ri) and
# dir (direct byte), imm (#byte), imm16 (#word), bit, nbit (/bit), rel, a16, a11
_SPEC = """
00 NOP | 02 LJMP a16 | 03 RR A | 04 INC A | 05 INC dir | 10 JBC bit,rel | 12 LCALL a16 | 13 RRC A
14 DEC A | 15 DEC dir | 20 JB bit,rel | 22 RET | 23 RL A | 24 ADD A,imm | 25 ADD A,dir | 30 JNB bit,rel
32 RETI | 33 RLC A | 34 ADDC A,imm | 35 ADDC A,dir | 40 JC rel | 42 ORL dir,A | 43 ORL dir,imm
44 ORL A,imm | 45 ORL A,dir | 50 JNC rel | 52 ANL dir,A | 53 ANL dir,imm | 54 ANL A,imm | 55 ANL A,dir
60 JZ rel | 62 XRL dir,A | 63 XRL dir,imm | 64 XRL A,imm | 65 XRL A,dir | 70 JNZ rel | 72 ORL C,bit
73 JMP @A+DPTR | 74 MOV A,imm | 75 MOV dir,imm | 80 SJMP rel | 82 ANL C,bit | 83 MOVC A,@A+PC
84 DIV AB | 85 MOV dir,dir | 90 MOV DPTR,imm16 | 92 MOV bit,C | 93 MOVC A,@A+DPTR | 94 SUBB A,imm
95 SUBB A,dir | A0 ORL C,nbit | A2 MOV C,bit | A3 INC DPTR | A4 MUL AB | B0 ANL C,nbit | B2 CPL bit
B3 CPL C | B4 CJNE A,imm,rel | B5 CJNE A,dir,rel | C0 PUSH dir | C2 CLR bit | C3 CLR C | C4 SWAP A
C5 XCH A,dir | D0 POP dir | D2 SETB bit | D3 SETB C | D4 DA A | D5 DJNZ dir,rel | E0 MOVX A,@DPTR
E4 CLR A | E5 MOV A,dir | F0 MOVX @DPTR,A | F4 CPL A | F5 MOV dir,A
"""
# ops with an @Ri (i = 0, 1) form at base + i and an Rn (n = 0..7) form at base + 2 + n
_RI_RN = {0x06: "INC", 0x16: "DEC", 0x26: "ADD A,", 0x36: "ADDC A,", 0x46: "ORL A,", 0x56: "ANL A,",
          0x66: "XRL A,", 0x76: "MOV {},imm", 0x86: "MOV dir,{}", 0x96: "SUBB A,", 0xA6: "MOV {},dir",
          0xB6: "CJNE {},imm,rel", 0xC6: "XCH A,", 0xE6: "MOV A,", 0xF6: "MOV {},A"}
_RI_ONLY = {0xD6: "XCHD A,", 0xE2: "MOVX A,", 0xF2: "MOVX {},A"}      # @R0/@R1 only
_RN_ONLY = {0xD8: "DJNZ {},rel"}                                      # R0..R7 only


def _table():
    ops = {}
    for item in filter(str.strip, re.split(r"[|\n]", _SPEC)):
        code, rest = item.split(None, 1)
        mn, _, args = rest.strip().partition(" ")
        ops[int(code, 16)] = (mn, tuple(args.split(",")) if args else ())

    def form(base, pattern, regs):
        for k, r in enumerate(regs):
            if "{}" in pattern:
                text = pattern.format(r)
            else:
                text = pattern + r if pattern.endswith(",") else f"{pattern} {r}"
            mn, _, args = text.partition(" ")
            ops[base + k] = (mn, tuple(args.split(",")))

    ri, rn = ("@R0", "@R1"), tuple(f"R{n}" for n in range(8))
    for base, pattern in _RI_RN.items():
        form(base, pattern, ri)
        form(base + 2, pattern, rn)
    for base, pattern in _RI_ONLY.items():
        form(base, pattern, ri)
    for base, pattern in _RN_ONLY.items():
        form(base, pattern, rn)
    for page in range(8):
        ops[0x01 + 32 * page] = ("AJMP", ("a11",))
        ops[0x11 + 32 * page] = ("ACALL", ("a11",))
    return ops


OPS = _table()
SIZE = {"dir": 1, "imm": 1, "imm16": 2, "bit": 1, "nbit": 1, "rel": 1, "a16": 2, "a11": 1}
LITERALS = {"A", "C", "AB", "DPTR", "@DPTR", "@A+DPTR", "@A+PC", "@R0", "@R1", *(f"R{n}" for n in range(8))}
SFR = {"P0": 0x80, "SP": 0x81, "DPL": 0x82, "DPH": 0x83, "PCON": 0x87, "TCON": 0x88, "TMOD": 0x89,
       "P1": 0x90, "SCON": 0x98, "P2": 0xA0, "IE": 0xA8, "P3": 0xB0, "IP": 0xB8, "PSW": 0xD0,
       "ACC": 0xE0, "B": 0xF0}


def size(opcode):
    return 1 + sum(SIZE.get(k, 0) for k in OPS[opcode][1])


# == DISASSEMBLER ==
def disasm(code, org=0):
    """[(address, bytes, text)] of a linear decode of `code` loaded at `org`."""
    out, pc = [], 0
    names = {v: k for k, v in SFR.items()}
    while pc < len(code):
        op = code[pc]
        mn, kinds = OPS.get(op, ("DB", ()))
        n = min(size(op) if op in OPS else 1, len(code) - pc)
        raw = code[pc:pc + n]
        args, i, end = [], 1, org + pc + n
        operands = list(raw[1:])
        if op == 0x85:                                   # MOV dir,dir encodes the source first
            operands = operands[::-1]
        for k in kinds:
            if k in LITERALS:
                args.append(k)
                continue
            b = operands[i - 1] if i - 1 < len(operands) else 0
            if k == "imm16" or k == "a16":
                v = (b << 8) | (operands[i] if i < len(operands) else 0)
                args.append(("#" if k == "imm16" else "") + f"0x{v:04X}")
                i += 2
                continue
            i += 1
            if k == "dir":
                args.append(names.get(b, f"0x{b:02X}"))
            elif k == "imm":
                args.append(f"#0x{b:02X}")
            elif k in ("bit", "nbit"):
                byte, bit = (0x20 + b // 8, b % 8) if b < 0x80 else (b & 0xF8, b & 7)
                args.append(("/" if k == "nbit" else "") + f"{names.get(byte, f'0x{byte:02X}')}.{bit}")
            elif k == "rel":
                args.append(f"0x{(end + (b - 256 if b > 127 else b)) & 0xFFFF:04X}")
            elif k == "a11":
                args.append(f"0x{(end & 0xF800) | ((op >> 5) << 8) | b:04X}")
        text = mn if op in OPS else f"DB 0x{op:02X}"
        out.append((org + pc, raw, f"{text} {','.join(args)}".strip()))
        pc += n
    return out


_BRANCH = {"LCALL", "ACALL", "LJMP", "AJMP", "SJMP", "JZ", "JNZ", "JC", "JNC", "JB", "JNB", "JBC", "CJNE", "DJNZ"}


def reached(code, entries):
    """Addresses of the instructions direct flow reaches from `entries` in a 64-KB CODE
    image, following calls; computed jumps (JMP @A+DPTR) end a path."""
    seen, todo = set(), list(entries)
    while todo:
        pc = todo.pop()
        while pc not in seen and pc < len(code):
            seen.add(pc)
            _, raw, text = disasm(code[pc:pc + 3], pc)[0]
            mn = text.split()[0]
            if mn in _BRANCH:
                todo.append(int(re.findall(r"0x([0-9A-F]{4})", text)[-1], 16))
            if mn in ("LJMP", "AJMP", "SJMP", "RET", "RETI") or mn == "JMP":
                break
            pc += len(raw)
    return seen


def targets(code, org):
    """Every address a branch in a linear decode of `code` names."""
    out = set()
    for _, _, text in disasm(code, org):
        if text.split()[0] in _BRANCH:
            out.add(int(re.findall(r"0x([0-9A-F]{4})", text)[-1], 16))
    return out


# == ASSEMBLER ==
class AsmError(Exception):
    pass


def _split(s):
    """Split operands on commas outside quotes."""
    parts, cur, quote = [], "", None
    for ch in s:
        if quote:
            cur += ch
            quote = None if ch == quote else quote
        elif ch in "'\"":
            cur, quote = cur + ch, ch
        elif ch == ",":
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    return [p for p in parts + [cur.strip()] if p]


def _strip_comment(line):
    """`line` up to its first `;` outside a quoted string."""
    quote = None
    for i, ch in enumerate(line):
        if quote:
            quote = None if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch == ";":
            return line[:i]
    return line


def _num(tok):
    tok = tok.strip()
    if re.fullmatch(r"[0-9][0-9A-Fa-f]*[hH]", tok):
        return int(tok[:-1], 16)
    return int(tok, 0)


class _Asm:
    def __init__(self, org, symbols):
        self.org, self.sym, self.final = org, dict(SFR, **(symbols or {})), False

    def value(self, expr, pc):
        expr = re.sub(r"\b[0-9][0-9A-Fa-f]*[hH]\b", lambda m: str(_num(m.group())), expr.strip())
        expr = expr.replace("$", str(pc))
        names = dict(self.sym)
        try:
            return int(eval(expr, {"__builtins__": {}}, names))   # noqa: S307 (our own sources)
        except NameError:
            if self.final:
                raise AsmError(f"undefined symbol in {expr!r}")
            return 0
        except Exception as e:
            raise AsmError(f"bad expression {expr!r}: {e}")

    def bit(self, expr, pc):
        m = re.fullmatch(r"(.+)\.([0-7])", expr.strip())
        if not m:
            return self.value(expr, pc)
        byte, n = self.value(m.group(1), pc), int(m.group(2))
        if 0x20 <= byte < 0x30:
            return (byte - 0x20) * 8 + n
        if byte >= 0x80 and byte % 8 == 0:
            return byte + n
        raise AsmError(f"{expr}: not bit-addressable")

    def candidates(self, mn, args):
        for op, (m, kinds) in OPS.items():
            if m != mn or len(kinds) != len(args) or m in ("AJMP", "ACALL"):
                continue
            ok = True
            for k, a in zip(kinds, args):
                u = a.upper()
                if k in LITERALS:
                    ok = u == k
                elif u in LITERALS:
                    ok = False
                elif k in ("imm", "imm16"):
                    ok = a.startswith("#")
                elif k == "nbit":
                    ok = a.startswith("/")
                else:
                    ok = not a.startswith(("#", "/", "@"))
                if not ok:
                    break
            if ok:
                yield op, kinds

    def encode(self, mn, args, pc):
        found = list(self.candidates(mn, args))
        if not found:
            raise AsmError(f"no encoding for {mn} {','.join(args)}")
        op, kinds = found[0]
        end = pc + size(op)
        out = []
        for k, a in zip(kinds, args):
            if k in LITERALS:
                continue
            if k == "imm16" or k == "a16":
                v = self.value(a.lstrip("#"), pc)
                if not 0 <= v <= 0xFFFF:
                    raise AsmError(f"{a}: out of 16-bit range")
                out += [v >> 8, v & 0xFF]
            elif k == "imm":
                v = self.value(a[1:], pc)
                if not -128 <= v <= 0xFF:
                    raise AsmError(f"{a}: out of byte range")
                out.append(v & 0xFF)
            elif k == "dir":
                v = self.value(a, pc)
                if not 0 <= v <= 0xFF:
                    raise AsmError(f"{a}: not a direct address")
                out.append(v)
            elif k in ("bit", "nbit"):
                out.append(self.bit(a.lstrip("/"), pc))
            elif k == "rel":
                d = self.value(a, pc) - end
                if self.final and not -128 <= d <= 127:
                    raise AsmError(f"{a}: relative jump of {d} out of range")
                out.append(d & 0xFF)
        if op == 0x85:
            out = out[::-1]
        return bytes([op] + out)

    def run(self, src):
        image, pc = {}, self.org
        for lineno, line in enumerate(src.splitlines(), 1):
            try:
                pc = self.line(line, pc, image)
            except AsmError as e:
                raise AsmError(f"line {lineno}: {e}  [{line.strip()}]") from None
        if not image:
            return b""
        lo, hi = min(image), max(image) + 1
        if lo < self.org:
            raise AsmError("code below ORG")
        return bytes(image.get(a, 0xFF) for a in range(self.org, hi))

    def line(self, line, pc, image):
        line = _strip_comment(line).strip()
        m = re.match(r"([A-Za-z_]\w*):\s*(.*)", line)
        if m:
            if not self.final and m.group(1) in self.sym and self.sym[m.group(1)] != pc:
                raise AsmError(f"{m.group(1)} defined twice")
            self.sym[m.group(1)] = pc
            line = m.group(2)
        if not line:
            return pc
        m = re.match(r"([A-Za-z_]\w*)\s+EQU\s+(.+)", line, re.I)
        if m:
            self.sym[m.group(1)] = self.value(m.group(2), pc)
            return pc
        mn, _, rest = line.partition(" ")
        mn, args = mn.upper(), _split(rest)
        if mn == "ORG":
            return self.value(args[0], pc)
        if mn in ("DB", "DW"):
            data = []
            for a in args:
                if a[0] in "'\"":
                    data += list(a[1:-1].encode("latin1"))
                else:
                    v = self.value(a, pc)
                    data += [v >> 8 & 0xFF, v & 0xFF] if mn == "DW" else [v & 0xFF]
            code = bytes(data)
        else:
            code = self.encode(mn, args, pc)
        for i, b in enumerate(code):
            if self.final and pc + i in image:
                raise AsmError(f"overlaps 0x{pc + i:04X}")
            image[pc + i] = b
        return pc + len(code)


def assemble(src, org=0, symbols=None):
    """Bytes of `src` from `org` (gaps 0xFF) and the symbol table: (code, symbols)."""
    a = _Asm(org, symbols)
    a.run(src)                                     # pass 1: label addresses
    a.final = True
    return a.run(src), a.sym
