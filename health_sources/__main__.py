"""命令行：python -m health_sources run|export|validate"""
from __future__ import annotations

import argparse
import json
import logging
import resource
import sys
from pathlib import Path

from . import runner
from .schema import validate
from .store import Store


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="health_sources")
    p.add_argument("--config", default=str(Path(__file__).resolve().parent.parent / "sources.yaml"))
    p.add_argument("--data-dir", default="./data")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="抓取全部（或指定）来源并导出快照")
    r.add_argument("--only", nargs="*", help="只跑这些来源 id")
    sub.add_parser("export", help="只从 SQLite 重新导出快照")
    sub.add_parser("validate", help="校验 latest.jsonl 每条记录")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.cmd == "run":
        results = runner.run(args.config, args.data_dir, args.only)
        rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        failed = [x["source"] for x in results if not x["ok"]]
        logging.info("done: %d sources, %d failed %s, peak RSS %.1f MB",
                     len(results), len(failed), failed or "", rss_mb)
        return 1 if results and len(failed) == len(results) else 0
    if args.cmd == "export":
        store = Store(Path(args.data_dir) / "health_sources.sqlite")
        try:
            st = runner.export(store, Path(args.data_dir))
        finally:
            store.close()
        print(json.dumps(st["sources"], ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "validate":
        bad = 0
        path = Path(args.data_dir) / "latest.jsonl"
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            errs = validate(json.loads(line))
            if errs:
                bad += 1
                print(f"line {i}: {errs}")
        print(f"checked {path}: {bad} invalid")
        return 1 if bad else 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
