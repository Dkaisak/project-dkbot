# Build de pxg_agent_loader.dll (MinGW-w64 / Zig). Para MSVC usa build_windows.bat.
#
#   make            -> usa x86_64-w64-mingw32-gcc (o gcc)
#   make zig        -> usa `python -m ziglang cc`
#   make clean

CC ?= x86_64-w64-mingw32-gcc
CFLAGS = -shared -O2 -Wall -Wno-unused-parameter
OUT = pxg_agent_loader.dll

all: $(OUT)

$(OUT): pxg_agent_loader.c signatures.h
	$(CC) $(CFLAGS) -o $(OUT) pxg_agent_loader.c -lkernel32

zig: pxg_agent_loader.c signatures.h
	python -m ziglang cc -target x86_64-windows-gnu $(CFLAGS) -o $(OUT) pxg_agent_loader.c

clean:
	-del /q $(OUT) 2>nul || rm -f $(OUT)

.PHONY: all zig clean
