"""App de escritorio (ventana nativa) para el bot.

Envuelve la UI web existente (`pxg_bot/web/`) en una ventana nativa con
**pywebview** (WebView2 en Windows), reutilizando el servidor y la API de
`pxg_bot.webui`. Anade bandeja del sistema (pystray), opcion "siempre encima" y
cierre -> minimizar a bandeja.

Si pywebview no esta disponible (o falla WebView2), cae al modo navegador.

Uso:  python main.py app
"""
from __future__ import annotations

import os
import sys
import threading
import time
from http.server import ThreadingHTTPServer

from . import paths
from .config import load_config
from .webui import Dashboard, Handler


def _icon_path(cfg: dict) -> str | None:
    ui = cfg.get("ui", {}) or {}
    icon = ui.get("icon") or os.path.join("tools", "icon.ico")
    if not os.path.isabs(icon):
        icon = os.path.join(paths.APP_DIR, icon)
    return icon if os.path.exists(icon) else None


def _load_pil_icon(icon_path: str | None):
    if not icon_path:
        return None
    try:
        from PIL import Image
        return Image.open(icon_path)
    except Exception:
        return None


def _start_tray(app: Dashboard, window, icon_path: str | None, state: dict):
    """Crea y arranca el icono de bandeja en un hilo daemon. Devuelve el Icon."""
    try:
        import pystray
        from pystray import Menu, MenuItem
    except Exception as exc:
        print(f"aviso: pystray no disponible ({exc}); sin bandeja")
        return None
    image = _load_pil_icon(icon_path)
    if image is None:
        try:
            from PIL import Image
            image = Image.new("RGBA", (64, 64), (200, 30, 30, 255))
        except Exception:
            return None

    def do_show(icon, item):
        try:
            window.show()
        except Exception:
            pass

    def do_hide(icon, item):
        try:
            window.hide()
        except Exception:
            pass

    def toggle_top(icon, item):
        state["on_top"] = not state["on_top"]
        try:
            window.on_top = state["on_top"]
        except Exception:
            pass

    def do_start(icon, item):
        try:
            app.start_bot()
        except Exception:
            pass

    def do_stop(icon, item):
        try:
            app.stop_bot()
        except Exception:
            pass

    def do_quit(icon, item):
        state["quit"] = True
        try:
            icon.stop()
        except Exception:
            pass
        try:
            window.destroy()
        except Exception:
            pass

    menu = Menu(
        MenuItem("Mostrar", do_show, default=True),
        MenuItem("Ocultar", do_hide),
        Menu.SEPARATOR,
        MenuItem("Siempre encima", toggle_top, checked=lambda item: state["on_top"]),
        Menu.SEPARATOR,
        MenuItem("Arrancar bot", do_start),
        MenuItem("Parar bot", do_stop),
        Menu.SEPARATOR,
        MenuItem("Salir", do_quit),
    )
    tray = pystray.Icon("dkbot", image, "dkbot", menu)
    threading.Thread(target=tray.run, daemon=True).start()
    return tray


def _fallback_browser(url: str, httpd: ThreadingHTTPServer) -> int:
    """Sin pywebview: abrir en el navegador y servir en el hilo principal."""
    print(f"pywebview no disponible; abriendo en el navegador: {url}")
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass
    print("Ctrl+C para salir (el bot, si esta corriendo, sigue activo).")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nApp detenida")
    finally:
        httpd.shutdown()
    return 0


def run(cfg_path: str, host: str | None = None, port: int | None = None) -> int:
    paths.ensure_app_files()
    cfg = load_config(cfg_path)
    ui = cfg.get("ui", {}) or {}
    win = ui.get("window", {}) or {}
    tray_cfg = ui.get("tray", {}) or {}

    host = host or ui.get("host", "127.0.0.1")
    port = int(port or ui.get("port", 8765))

    app = Dashboard(cfg_path)
    Handler.app = app
    app.start_telegram_commands()
    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        print(f"error: no se pudo abrir el puerto {port}: {exc}", file=sys.stderr)
        return 1
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://{host}:{port}/"
    print(f"dkbot app en {url}")

    try:
        import webview
    except Exception as exc:
        print(f"aviso: pywebview no instalado ({exc})")
        app.stop_telegram_commands()
        return _fallback_browser(url, httpd)

    icon_path = _icon_path(cfg)
    state = {"on_top": bool(win.get("on_top", False)), "quit": False}
    minimize_on_close = bool(tray_cfg.get("minimize_on_close", True))

    window = webview.create_window(
        win.get("title", "dkbot"),
        url,
        width=int(win.get("width", 1280)),
        height=int(win.get("height", 860)),
        min_size=(900, 600),
        on_top=state["on_top"],
        text_select=True,
    )

    tray = _start_tray(app, window, icon_path, state) if tray_cfg.get("enabled", True) else None

    if tray is not None and minimize_on_close:
        def on_closing():
            if state["quit"]:
                return True
            try:
                window.hide()
            except Exception:
                pass
            return False  # cancela el cierre -> se queda en bandeja
        try:
            window.events.closing += on_closing
        except Exception:
            pass

    try:
        def _on_started():
            try:
                print(f"backend GUI: {webview.guilib}")
            except Exception:
                pass

        webview.start(func=_on_started, icon=icon_path, debug=bool(ui.get("debug", False)))
    except Exception as exc:
        print(f"aviso: no se pudo iniciar la ventana nativa ({exc})")
        app.stop_telegram_commands()
        return _fallback_browser(url, httpd)
    finally:
        try:
            if tray is not None:
                tray.stop()
        except Exception:
            pass
        app.stop_telegram_commands()
        httpd.shutdown()
    return 0


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="pxg-app", description="App de escritorio del bot")
    parser.add_argument("--config", default=paths.default_config_path())
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--host", default=None)
    args = parser.parse_args(argv)
    return run(args.config, host=args.host, port=args.port)


if __name__ == "__main__":
    sys.exit(main())
