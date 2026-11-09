"""Discovers strategy plugins.

Strategies are collected from three places:

1. the built-ins in ``alpaca_manager.strategies.builtin``;
2. Python files in the plugins folder (``<data dir>/plugins/*.py``): any
   ``Strategy`` subclass defined in the file is registered;
3. installed packages that declare an ``alpaca_manager.strategies`` entry point
   pointing at a ``Strategy`` subclass (or a module containing them).

A plugin that fails to import is reported in the UI instead of breaking the app.
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
import threading
import traceback
from importlib.metadata import entry_points
from pathlib import Path
from types import ModuleType

from .base import Strategy
from .builtin import BUILTIN

PLUGIN_TEMPLATE = '''"""My strategy plugin - edit me, then press "Reload plugins" in the app."""

from alpaca_manager.strategies import Param, Strategy, indicators


class PriceAboveAverage(Strategy):
    key = "price_above_average"          # unique id
    name = "Price above average"
    description = "Hold each symbol while its close is above its N-bar moving average."
    params = {
        "window": Param(100, min=2, max=500, help="Moving-average window (bars)"),
    }

    def warmup(self):
        return self.p["window"]

    def on_bar(self, ctx):
        weights = {}
        for sym in ctx.symbols:
            close = ctx.history(sym, self.p["window"])["close"]
            avg = indicators.sma(close, self.p["window"]).iloc[-1]
            weights[sym] = 1 / len(ctx.symbols) if close.iloc[-1] > avg else 0.0
        return weights
'''


class Registry:
    def __init__(self, plugin_dir: Path):
        self.plugin_dir = plugin_dir
        self._lock = threading.Lock()
        self.strategies: dict[str, dict] = {}  # key -> {"cls", "source"}
        self.errors: list[dict] = []
        self.reload()

    def _add(self, cls: type[Strategy], source: str) -> None:
        key = cls.strategy_key()
        if key in self.strategies and self.strategies[key]["source"] != source:
            self.errors.append({"source": source,
                                "error": f"Duplicate strategy key '{key}' (already from "
                                         f"{self.strategies[key]['source']}) - skipped"})
            return
        self.strategies[key] = {"cls": cls, "source": source}

    @staticmethod
    def _classes_in(module: ModuleType) -> list[type[Strategy]]:
        return [obj for _, obj in inspect.getmembers(module, inspect.isclass)
                if issubclass(obj, Strategy) and obj is not Strategy
                and obj.__module__ == module.__name__ and not inspect.isabstract(obj)]

    def reload(self) -> None:
        with self._lock:
            self.strategies, self.errors = {}, []
            for cls in BUILTIN:
                self._add(cls, "built-in")
            self._load_folder()
            self._load_entry_points()

    def _load_folder(self) -> None:
        self.plugin_dir.mkdir(parents=True, exist_ok=True)
        example = self.plugin_dir / "example_strategy.py.txt"
        if not example.exists():
            example.write_text(PLUGIN_TEMPLATE, encoding="utf-8")
        for path in sorted(self.plugin_dir.glob("*.py")):
            mod_name = f"alpaca_manager_plugin_{path.stem}"
            try:
                spec = importlib.util.spec_from_file_location(mod_name, path)
                module = importlib.util.module_from_spec(spec)
                sys.modules[mod_name] = module
                spec.loader.exec_module(module)
                found = self._classes_in(module)
                if not found:
                    self.errors.append({"source": path.name, "error": "No Strategy subclasses found"})
                for cls in found:
                    self._add(cls, path.name)
            except Exception:  # plugin code is untrusted to be correct
                self.errors.append({"source": path.name,
                                    "error": traceback.format_exc(limit=3).strip().splitlines()[-1],
                                    "traceback": traceback.format_exc(limit=5)})

    def _load_entry_points(self) -> None:
        try:
            eps = entry_points(group="alpaca_manager.strategies")
        except Exception:  # pragma: no cover - metadata problems
            return
        for ep in eps:
            try:
                obj = ep.load()
                classes = [obj] if inspect.isclass(obj) else self._classes_in(obj)
                for cls in classes:
                    if issubclass(cls, Strategy):
                        self._add(cls, f"package: {ep.value}")
            except Exception:
                self.errors.append({"source": f"package: {ep.value}",
                                    "error": traceback.format_exc(limit=1).strip().splitlines()[-1]})

    def get(self, key: str) -> type[Strategy]:
        try:
            return self.strategies[key]["cls"]
        except KeyError:
            raise ValueError(f"Unknown strategy '{key}' - was its plugin removed?") from None

    def build(self, key: str, params: dict) -> Strategy:
        return self.get(key)(**(params or {}))

    def describe(self) -> dict:
        return {
            "plugin_dir": str(self.plugin_dir),
            "strategies": [dict(v["cls"].describe(), source=v["source"])
                           for v in self.strategies.values()],
            "errors": self.errors,
        }
