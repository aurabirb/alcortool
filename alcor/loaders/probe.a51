; alcortool probe loader for the F700 (alcor/asm51.py; `alcortool dev loader`, `dev nand`).
; A RAM loader of our own: no AlcorMP code. FA 0B subs:
;   E0 ident   in, 512 B: the signature, then internal RAM 0x00-0xFF from offset 0x100
;   E1 xread   in, CDB[9] x 512 B of XDATA from address CDB[4:6]
;   E2 xwrite  none: XDATA[CDB[4:6]] = CDB[7]
;   E3 nandcmd in, 512 B: on CE CDB[3], command CDB[4], one address cycle CDB[5], then
;              CDB[6:8] data bytes (at most 512); CDB[10] bit 0: wait for ready by status (70h)
;              first, then 00h (for a command with a busy time, e.g. ECh)
;   E4 read    in, CDB[9] x 512 B: page read [CDB[10] prefix command, e.g. DAh = Micron
;              SLC mode], 00h, the five address cycles CDB[4:9], 30h, ready by status (70h),
;              00h, then the raw page clocked out byte by byte: data, ECC parity and spare
;              as stored (the controller's ECC transfer engine is not used: docs/nand.md)
;   E5 erase   in, 512 B: block erase 60h, the three row cycles CDB[4:7], D0h, status at [0]
; The mask-ROM contract (docs/structures.md, RAM loader): 31 LJMP slots at 0xC000; the ROM calls slot 0
; once after the upload, slot 4 for every FA 0B (sub at XDATA 0x11), slot 10 from its idle
; loop; slots 2, 3, 7, 19 and 22 return constants in R7; a slot-4 result other than 0x81
; makes the ROM send a passing CSW. Data-in: 512 B in the endpoint buffer at XDATA 0x0000,
; the buffer pointer 0x4400/0x4401 cleared, then ROM USB_SEND sends it and waits.
; The NAND side follows the ROM's own READ ID (CODE:47A4) and page read cycles (CODE:2727).

CDB       EQU 0x000F            ; CDB[0] in the CBW, XDATA
EP_BUF    EQU 0x0000            ; endpoint buffer (the CBW arrives here too)
HIGH_EP   EQU EP_BUF >> 8
EP_PTR    EQU 0x4400
USB_SEND  EQU 0x66C2            ; ROM: send the 512-B endpoint buffer, wait until sent
PASS      EQU 0x00              ; slot-4 result: the ROM sends a passing CSW

NAND_CMD  EQU 0x8000            ; command cycle
NAND_ADDR EQU 0x8001            ; address cycle
NAND_DATA EQU 0x8002            ; the byte clocked in by NAND_CYCLE
NAND_CYCLE EQU 0x8004           ; 1: clock one data byte
NAND_CE   EQU 0x8007            ; chip-enable mask
CE_MASKS  EQU 0x5E1B            ; ROM: CE n -> NAND_CE mask
DELAY     EQU 0x6886            ; ROM: short spin (uses R7)
WAIT_READY EQU 0x6581           ; ROM: until 0x8005 bit 7 (ready)

        ORG 0xC000
        LJMP done               ; 0  init, after the upload
        LJMP done               ; 1
        LJMP ret80              ; 2
        LJMP ret01              ; 3
        LJMP fa0b               ; 4  FA 0B
        LJMP done               ; 5
        LJMP done               ; 6
        LJMP ret01              ; 7
        LJMP done               ; 8
        LJMP done               ; 9
        LJMP done               ; 10 idle loop
        LJMP done               ; 11
        LJMP done               ; 12
        LJMP done               ; 13
        LJMP done               ; 14
        LJMP done               ; 15
        LJMP done               ; 16
        LJMP done               ; 17
        LJMP done               ; 18
        LJMP ret00              ; 19
        LJMP done               ; 20
        LJMP done               ; 21
        LJMP ret01              ; 22
        LJMP done               ; 23
        LJMP done               ; 24
        LJMP done               ; 25
        LJMP done               ; 26
        LJMP done               ; 27
        LJMP done               ; 28
        LJMP done               ; 29
        LJMP done               ; 30

ret80:  MOV R7,#0x80
        RET
ret01:  MOV R7,#0x01
        RET
ret00:  MOV R7,#0x00
done:   RET

; -- slot 4: dispatch on the sub ---------------------------------------------------------
fa0b:   MOV DPTR,#CDB+2
        MOVX A,@DPTR
        CJNE A,#0xE0,not_e0
        LJMP ident
not_e0: CJNE A,#0xE1,not_e1
        LJMP xread
not_e1: CJNE A,#0xE2,not_e2
        LJMP xwrite
not_e2: CJNE A,#0xE3,not_e3
        LJMP nandcmd
not_e3: CJNE A,#0xE4,not_e4
        LJMP rawread
not_e4: CJNE A,#0xE5,not_e5
        LJMP erase
not_e5: MOV R7,#PASS
        RET

; -- helpers --------------------------------------------------------------------------------
; R2:R3 = CDB[4:6], the address operand
addr:   MOV DPTR,#CDB+4
        MOVX A,@DPTR
        MOV R2,A
        INC DPTR
        MOVX A,@DPTR
        MOV R3,A
        RET

; A = the CDB byte at XDATA address A
cdbbyte: MOV DPL,A
        MOV DPH,#0
        MOVX A,@DPTR
        RET

; send the endpoint buffer as one 512-B sector of the data-in phase
send:   MOV DPTR,#EP_PTR
        CLR A
        MOVX @DPTR,A
        INC DPTR
        MOVX @DPTR,A
        LCALL USB_SEND
        RET

; copy 512 B from XDATA R2:R3 to the endpoint buffer and send it; R2:R3 advance by 512.
; Uses R1, R4, R5, R7.
sendsec: MOV R4,#HIGH_EP
        MOV R5,#0
        MOV R7,#0               ; 2 x 256 bytes
        MOV R1,#2
ss_byt: MOV DPH,R2
        MOV DPL,R3
        MOVX A,@DPTR
        INC DPTR
        MOV R2,DPH
        MOV R3,DPL
        MOV DPH,R4
        MOV DPL,R5
        MOVX @DPTR,A
        INC DPTR
        MOV R4,DPH
        MOV R5,DPL
        DJNZ R7,ss_byt
        DJNZ R1,ss_byt
        LJMP send

; zero 512 B of XDATA from page A (uses R0)
clrpage: MOV DPH,A
        MOV DPL,#0
        CLR A
        MOV R0,#0
cp_lp:  MOVX @DPTR,A
        INC DPTR
        MOVX @DPTR,A
        INC DPTR
        DJNZ R0,cp_lp
        RET

; select the CE in CDB[3] and wait until the NAND is ready
ce_sel: MOV DPTR,#CDB+3
        MOVX A,@DPTR
        ANL A,#0x07
        MOV DPTR,#CE_MASKS
        MOVC A,@A+DPTR
        MOV DPTR,#NAND_CE
        MOVX @DPTR,A
        LCALL DELAY
        LJMP WAIT_READY

ncmd:   MOV DPTR,#NAND_CMD      ; command cycle A
        MOVX @DPTR,A
        RET

nadr:   MOV DPTR,#NAND_ADDR     ; address cycle A
        MOVX @DPTR,A
        RET

cdbadr: LCALL cdbbyte           ; address cycle from the CDB byte at XDATA A
        SJMP nadr

nbyte:  MOV DPTR,#NAND_CYCLE    ; A = the next data byte
        MOV A,#1
        MOVX @DPTR,A
        MOV DPTR,#NAND_DATA
        MOVX A,@DPTR
        RET

; read status (70h) until ready (bit 6); A = the status
nstat:  MOV A,#0x70
        LCALL ncmd
ns_lp:  LCALL nbyte
        JNB ACC.6,ns_lp
        RET

; -- E0 ident ---------------------------------------------------------------------------
ident:  MOV A,#HIGH_EP          ; 0x000-0x0FF: the signature, zero padded
        LCALL clrpage
id_sig: MOV A,R0
        MOV DPTR,#signature
        MOVC A,@A+DPTR
        JZ id_ram
        MOV DPH,#HIGH_EP
        MOV DPL,R0
        MOVX @DPTR,A
        INC R0
        SJMP id_sig
id_ram: MOV DPTR,#EP_BUF+0x100  ; 0x100-0x1FF: internal RAM 0x00-0xFF
        MOV R0,#0
id_cpy: MOV A,@R0
        MOVX @DPTR,A
        INC DPTR
        INC R0
        CJNE R0,#0,id_cpy
        LCALL send
        MOV R7,#PASS
        RET

; -- E1 xread: CDB[9] sectors from XDATA R2:R3 -------------------------------------------
xread:  LCALL addr
        MOV DPTR,#CDB+9
        MOVX A,@DPTR
        MOV R6,A                ; sectors left
        JZ xr_end
xr_sec: LCALL sendsec
        DJNZ R6,xr_sec
xr_end: MOV R7,#PASS
        RET

; -- E2 xwrite: XDATA[R2:R3] = CDB[7] ------------------------------------------------------
xwrite: MOV DPTR,#CDB+7
        MOVX A,@DPTR
        MOV R7,A
        LCALL addr
        MOV DPH,R2
        MOV DPL,R3
        MOV A,R7
        MOVX @DPTR,A
        MOV R7,#PASS
        RET

; -- E3 nandcmd ---------------------------------------------------------------------------
nandcmd: LCALL ce_sel
        MOV A,#CDB+6            ; R4:R5 = bytes to read, at most 512
        LCALL cdbbyte
        MOV R4,A
        MOV A,#CDB+7
        LCALL cdbbyte
        MOV R5,A
        MOV A,#CDB+10
        LCALL cdbbyte
        MOV R6,A                ; flags
        MOV A,#CDB+4
        LCALL cdbbyte
        LCALL ncmd
        MOV A,#CDB+5
        LCALL cdbadr
        NOP                     ; the ROM's READ ID waits 12 cycles here
        NOP
        NOP
        NOP
        NOP
        NOP
        NOP
        NOP
        NOP
        NOP
        NOP
        NOP
        MOV A,R6
        JNB ACC.0,nc_go
        LCALL nstat             ; busy command: wait, then back to data output
        CLR A
        LCALL ncmd
nc_go:  MOV A,R4                ; cap at 512
        CLR C
        SUBB A,#2
        JC nc_cap
        MOV R4,#2
        MOV R5,#0
nc_cap: MOV A,#HIGH_EP
        LCALL clrpage
        MOV R2,#HIGH_EP         ; R2:R3 = destination
        MOV R3,#0
nc_lp:  MOV A,R4
        ORL A,R5
        JZ nc_end
        LCALL nbyte
        MOV DPH,R2
        MOV DPL,R3
        MOVX @DPTR,A
        INC DPTR
        MOV R2,DPH
        MOV R3,DPL
        MOV A,R5
        JNZ nc_dec
        DEC R4
nc_dec: DEC R5
        SJMP nc_lp
nc_end: LCALL send
        MOV R7,#PASS
        RET

; [prefix], 00h, the five address cycles CDB[4:9], 30h; R6 = CDB[9]
pagecmd: LCALL ce_sel
        MOV A,#CDB+9
        LCALL cdbbyte
        MOV R6,A
        MOV A,#CDB+10           ; the prefix command, if any
        LCALL cdbbyte
        JZ pg_cmd
        LCALL ncmd
pg_cmd: CLR A
        LCALL ncmd
        MOV A,#CDB+4
        LCALL cdbadr
        MOV A,#CDB+5
        LCALL cdbadr
        MOV A,#CDB+6
        LCALL cdbadr
        MOV A,#CDB+7
        LCALL cdbadr
        MOV A,#CDB+8
        LCALL cdbadr
        MOV A,#0x30
        LJMP ncmd

; -- E4 read: CDB[9] sectors of the raw page, byte by byte -------------------------------
rawread: LCALL pagecmd
        LCALL nstat             ; ready, then back to data output
        CLR A
        LCALL ncmd
        MOV A,R6
        JZ rw_end
rw_sec: MOV DPTR,#EP_BUF        ; 512 bytes into the endpoint buffer
        MOV R2,#HIGH_EP
        MOV R3,#0
        MOV R4,#2
        MOV R5,#0
rw_byt: LCALL nbyte
        MOV DPH,R2
        MOV DPL,R3
        MOVX @DPTR,A
        INC DPTR
        MOV R2,DPH
        MOV R3,DPL
        DJNZ R5,rw_byt
        DJNZ R4,rw_byt
        LCALL send
        DJNZ R6,rw_sec
rw_end: MOV R7,#PASS
        RET

; -- E5 erase -----------------------------------------------------------------------------
erase:  LCALL ce_sel
        MOV A,#0x60             ; 60h, three row cycles, D0h
        LCALL ncmd
        MOV A,#CDB+4
        LCALL cdbadr
        MOV A,#CDB+5
        LCALL cdbadr
        MOV A,#CDB+6
        LCALL cdbadr
        MOV A,#0xD0
        LCALL ncmd
        LCALL nstat
        MOV R6,A
        MOV A,#HIGH_EP
        LCALL clrpage
        MOV DPTR,#EP_BUF
        MOV A,R6
        MOVX @DPTR,A
        LCALL send
        MOV R7,#PASS
        RET

signature:
        DB "alcortool probe 5", 0
