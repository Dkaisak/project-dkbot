/*
 * pxg_agent_loader.dll — inyecta el agente Lua (tools/pxg_agent.lua) dentro de
 * pxgme.exe (cliente PokeXGames de Windows) sin usar la ventana.
 *
 * Equivalente Windows del inyector gdb de Linux (tools/install_agent.py):
 *   1. localiza lua_gettop / luaL_loadbuffer / lua_pcall en el modulo principal
 *      (por firma; ver signatures.h),
 *   2. instala un hook inline en lua_gettop para capturar el lua_State* (L),
 *   3. fija PXG_DIR (prependiendo el fuente) y ejecuta el agente con
 *      luaL_loadbuffer + lua_pcall.
 *
 * Se carga con tools/inject_windows.py (CreateRemoteThread + LoadLibraryW).
 * Config: pxg_agent_dll.json junto a la DLL:
 *   {"agent":"<abs pxg_agent.lua>","dir":"<abs mydata>","log":"<abs log>",
 *    "suspend":false}
 *
 * Build (ver build_windows.bat / Makefile):
 *   x86_64-w64-mingw32-gcc -shared -O2 -o pxg_agent_loader.dll pxg_agent_loader.c
 *
 * Compilador objetivo: x86_64. El cliente es x86_64 (mingw-14-posix).
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <tlhelp32.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include <string.h>
#include <stdint.h>

#include "signatures.h"

typedef void lua_State;

typedef int (*lua_gettop_fn)(lua_State *L);
typedef int (*luaL_loadbuffer_fn)(lua_State *L, const char *buff, size_t sz, const char *name);
typedef int (*lua_pcall_fn)(lua_State *L, int nargs, int nresults, int errfunc);

/* lua_State capturado por el hook de lua_gettop. */
static volatile void *g_L = NULL;
static volatile LONG g_ran = 0;

static char g_log[MAX_PATH * 2] = {0};
static char g_dir[MAX_PATH * 2] = {0};
static char g_agent[MAX_PATH * 2] = {0};
static int g_suspend = 0;

/* ---------------------------------------------------------------- log ---- */

static void logmsg(const char *fmt, ...) {
    if (!g_log[0]) return;
    FILE *f = fopen(g_log, "a");
    if (!f) return;
    SYSTEMTIME st;
    GetLocalTime(&st);
    fprintf(f, "%04d-%02d-%02d %02d:%02d:%02d [loader] ",
            st.wYear, st.wMonth, st.wDay, st.wHour, st.wMinute, st.wSecond);
    va_list ap;
    va_start(ap, fmt);
    vfprintf(f, fmt, ap);
    va_end(ap);
    fputc('\n', f);
    fclose(f);
}

/* ------------------------------------------------------------- config ---- */

/* Extrae un valor string de un JSON minimo: "key" : "value". */
static int json_str(const char *json, const char *key, char *out, size_t outsz) {
    char pat[128];
    snprintf(pat, sizeof(pat), "\"%s\"", key);
    const char *p = strstr(json, pat);
    if (!p) return 0;
    p = strchr(p + strlen(pat), ':');
    if (!p) return 0;
    p++;
    while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n') p++;
    if (*p != '"') return 0;
    p++;
    size_t i = 0;
    while (*p && *p != '"' && i + 1 < outsz) {
        if (*p == '\\' && p[1]) p++;
        out[i++] = *p++;
    }
    out[i] = 0;
    return 1;
}

static int json_bool(const char *json, const char *key, int dflt) {
    char pat[128];
    snprintf(pat, sizeof(pat), "\"%s\"", key);
    const char *p = strstr(json, pat);
    if (!p) return dflt;
    p = strchr(p + strlen(pat), ':');
    if (!p) return dflt;
    p++;
    while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n') p++;
    if (strncmp(p, "true", 4) == 0) return 1;
    if (strncmp(p, "false", 5) == 0) return 0;
    return dflt;
}

static char *read_file(const char *path, size_t *out_len) {
    HANDLE h = CreateFileA(path, GENERIC_READ, FILE_SHARE_READ, NULL,
                           OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (h == INVALID_HANDLE_VALUE) return NULL;
    DWORD sz = GetFileSize(h, NULL);
    if (sz == INVALID_FILE_SIZE) { CloseHandle(h); return NULL; }
    char *buf = (char *)malloc(sz + 1);
    if (!buf) { CloseHandle(h); return NULL; }
    DWORD rd = 0;
    if (!ReadFile(h, buf, sz, &rd, NULL)) { free(buf); CloseHandle(h); return NULL; }
    CloseHandle(h);
    buf[rd] = 0;
    *out_len = rd;
    return buf;
}

/* ---------------------------------------------------- busqueda de firmas -- */

static uint8_t *find_sig(uint8_t *base, size_t size, const uint8_t *sig, size_t n) {
    if (n == 0 || size < n) return NULL;
    uint8_t *end = base + size - n;
    for (uint8_t *p = base; p <= end; p++) {
        if (*p == sig[0] && memcmp(p, sig, n) == 0) return p;
    }
    return NULL;
}

static void get_module_bounds(HMODULE mod, uint8_t **base, size_t *size) {
    uint8_t *b = (uint8_t *)mod;
    IMAGE_DOS_HEADER *dos = (IMAGE_DOS_HEADER *)b;
    IMAGE_NT_HEADERS *nt = (IMAGE_NT_HEADERS *)(b + dos->e_lfanew);
    *base = b;
    *size = nt->OptionalHeader.SizeOfImage;
}

/* Localiza la seccion de codigo (.text) del modulo. */
static void get_text_bounds(HMODULE mod, uint8_t **base, size_t *size) {
    uint8_t *b = (uint8_t *)mod;
    IMAGE_DOS_HEADER *dos = (IMAGE_DOS_HEADER *)b;
    IMAGE_NT_HEADERS *nt = (IMAGE_NT_HEADERS *)(b + dos->e_lfanew);
    IMAGE_SECTION_HEADER *sec = IMAGE_FIRST_SECTION(nt);
    for (int i = 0; i < nt->FileHeader.NumberOfSections; i++, sec++) {
        if (sec->Characteristics & IMAGE_SCN_CNT_CODE) {
            *base = b + sec->VirtualAddress;
            *size = sec->Misc.VirtualSize ? sec->Misc.VirtualSize : sec->SizeOfRawData;
            return;
        }
    }
    get_module_bounds(mod, base, size);
}

/* ---------------------------------------------------------- hook inline -- */

/* Reserva memoria ejecutable DENTRO de +-2GB de `near_addr` (necesario para
 * que el `jmp rel32` del parche y del stub alcancen el stub). */
static uint8_t *alloc_near(uint8_t *near_addr, size_t size) {
    SYSTEM_INFO si;
    GetSystemInfo(&si);
    uintptr_t gran = si.dwAllocationGranularity ? si.dwAllocationGranularity : 0x10000;
    uintptr_t base = (uintptr_t)near_addr;
    uintptr_t limit = 0x70000000ull; /* < 2GB, con margen */
    for (uintptr_t delta = gran; delta < limit; delta += gran) {
        uintptr_t up = (base + delta) & ~(gran - 1);
        uint8_t *p = (uint8_t *)VirtualAlloc((void *)up, size,
                                             MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE);
        if (p) return p;
        if (base > delta) {
            uintptr_t down = (base - delta) & ~(gran - 1);
            p = (uint8_t *)VirtualAlloc((void *)down, size,
                                        MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE);
            if (p) return p;
        }
    }
    return NULL;
}

/* Hook inline de lua_gettop. Guarda rcx (=L) en g_L con direccionamiento
 * ABSOLUTO (movabs) y ejecuta el prologo original en un stub.
 *
 * El stub DEBE estar a <2GB del exe: tanto el `jmp rel32` del parche como el
 * `jmp rel32` de vuelta del stub usan desplazamiento de 32 bits. Por eso se
 * reserva con alloc_near(). g_L se direcciona con movabs (absoluto).
 *
 * gettop[0..7] = mov rax,[rcx+0x28]; sub rax,[rcx+0x20]  (8 bytes)
 * gettop[8..]  = sar rax,3; ret
 * Parche de los 8 primeros bytes: jmp stub + 3 nops.
 */
static int install_gettop_hook(uint8_t *gettop, void **stub_out) {
    uint8_t *stub = alloc_near(gettop, 64);
    if (!stub) return 0;
    size_t o = 0;
    /* movabs rax, &g_L */
    stub[o++] = 0x48;
    stub[o++] = 0xB8;
    uint64_t gaddr = (uint64_t)(uintptr_t)&g_L;
    memcpy(stub + o, &gaddr, 8); o += 8;
    /* mov [rax], rcx */
    stub[o++] = 0x48; stub[o++] = 0x89; stub[o++] = 0x08;
    /* prologo original (8 bytes) */
    memcpy(stub + o, gettop, 8); o += 8;
    /* jmp gettop+8 */
    stub[o++] = 0xE9;
    int32_t rel = (int32_t)((gettop + 8) - (stub + o + 4));
    memcpy(stub + o, &rel, 4); o += 4;

    DWORD old = 0;
    if (!VirtualProtect(gettop, 8, PAGE_EXECUTE_READWRITE, &old)) return 0;
    gettop[0] = 0xE9;
    int32_t r2 = (int32_t)(stub - (gettop + 5));
    memcpy(gettop + 1, &r2, 4);
    gettop[5] = 0x90; gettop[6] = 0x90; gettop[7] = 0x90;
    VirtualProtect(gettop, 8, old, &old);
    FlushInstructionCache(GetCurrentProcess(), gettop, 8);
    *stub_out = stub;
    return 1;
}

/* --------------------------------------------------------- suspender hilos */

static int suspend_others(DWORD *ids, int max, int *count) {
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
    if (snap == INVALID_HANDLE_VALUE) return 0;
    THREADENTRY32 te;
    te.dwSize = sizeof(te);
    DWORD self = GetCurrentThreadId();
    DWORD pid = GetCurrentProcessId();
    *count = 0;
    if (Thread32First(snap, &te)) {
        do {
            if (te.th32OwnerProcessID == pid && te.th32ThreadID != self) {
                HANDLE h = OpenThread(THREAD_SUSPEND_RESUME, FALSE, te.th32ThreadID);
                if (h) {
                    if (SuspendThread(h) != (DWORD)-1 && *count < max)
                        ids[(*count)++] = te.th32ThreadID;
                    CloseHandle(h);
                }
            }
        } while (Thread32Next(snap, &te));
    }
    CloseHandle(snap);
    return 1;
}

static void resume_threads(DWORD *ids, int count) {
    for (int i = 0; i < count; i++) {
        HANDLE h = OpenThread(THREAD_SUSPEND_RESUME, FALSE, ids[i]);
        if (h) { ResumeThread(h); CloseHandle(h); }
    }
}

/* ------------------------------------------------------------- worker ---- */

static void run_agent(void) {
    HMODULE main_mod = GetModuleHandleW(NULL);
    uint8_t *base = NULL;
    size_t size = 0;
    get_module_bounds(main_mod, &base, &size);

    uint8_t *text = NULL;
    size_t text_size = 0;
    get_text_bounds(main_mod, &text, &text_size);

    uint8_t *gettop = find_sig(text, text_size, SIG_LUA_GETTOP, sizeof(SIG_LUA_GETTOP));
    uint8_t *loadbuf = find_sig(text, text_size, SIG_LUA_LOADBUFFER, sizeof(SIG_LUA_LOADBUFFER));
    uint8_t *pcall = find_sig(text, text_size, SIG_LUA_PCALL, sizeof(SIG_LUA_PCALL));

    logmsg("modulo base=%p size=0x%zX .text=%p/0x%zX", (void *)base, size,
           (void *)text, text_size);
    logmsg("gettop=%p loadbuffer=%p pcall=%p", (void *)gettop, (void *)loadbuf, (void *)pcall);

    if (!loadbuf) loadbuf = base + PXG_RVA_LUA_LOADBUFFER;
    if (!pcall) pcall = base + PXG_RVA_LUA_PCALL;

    lua_State *L = NULL;
    if (gettop) {
        void *stub = NULL;
        DWORD ptids[512];
        int pn = 0;
        suspend_others(ptids, 512, &pn);
        int hook_ok = install_gettop_hook(gettop, &stub);
        resume_threads(ptids, pn);
        if (!hook_ok) {
            logmsg("ERROR: no se pudo instalar el hook de lua_gettop");
            return;
        }
        logmsg("hook de lua_gettop instalado (stub=%p) parche=%02X %02X %02X %02X %02X %02X %02X %02X",
               stub, gettop[0], gettop[1], gettop[2], gettop[3],
               gettop[4], gettop[5], gettop[6], gettop[7]);
        /* Esperar a que el cliente llame a lua_gettop y capture L. */
        for (int i = 0; i < 200 && g_L == NULL; i++) Sleep(50);
        L = (lua_State *)g_L;
    } else {
        /* Ya hay un hook de una inyeccion previa. El parche de lua_gettop
         * (E9 rel32) apunta al stub, cuya 1a instruccion es `movabs rax,&g_L`.
         * Leemos L de ahi -> recargar el agente SIN reiniciar el cliente. */
        uint8_t *cand = base + PXG_RVA_LUA_GETTOP;
        if (cand[0] == 0xE9) {
            int32_t rel = 0;
            memcpy(&rel, cand + 1, 4);
            uint8_t *st = cand + 5 + rel;
            if (st[0] == 0x48 && st[1] == 0xB8) {
                uint64_t gaddr = 0;
                memcpy(&gaddr, st + 2, 8);
                L = *(lua_State **)(uintptr_t)gaddr;
                logmsg("hook previo detectado (stub=%p); lua_State=%p", (void *)st, (void *)L);
            }
        }
    }
    if (L == NULL) {
        logmsg("ERROR: no se pudo obtener lua_State (cliente no esperado o hook no reconocido)");
        return;
    }
    logmsg("lua_State: %p", (void *)L);

    size_t src_len = 0;
    char *src = read_file(g_agent, &src_len);
    if (!src) {
        logmsg("ERROR: no se pudo leer el agente '%s'", g_agent);
        return;
    }

    /* Prepend: fija PXG_DIR (evita depender de lua_pushstring/lua_setfield). */
    char *dir_esc = (char *)malloc(strlen(g_dir) + 1);
    size_t di = 0;
    for (const char *p = g_dir; *p; p++)
        dir_esc[di++] = (*p == '\\') ? '/' : *p;
    dir_esc[di] = 0;

    const char *prefix_a = "PXG_DIR = \"";
    const char *prefix_b = "\"\n";
    size_t total = strlen(prefix_a) + di + strlen(prefix_b) + src_len + 1;
    char *full = (char *)malloc(total);
    if (!full) { free(src); free(dir_esc); logmsg("ERROR: sin memoria"); return; }
    size_t off = 0;
    memcpy(full + off, prefix_a, strlen(prefix_a)); off += strlen(prefix_a);
    memcpy(full + off, dir_esc, di); off += di;
    memcpy(full + off, prefix_b, strlen(prefix_b)); off += strlen(prefix_b);
    memcpy(full + off, src, src_len); off += src_len;
    full[off] = 0;
    free(src);
    free(dir_esc);

    DWORD tids[512];
    int nthreads = 0;
    if (g_suspend) {
        suspend_others(tids, 512, &nthreads);
        logmsg("suspendidos %d hilos", nthreads);
    }

    luaL_loadbuffer_fn loadbuffer = (luaL_loadbuffer_fn)loadbuf;
    lua_pcall_fn pcallf = (lua_pcall_fn)pcall;
    int st = loadbuffer(L, full, off, "@pxg_agent.lua");
    int st2 = -999;
    if (st == 0) {
        st2 = pcallf(L, 0, 0, 0);
    }

    if (g_suspend) resume_threads(tids, nthreads);

    logmsg("loadbuffer=%d pcall=%d (%zu bytes)", st, st2, off);
    free(full);
    InterlockedExchange(&g_ran, 1);
}

static DWORD WINAPI worker(LPVOID param) {
    HMODULE self = (HMODULE)param;
    char cfg_path[MAX_PATH * 2] = {0};
    char self_path[MAX_PATH * 2] = {0};
    GetModuleFileNameA(self, self_path, sizeof(self_path) - 1);
    strncpy(cfg_path, self_path, sizeof(cfg_path) - 1);
    char *slash = strrchr(cfg_path, '\\');
    if (slash) { slash[1] = 0; } else { cfg_path[0] = 0; }
    strncat(cfg_path, "pxg_agent_dll.json", sizeof(cfg_path) - strlen(cfg_path) - 1);

    size_t clen = 0;
    char *cfg = read_file(cfg_path, &clen);
    if (!cfg) {
        /* sin config: intentar log junto a la DLL para dejar rastro */
        char dbg[MAX_PATH * 2];
        strncpy(dbg, self_path, sizeof(dbg) - 1);
        dbg[sizeof(dbg) - 1] = 0;
        char *dot = strrchr(dbg, '.');
        if (dot) *dot = 0;
        strncat(dbg, ".log", sizeof(dbg) - strlen(dbg) - 1);
        strncpy(g_log, dbg, sizeof(g_log) - 1);
        logmsg("ERROR: no se encontro config '%s'", cfg_path);
        return 0;
    }

    json_str(cfg, "log", g_log, sizeof(g_log));
    json_str(cfg, "dir", g_dir, sizeof(g_dir));
    json_str(cfg, "agent", g_agent, sizeof(g_agent));
    g_suspend = json_bool(cfg, "suspend", 1);
    if (!g_log[0]) {
        char dbg[MAX_PATH * 2];
        strncpy(dbg, self_path, sizeof(dbg) - 1);
        dbg[sizeof(dbg) - 1] = 0;
        char *dot = strrchr(dbg, '.');
        if (dot) *dot = 0;
        strncat(dbg, ".log", sizeof(dbg) - strlen(dbg) - 1);
        strncpy(g_log, dbg, sizeof(g_log) - 1);
    }
    if (!g_agent[0] || !g_dir[0]) {
        logmsg("ERROR: config incompleta (agent/dir)");
        free(cfg);
        return 0;
    }
    logmsg("cargando agente=%s dir=%s suspend=%d", g_agent, g_dir, g_suspend);
    free(cfg);

#ifdef _MSC_VER
    __try {
        run_agent();
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        logmsg("EXCEPCION 0x%08lX durante la carga del agente", GetExceptionCode());
    }
#else
    run_agent();
#endif
    return 0;
}

BOOL WINAPI DllMain(HINSTANCE hinst, DWORD reason, LPVOID reserved) {
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(hinst);
        HANDLE h = CreateThread(NULL, 0, worker, (LPVOID)hinst, 0, NULL);
        if (h) CloseHandle(h);
    }
    return TRUE;
}
