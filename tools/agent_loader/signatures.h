/* signatures.h — direcciones/firmas de la API Lua dentro de pxgme.exe.
 *
 * Cliente: PokeXGames pxgme.exe (x86_64, MinGW gcc-14, LuaJIT 2.1 estatico).
 * Build de referencia: "Built for Windows on Tue Oct  6 07:31:06 2026".
 *
 * Como se derivaron (ver tools/derive_signatures.py y NOTES.md):
 *  - lua_gettop: firma exacta (accessor hoja; ABI no cambia el cuerpo).
 *  - lua_pcall / luaL_loadbuffer / lua_load: localizados por los call sites de
 *    los wrappers C++ del cliente (p. ej. Lua::pcall llama a lua_pcall con
 *    (L, nargs, -1, errfunc)) y verificados por su estructura.
 *
 * El codigo x64 es RIP-relativo => los bytes de una funcion no dependen de la
 * base de carga (ASLR), asi que las firmas valen en runtime. Si el cliente se
 * actualiza, re-derivar con tools/derive_signatures.py.
 *
 * El loader resuelve primero por firma (robusto) y, si falla, por RVA.
 */
#ifndef PXG_SIGNATURES_H
#define PXG_SIGNATURES_H

#include <stdint.h>
#include <stddef.h>

/* RVAs de referencia (por si la firma no se encontrara). */
#define PXG_RVA_LUA_GETTOP      0x0A2AC30u
#define PXG_RVA_LUA_PCALL       0x0A33380u
#define PXG_RVA_LUA_LOADBUFFER  0x0A34A40u
#define PXG_RVA_LUA_LOAD        0x0A34230u

/* --- Firmas (bytes exactos del build de referencia) --- */

/* lua_gettop: mov rax,[rcx+0x28]; sub rax,[rcx+0x20]; sar rax,3; ret */
static const uint8_t SIG_LUA_GETTOP[] = {
    0x48, 0x8B, 0x41, 0x28, 0x48, 0x2B, 0x41, 0x20, 0x48, 0xC1, 0xF8, 0x03, 0xC3
};

/* luaL_loadbuffer: sub rsp,0x48; ...; lea rdx,[rip+reader]; ...; call lua_load */
static const uint8_t SIG_LUA_LOADBUFFER[] = {
    0x48, 0x83, 0xEC, 0x48, 0x48, 0xC7, 0x44, 0x24, 0x20, 0x00, 0x00, 0x00, 0x00,
    0x48, 0x89, 0x54, 0x24, 0x30, 0x48, 0x8D, 0x15, 0x97, 0x09, 0xFA, 0xFF,
    0x4C, 0x89, 0x44, 0x24, 0x38, 0x4C, 0x8D, 0x44, 0x24, 0x30,
    0xE8, 0xC8, 0xF7, 0xFF, 0xFF, 0x48, 0x83, 0xC4, 0x48, 0xC3
};

/* lua_pcall (prefijo distintivo). */
static const uint8_t SIG_LUA_PCALL[] = {
    0x41, 0x56, 0x41, 0x55, 0x41, 0x54, 0x55, 0x57, 0x56, 0x53, 0x48, 0x83, 0xEC, 0x20,
    0x45, 0x31, 0xF6, 0x4C, 0x8B, 0x69, 0x10, 0x48, 0x8B, 0x71, 0x28, 0x41, 0x0F, 0xB6,
    0xAD, 0x91, 0x00, 0x00, 0x00
};

/* Prologo de lua_gettop que hay que reubicar en el stub del hook inline:
 * `mov rax,[rcx+0x28]; sub rax,[rcx+0x20]` (8 bytes). */
#define PXG_GETTOP_PROLOGUE_LEN 8

#endif /* PXG_SIGNATURES_H */
