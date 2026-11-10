; alcortool boot-firmware capture stub (payloads.page0_loader_image; docs/processes.md).
; Assembled over an unused slot handler of the stick's own page0 at STUB, which the upload
; then runs as a RAM loader. FA 0B SUB: load overlay CDB[4] through page0's select routine
; SELECT (the ROM's overlay loader, which descrambles it into the window at 0xD000); the
; host reads it back with FA 10. The ROM-facing slots point at the returns below.

CDB     EQU 0x000F              ; CDB[0] in the CBW, XDATA

        ORG STUB
capture: MOV DPTR,#CDB+2
        MOVX A,@DPTR
        CJNE A,#SUB,result
        MOV DPTR,#CDB+4
        MOVX A,@DPTR
        MOV R7,A
        LCALL SELECT
result: MOV R7,#0x00            ; slot-4 result: the ROM sends a passing CSW
done:   RET
ret_80: MOV R7,#0x80
        RET
ret_01: MOV R7,#0x01
        RET
