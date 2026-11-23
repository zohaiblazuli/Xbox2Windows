; coverage: source cpu_aot_lahf
; Source-owned differential fixture for LAHF after a register ADD, SUB or CMP in the shared lowering.
; Every export takes RCX = x, RDX = y and R8 = the value RAX holds before the flag writer, so the result
; shows that LAHF replaces AH only. The return value is the whole RAX.
.code

lahf_add64 PROC FRAME
    .endprolog
    mov rax, r8
    add rcx, rdx
    lahf
    ret
lahf_add64 ENDP

lahf_add32 PROC FRAME
    .endprolog
    mov rax, r8
    add ecx, edx
    lahf
    ret
lahf_add32 ENDP

lahf_add16 PROC FRAME
    .endprolog
    mov rax, r8
    add cx, dx
    lahf
    ret
lahf_add16 ENDP

lahf_add8 PROC FRAME
    .endprolog
    mov rax, r8
    add cl, dl
    lahf
    ret
lahf_add8 ENDP

lahf_sub64 PROC FRAME
    .endprolog
    mov rax, r8
    sub rcx, rdx
    lahf
    ret
lahf_sub64 ENDP

lahf_sub32 PROC FRAME
    .endprolog
    mov rax, r8
    sub ecx, edx
    lahf
    ret
lahf_sub32 ENDP

lahf_sub16 PROC FRAME
    .endprolog
    mov rax, r8
    sub cx, dx
    lahf
    ret
lahf_sub16 ENDP

lahf_sub8 PROC FRAME
    .endprolog
    mov rax, r8
    sub cl, dl
    lahf
    ret
lahf_sub8 ENDP

lahf_cmp64 PROC FRAME
    .endprolog
    mov rax, r8
    cmp rcx, rdx
    lahf
    ret
lahf_cmp64 ENDP

lahf_cmp32 PROC FRAME
    .endprolog
    mov rax, r8
    cmp ecx, edx
    lahf
    ret
lahf_cmp32 ENDP

lahf_cmp16 PROC FRAME
    .endprolog
    mov rax, r8
    cmp cx, dx
    lahf
    ret
lahf_cmp16 ENDP

lahf_cmp8 PROC FRAME
    .endprolog
    mov rax, r8
    cmp cl, dl
    lahf
    ret
lahf_cmp8 ENDP

lahf_add_imm8 PROC FRAME
    .endprolog
    mov rax, r8
    add rcx, 0Fh
    lahf
    ret
lahf_add_imm8 ENDP

lahf_sub_imm32 PROC FRAME
    .endprolog
    mov rax, r8
    sub ecx, 12345678h
    lahf
    ret
lahf_sub_imm32 ENDP

lahf_cmp_imm8 PROC FRAME
    .endprolog
    mov rax, r8
    cmp dl, 80h
    lahf
    ret
lahf_cmp_imm8 ENDP

lahf_add_rax PROC FRAME
    .endprolog
    mov rax, r8
    add rax, rcx
    lahf
    ret
lahf_add_rax ENDP

lahf_cmp_ah PROC FRAME
    .endprolog
    mov rax, r8
    cmp ah, cl
    lahf
    ret
lahf_cmp_ah ENDP

lahf_sub_al PROC FRAME
    .endprolog
    mov rax, r8
    sub al, dl
    lahf
    ret
lahf_sub_al ENDP

lahf_moves_between PROC FRAME
    .endprolog
    mov rax, r8
    sub rcx, rdx
    mov r9, rcx
    lea r10, [rcx + 1]
    lahf
    ret
lahf_moves_between ENDP

lahf_consumer PROC FRAME
    .endprolog
    mov rax, r8
    cmp rcx, rdx
    lahf
    setl al
    jbe lahf_consumer_done
    bswap rax
lahf_consumer_done:
    ret
lahf_consumer ENDP

END
