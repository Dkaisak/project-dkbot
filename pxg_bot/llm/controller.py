"""Controlador del LLM: hilo que refresca la decision a su propia cadencia.

No bloquea el bucle del bot. El bot entrega el ultimo `GameState` con
`observe()`; el hilo construye el contexto, invoca el grafo y guarda la ultima
decision valida con su timestamp. `latest()` devuelve la decision solo si sigue
fresca (`stale_secs`).
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from .config import normalize, resolve_api_key
from .perception import render, summarize


class LlmController:
    def __init__(self, cfg: dict, governed: list):
        self.cfg = normalize(cfg)
        self.governed = list(governed)
        self.available = False
        self.error = ""
        self._lock = threading.Lock()
        self._state = None
        self._extra: dict = {}
        self._decision: Optional[dict] = None
        self._decision_at = 0.0
        self._last_error = ""
        self._calls = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._graph = None
        self._model = None

    # --- ciclo de vida ---
    def start(self) -> None:
        if not self.cfg.get("enabled", False):
            return
        if not self.governed:
            self.error = "sin behaviors gobernados"
            return
        try:
            from .graph import build_graph, build_model

            api_key = resolve_api_key(self.cfg)
            if not api_key:
                self.error = (
                    f"sin API key (config 'api_key' o env "
                    f"'{self.cfg.get('api_key_env')}')"
                )
                return
            self._model = build_model(self.cfg, api_key)
            method = str(self.cfg.get("structured_method", "function_calling"))
            self._graph = build_graph(self._model, self.governed, method=method)
            self.available = True
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            return
        self._thread = threading.Thread(target=self._run, name="llm-decide", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # --- entrada/salida ---
    def observe(self, state, extra: Optional[dict] = None) -> None:
        with self._lock:
            self._state = state
            if extra:
                self._extra = extra

    def latest(self) -> Optional[dict]:
        if not self.available:
            return None
        with self._lock:
            decision = self._decision
            at = self._decision_at
        if not decision:
            return None
        age = time.time() - at
        if age > float(self.cfg.get("stale_secs", 12.0)):
            return None
        return {**decision, "age": round(age, 2)}

    def status(self) -> dict:
        with self._lock:
            decision = dict(self._decision) if self._decision else None
            at = self._decision_at
            err = self._last_error
        return {
            "enabled": bool(self.cfg.get("enabled", False)),
            "available": self.available,
            "model": self.cfg.get("model"),
            "governed": list(self.governed),
            "calls": self._calls,
            "decision": decision,
            "decision_age": round(time.time() - at, 2) if at else None,
            "error": err or self.error,
        }

    # --- bucle ---
    def _run(self) -> None:
        interval = max(0.5, float(self.cfg.get("decide_interval", 2.5)))
        while not self._stop.is_set():
            try:
                self._decide_once()
            except Exception as exc:  # nunca debe tumbar el hilo
                with self._lock:
                    self._last_error = f"{type(exc).__name__}: {exc}"
            if self._stop.wait(interval):
                break

    def _decide_once(self) -> None:
        with self._lock:
            state = self._state
            extra = dict(self._extra)
        if state is None:
            return
        ctx = summarize(state, self.cfg, extra)
        text = render(ctx)
        out = self._graph.invoke({"context": text})
        decision = out.get("decision")
        error = out.get("error", "")
        with self._lock:
            self._calls += 1
            if decision:
                self._decision = decision
                self._decision_at = time.time()
                self._last_error = ""
            elif error:
                self._last_error = str(error)
