"""Reproducible synthetic benchmark; no market services or LLM calls.

Run from the repository root: env/bin/python scripts/benchmark_structure_runtime.py
The frozen test reference represents the implementation before incremental updates.
"""
from pathlib import Path
import random
import runpy
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    cold = """
import time
start = time.perf_counter()
from core.tools.implementations.technical.chanlun_segments import chanlun_segments
from core.tools.implementations.technical.structure_bars import StructureBar
print(time.perf_counter() - start)
"""
    times = [float(subprocess.check_output([sys.executable, "-c", cold], cwd=ROOT, text=True))
             for _ in range(3)]
    print(f"Cold pure algorithm import: {statistics.median(times) * 1000:.2f} ms (median of 3)")
    from core.tools.implementations.technical.chanlun_segments import chanlun_segments
    from core.tools.implementations.technical.structure_bars import StructureBar
    reference = runpy.run_path(str(ROOT / "tests/fixtures/chanlun_segments_reference.py"))
    print("case,endpoints,reference_ms,incremental_ms,speedup")
    for kind in ("trend", "random"):
        for count in (100, 500, 1000, 2000):
            rng = random.Random(20261020)
            values = [100]
            for i in range(1, count):
                values.append(100 + i // 2 + (10 if i % 2 else 0) if kind == "trend"
                              else values[-1] + (1 if i % 2 else -1) * rng.randint(1, 20))
            strokes = [("top" if i % 2 else "bottom", i, StructureBar(str(i), p, p, p, p))
                       for i, p in enumerate(values)]
            elapsed, results = [], []
            for function in (reference["chanlun_segments"], chanlun_segments):
                samples = []
                for _ in range(3):
                    start = time.perf_counter()
                    result = function(strokes, last_endpoint_confirmed=True)
                    samples.append((time.perf_counter() - start) * 1000)
                elapsed.append(statistics.median(samples))
                results.append(result)
            assert results[0] == results[1]
            print(f"{kind},{count},{elapsed[0]:.2f},{elapsed[1]:.2f},{elapsed[0]/elapsed[1]:.1f}x")


if __name__ == "__main__":
    main()
