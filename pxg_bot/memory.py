from __future__ import annotations

import ctypes
import glob
import os
import struct
import sys
from ctypes import wintypes
from typing import Iterator, Optional

IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")

if IS_WINDOWS:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
else:
    kernel32 = None

if IS_LINUX:
    _libc = ctypes.CDLL("libc.so.6", use_errno=True)
else:
    _libc = None

PTRACE_ATTACH = 16
PTRACE_DETACH = 17

TH32CS_SNAPPROCESS = 0x00000002
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
MAX_PATH = 260

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_ALL_ACCESS = 0x1F0FFF

MEM_COMMIT = 0x1000
PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100
PAGE_READABLE = 0x02 | 0x04 | 0x20 | 0x40 | 0x80


class ProcessError(RuntimeError):
    pass


class ProcessNotFound(ProcessError):
    pass


class MemoryReadError(ProcessError):
    pass


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_char * MAX_PATH),
    ]


class MODULEENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("th32ModuleID", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("GlblcntUsage", wintypes.DWORD),
        ("ProccntUsage", wintypes.DWORD),
        ("modBaseAddr", ctypes.POINTER(ctypes.c_byte)),
        ("modBaseSize", wintypes.DWORD),
        ("hModule", wintypes.HMODULE),
        ("szModule", ctypes.c_char * 256),
        ("szExePath", ctypes.c_char * MAX_PATH),
    ]


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


def _setup_prototypes() -> None:
    if not IS_WINDOWS:
        return
    k = kernel32
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL
    k.ReadProcessMemory.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
    ]
    k.ReadProcessMemory.restype = wintypes.BOOL
    k.WriteProcessMemory.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
    ]
    k.WriteProcessMemory.restype = wintypes.BOOL
    k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k.Process32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
    k.Process32First.restype = wintypes.BOOL
    k.Process32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
    k.Process32Next.restype = wintypes.BOOL
    k.Module32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32)]
    k.Module32First.restype = wintypes.BOOL
    k.Module32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32)]
    k.Module32Next.restype = wintypes.BOOL
    k.VirtualQueryEx.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p,
        ctypes.POINTER(MEMORY_BASIC_INFORMATION), ctypes.c_size_t,
    ]
    k.VirtualQueryEx.restype = ctypes.c_size_t
    k.IsWow64Process.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    k.IsWow64Process.restype = wintypes.BOOL


if IS_WINDOWS:
    _setup_prototypes()


def _ptrace(request: int, pid: int, address: int = 0, data: int = 0) -> int:
    if not IS_LINUX:
        raise ProcessError("ptrace solo disponible en Linux")
    res = _libc.ptrace(
        ctypes.c_ulong(request), ctypes.c_ulong(pid),
        ctypes.c_void_p(address), ctypes.c_void_p(data),
    )
    if res == -1:
        err = ctypes.get_errno()
        raise ProcessError(f"ptrace falló: {os.strerror(err)}")
    return res


class MemoryAccess:
    pointer_size = 8

    def read_bytes(self, address: int, size: int) -> bytes:
        raise NotImplementedError

    def write_bytes(self, address: int, data: bytes) -> bool:
        raise NotImplementedError

    def try_read_bytes(self, address: int, size: int) -> Optional[bytes]:
        try:
            return self.read_bytes(address, size)
        except MemoryReadError:
            return None

    def read(self, address: int, fmt: str):
        size = struct.calcsize(fmt)
        return struct.unpack(fmt, self.read_bytes(address, size))[0]

    def read_u8(self, address: int) -> int:
        return self.read(address, "<B")

    def read_u16(self, address: int) -> int:
        return self.read(address, "<H")

    def read_i32(self, address: int) -> int:
        return self.read(address, "<i")

    def read_u32(self, address: int) -> int:
        return self.read(address, "<I")

    def read_f32(self, address: int) -> float:
        return self.read(address, "<f")

    def read_f64(self, address: int) -> float:
        return self.read(address, "<d")

    def read_pointer(self, address: int) -> int:
        if self.pointer_size == 4:
            return self.read_u32(address)
        return self.read(address, "<Q")

    def try_read_pointer(self, address: int) -> Optional[int]:
        try:
            return self.read_pointer(address)
        except MemoryReadError:
            return None

    def read_cstring(self, address: int, max_len: int = 64, encoding: str = "latin-1") -> str:
        raw = self.read_bytes(address, max_len)
        end = raw.find(b"\x00")
        if end >= 0:
            raw = raw[:end]
        return raw.decode(encoding, "ignore")

    def read_wstring(self, address: int, max_chars: int = 64) -> str:
        raw = self.read_bytes(address, max_chars * 2)
        end = raw.find(b"\x00\x00")
        if end >= 0:
            end += end % 2
            raw = raw[:end]
        return raw.decode("utf-16-le", "ignore")

    def resolve(self, address: int, offsets: list[int]) -> int:
        ptr = address
        for i, off in enumerate(offsets):
            base = ptr + off
            ptr = self.read_pointer(base) if i < len(offsets) - 1 else base
        return ptr


class WindowsProcess(MemoryAccess):
    def __init__(self, handle: int, pid: int, pointer_size: int = 8):
        self.handle = handle
        self.pid = pid
        self.pointer_size = pointer_size
        self._modules: dict[str, tuple[int, int]] = {}

    @classmethod
    def by_name(cls, name: str) -> "WindowsProcess":
        if not IS_WINDOWS:
            raise ProcessError("La lectura de memoria de Windows requiere Windows")
        pid = find_pid(name)
        if pid is None:
            raise ProcessNotFound(f"Proceso '{name}' no encontrado")
        return cls.open(pid)

    @classmethod
    def by_pid(cls, pid: int) -> "WindowsProcess":
        return cls.open(pid)

    @classmethod
    def open(cls, pid: int) -> "WindowsProcess":
        if not IS_WINDOWS:
            raise ProcessError("La lectura de memoria de Windows requiere Windows")
        handle = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
        if not handle:
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_INFORMATION | PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_VM_OPERATION,
                False, pid,
            )
        if not handle:
            raise ProcessError(f"No se pudo abrir el proceso {pid} (error {ctypes.get_last_error()})")
        proc = cls(handle, pid, cls._detect_pointer_size(handle))
        proc._load_modules()
        return proc

    @staticmethod
    def _detect_pointer_size(handle: int) -> int:
        try:
            wow64 = wintypes.BOOL()
            if kernel32.IsWow64Process(handle, ctypes.byref(wow64)) and wow64.value:
                return 4
        except Exception:
            pass
        return 8

    def close(self) -> None:
        if self.handle:
            kernel32.CloseHandle(self.handle)
            self.handle = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _load_modules(self) -> None:
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, self.pid)
        if snap == INVALID_HANDLE_VALUE:
            return
        entry = MODULEENTRY32()
        entry.dwSize = ctypes.sizeof(MODULEENTRY32)
        try:
            ok = kernel32.Module32First(snap, ctypes.byref(entry))
            while ok:
                name = entry.szModule.decode("latin-1", "ignore").lower()
                base = ctypes.cast(entry.modBaseAddr, ctypes.c_void_p).value or 0
                self._modules[name] = (base, entry.modBaseSize)
                ok = kernel32.Module32Next(snap, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snap)

    def module(self, name: str) -> tuple[int, int]:
        base = self._modules.get(name.lower())
        if base is None:
            raise ProcessError(f"Módulo '{name}' no encontrado en el proceso")
        return base

    def read_bytes(self, address: int, size: int) -> bytes:
        if size <= 0:
            return b""
        buf = ctypes.create_string_buffer(size)
        read = ctypes.c_size_t(0)
        ok = kernel32.ReadProcessMemory(
            self.handle, ctypes.c_void_p(address), buf, size, ctypes.byref(read)
        )
        if not ok:
            raise MemoryReadError(f"Lectura fallida en 0x{address:X}")
        return buf.raw[: read.value]

    def write_bytes(self, address: int, data: bytes) -> bool:
        buf = ctypes.create_string_buffer(data, len(data))
        written = ctypes.c_size_t(0)
        return bool(
            kernel32.WriteProcessMemory(
                self.handle, ctypes.c_void_p(address), buf, len(data), ctypes.byref(written)
            )
        )

    def regions(self, readable_only: bool = True) -> Iterator[tuple[int, int, int]]:
        address = 0
        mbi = MEMORY_BASIC_INFORMATION()
        while address < 0x7FFFFFFFFFFF:
            written = kernel32.VirtualQueryEx(
                self.handle, ctypes.c_void_p(address), ctypes.byref(mbi), ctypes.sizeof(mbi)
            )
            if not written:
                break
            base = mbi.BaseAddress or 0
            size = mbi.RegionSize
            if mbi.State == MEM_COMMIT and not (mbi.Protect & PAGE_GUARD):
                if not readable_only or (mbi.Protect & PAGE_READABLE):
                    yield base, size, mbi.Protect
            if size == 0:
                break
            address = base + size

    def module_containing(self, address: int) -> Optional[int]:
        for base, size in self._modules.values():
            if base <= address < base + size:
                return base
        return None


class LinuxProcess(MemoryAccess):
    def __init__(self, pid: int, pointer_size: int = 8):
        self.pid = pid
        self.pointer_size = pointer_size
        self._fd: Optional[int] = None
        self._attached = False
        self._modules: dict[str, tuple[int, int]] = {}
        self._maps: list[tuple[int, int, str]] = []

    @classmethod
    def by_name(cls, name: str, pointer_size: int = 8, hold: bool = False) -> "LinuxProcess":
        pid = find_pid(name)
        if pid is None:
            raise ProcessNotFound(f"Proceso '{name}' no encontrado")
        return cls.open(pid, pointer_size=pointer_size, hold=hold)

    @classmethod
    def by_pid(cls, pid: int, pointer_size: int = 8, hold: bool = False) -> "LinuxProcess":
        return cls.open(pid, pointer_size=pointer_size, hold=hold)

    @classmethod
    def open(cls, pid: int, attach: bool = True, hold: bool = False, pointer_size: int = 8) -> "LinuxProcess":
        if not IS_LINUX:
            raise ProcessError("LinuxProcess requiere Linux")
        if not os.path.isdir(f"/proc/{pid}"):
            raise ProcessNotFound(f"El proceso {pid} no existe")
        proc = cls(pid, pointer_size)
        if attach and pid != os.getpid():
            proc._attach()
        try:
            proc._fd = os.open(f"/proc/{pid}/mem", os.O_RDWR)
        except PermissionError as exc:
            proc._detach()
            raise ProcessError(
                f"Sin permiso para /proc/{pid}/mem. Prueba con sudo o "
                f"sysctl -w kernel.yama.ptrace_scope=0"
            ) from exc
        if proc._attached and not hold:
            proc._detach()
        proc._load_maps()
        return proc

    def _attach(self) -> None:
        try:
            _ptrace(PTRACE_ATTACH, self.pid)
            os.waitpid(self.pid, 0)
            self._attached = True
        except (ProcessError, ChildProcessError) as exc:
            raise ProcessError(
                f"No se pudo hacer ptrace attach al pid {self.pid}: {exc}. "
                f"Revisa kernel.yama.ptrace_scope o ejecuta con sudo."
            ) from exc

    def _detach(self) -> None:
        if self._attached:
            try:
                _ptrace(PTRACE_DETACH, self.pid)
            except ProcessError:
                pass
            self._attached = False

    def _load_maps(self) -> None:
        self._modules.clear()
        self._maps.clear()
        with open(f"/proc/{self.pid}/maps", "r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) < 5:
                    continue
                addr_range, perms = parts[0], parts[1]
                path = parts[-1] if len(parts) >= 6 else ""
                start_s, end_s = addr_range.split("-")
                start, end = int(start_s, 16), int(end_s, 16)
                self._maps.append((start, end - start, perms))
                if "r" not in perms or not path:
                    continue
                base_name = os.path.basename(path).lower()
                key = path if os.path.isabs(path) and os.path.exists(path) else base_name
                if key not in self._modules:
                    self._modules[key] = (start, end - start)
                if base_name not in self._modules:
                    self._modules[base_name] = (start, end - start)
                if path.startswith("/") and path.lower() not in self._modules:
                    self._modules[path.lower()] = (start, end - start)

    def module(self, name: str) -> tuple[int, int]:
        key = name.lower()
        if key in self._modules:
            return self._modules[key]
        for stored, value in self._modules.items():
            if stored.endswith("/" + key) or os.path.basename(stored) == key:
                return value
        raise ProcessError(f"Módulo '{name}' no encontrado en el proceso")

    def read_bytes(self, address: int, size: int) -> bytes:
        if self._fd is None:
            raise ProcessError("proceso cerrado")
        if size <= 0:
            return b""
        try:
            data = os.pread(self._fd, size, address)
        except OSError as exc:
            raise MemoryReadError(f"Lectura fallida en 0x{address:X}: {exc}") from exc
        if not data:
            raise MemoryReadError(f"Lectura vacía en 0x{address:X}")
        return data

    def write_bytes(self, address: int, data: bytes) -> bool:
        if self._fd is None:
            return False
        try:
            os.pwrite(self._fd, data, address)
            return True
        except OSError:
            return False

    def regions(self, readable_only: bool = True) -> Iterator[tuple[int, int, int]]:
        for start, size, perms in self._maps:
            if readable_only and "r" not in perms:
                continue
            yield start, size, 0

    def module_containing(self, address: int) -> Optional[int]:
        for base, size in self._modules.values():
            if base <= address < base + size:
                return base
        return None

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        if self._attached:
            try:
                _ptrace(PTRACE_DETACH, self.pid)
            except ProcessError:
                pass
            self._attached = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


Process = WindowsProcess


def list_processes(name: Optional[str] = None) -> list[tuple[int, str]]:
    if IS_WINDOWS:
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == INVALID_HANDLE_VALUE:
            return []
        result: list[tuple[int, str]] = []
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        try:
            ok = kernel32.Process32First(snap, ctypes.byref(entry))
            target = name.lower() if name else None
            while ok:
                exe = entry.szExeFile.decode("latin-1", "ignore")
                if target is None or exe.lower() == target:
                    result.append((entry.th32ProcessID, exe))
                ok = kernel32.Process32Next(snap, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snap)
        return result
    if IS_LINUX:
        result = []
        target = name.lower() if name else None
        for proc_dir in glob.glob("/proc/[0-9]*"):
            pid = int(os.path.basename(proc_dir))
            exe = _linux_comm(pid)
            if exe is None:
                continue
            if target is None or exe.lower() == target or target in exe.lower():
                result.append((pid, exe))
        return result
    return []


def _linux_comm(pid: int) -> Optional[str]:
    try:
        with open(f"/proc/{pid}/comm", "r", encoding="utf-8", errors="ignore") as handle:
            return handle.read().strip()
    except OSError:
        return None


def find_pid(name: str) -> Optional[int]:
    direct = list_processes(name)
    if direct:
        return direct[0][0]
    if IS_LINUX:
        target = name.lower()
        for proc_dir in glob.glob("/proc/[0-9]*"):
            pid = int(os.path.basename(proc_dir))
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as handle:
                    cmdline = handle.read().decode("latin-1", "ignore")
            except OSError:
                continue
            if target in cmdline.lower():
                return pid
    return None


def open_process(name: Optional[str] = None, pid: Optional[int] = None,
                 pointer_size: int = 8, hold: bool = False):
    if IS_WINDOWS:
        if pid is not None:
            return WindowsProcess.by_pid(pid)
        if name is None:
            raise ProcessError("se requiere nombre o pid")
        return WindowsProcess.by_name(name)
    if IS_LINUX:
        if pid is not None:
            return LinuxProcess.by_pid(pid, pointer_size=pointer_size, hold=hold)
        if name is None:
            raise ProcessError("se requiere nombre o pid")
        resolved = find_pid(name)
        if resolved is None:
            raise ProcessNotFound(f"Proceso '{name}' no encontrado")
        return LinuxProcess.by_pid(resolved, pointer_size=pointer_size, hold=hold)
    raise ProcessError(f"plataforma no soportada: {sys.platform}")