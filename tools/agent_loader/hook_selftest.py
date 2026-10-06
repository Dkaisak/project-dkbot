"""Valida la mecanica del hook inline de lua_gettop sin tocar el cliente.

Crea una funcion falsa con el mismo prologo (13 bytes), aplica el parche
(jmp stub + nops) y el stub (movabs g_L; mov [rax],rcx; prologo; jmp vuelta),
la llama y comprueba: (a) el valor de retorno sigue siendo correcto, (b) g_L se
captura. Asi se verifica la aritmetica de los rel32/movabs antes de inyectar.
"""
import ctypes
import struct
from ctypes import wintypes

k = ctypes.WinDLL("kernel32", use_last_error=True)
k.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
k.VirtualAlloc.restype = ctypes.c_void_p

MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
PAGE_EXECUTE_READWRITE = 0x40

GETTOP = bytes.fromhex("488b4128482b412048c1f803c3")  # mov rax,[rcx+0x28]; sub rax,[rcx+0x20]; sar rax,3; ret

# "lua_State" falso: top=+0x28, base=+0x20. top-base = 3 elementos -> retorno 3
state = (ctypes.c_ubyte * 0x40)()
top_ptr = ctypes.addressof(state) + 0x28
base_ptr = ctypes.addressof(state) + 0x20
ctypes.c_uint64.from_address(top_ptr).value = 0x1000 + 3 * 8  # top
ctypes.c_uint64.from_address(base_ptr).value = 0x1000        # base

g_L = ctypes.c_uint64(0)

fn = k.VirtualAlloc(None, 64, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE)
ctypes.memmove(fn, GETTOP, len(GETTOP))

# alloc_near: reservar cerca de fn (dentro de +-2GB) intentando direcciones
def alloc_near(near, size=64):
    gran = 0x10000
    for delta in range(gran, 0x70000000, gran):
        for addr in (near + delta, near - delta):
            if addr <= 0:
                continue
            p = k.VirtualAlloc(ctypes.c_void_p(addr), size, MEM_COMMIT | MEM_RESERVE,
                               PAGE_EXECUTE_READWRITE)
            if p:
                return p
    return None

stub = alloc_near(fn)
assert stub, "no se pudo reservar stub cercano"

# stub: movabs rax,&g_L ; mov [rax],rcx ; prologo(8) ; jmp fn+8
sb = bytearray()
sb += bytes([0x48, 0xB8]) + struct.pack("<Q", ctypes.addressof(g_L))  # movabs rax,&g_L
sb += bytes([0x48, 0x89, 0x08])                                              # mov [rax],rcx
sb += GETTOP[:8]                                                            # prologo original
rel_back = (fn + 8) - (stub + len(sb) + 5)
sb += bytes([0xE9]) + struct.pack("<i", rel_back)
ctypes.memmove(stub, bytes(sb), len(sb))

# parche: jmp stub + 3 nops en los primeros 8 bytes
rel = stub - (fn + 5)
patch = bytes([0xE9]) + struct.pack("<i", rel) + b"\x90\x90\x90"
ctypes.memmove(fn, patch, 8)

proto = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)
f = proto(fn)
res = f(ctypes.addressof(state))
print("retorno lua_gettop falso =", res, "(esperado 3)")
print("g_L capturado           =", hex(g_L.value), "(esperado", hex(ctypes.addressof(state)) + ")")
ok = (res == 3 and g_L.value == ctypes.addressof(state))
print("RESULTADO:", "OK" if ok else "FALLO")
raise SystemExit(0 if ok else 1)
