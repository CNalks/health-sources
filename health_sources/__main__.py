"""命令行：python -m health_sources run|enrich|backfill|export|push|validate"""
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
    r.add_argument("--no-enrich", action="store_true", help="跳过按 DOI 回查 Europe PMC 的补全步骤")
    e = sub.add_parser("enrich", help="只跑补全步骤（按 DOI 回查 Europe PMC），然后重新导出")
    e.add_argument("--only", nargs="*", help="只补这些来源 id 的记录")
    e.add_argument("--window-days", type=int, help="覆盖 enrich.window_days（回补后补全往期用）")
    e.add_argument("--max-per-run", type=int, help="覆盖 enrich.max_per_run")
    e.add_argument("--max-seconds", type=int, help="覆盖 enrich.max_seconds")
    b = sub.add_parser("backfill", help="往期回补（各来源自己的往期入口，分批写库），然后重新导出")
    b.add_argument("--only", nargs="*", help="只回补这些来源 id（默认全部）")
    b.add_argument("--since", required=True, help="起始日期 YYYY-MM-DD（含）")
    b.add_argument("--year", type=int, help="cdc_weekly：年份")
    b.add_argument("--issues", help="cdc_weekly：期号范围，如 28-43")
    sub.add_parser("export", help="只从 SQLite 重新导出快照")
    pu = sub.add_parser("push", help="向下游订阅方增量推送（只读本库；游标存在订阅方）")
    pu.add_argument("--subscriber", required=True, help="订阅方名，对应 <config-dir>/<name>.toml")
    pu.add_argument("--config-dir", default="/etc/health-sources/subscribers")
    pu.add_argument("--reconcile", action="store_true", help="对账：发窗口内 id+content_hash 清单，补推缺失/不同的")
    pu.add_argument("--dry-run", action="store_true", help="不联网，按空游标统计会推多少（不需要密钥）")
    sub.add_parser("validate", help="校验 latest.jsonl 每条记录")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.cmd == "run":
        results = runner.run(args.config, args.data_dir, args.only, do_enrich=not args.no_enrich)
        rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        failed = [x["source"] for x in results if not x["ok"]]
        logging.info("done: %d sources, %d failed %s, peak RSS %.1f MB",
                     len(results), len(failed), failed or "", rss_mb)
        return 1 if results and len(failed) == len(results) else 0
    if args.cmd == "enrich":
        from .util import now_iso
        cfg = runner.load_config(args.config)
        store = Store(Path(args.data_dir) / "health_sources.sqlite")
        try:
            over = {k: v for k, v in (("window_days", args.window_days), ("max_per_run", args.max_per_run),
                                      ("max_seconds", args.max_seconds)) if v}
            stats = runner.run_enrich(cfg, store, now_iso(), only=args.only, overrides=over)
            runner.export(store, Path(args.data_dir), enrich_stats=stats, **runner.export_opts(cfg))
        finally:
            store.close()
        rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        logging.info("enrich done, peak RSS %.1f MB", rss_mb)
        return 0
    if args.cmd == "backfill":
        results = runner.run_backfill(args.config, args.data_dir, args.only or [], args.since,
                                      {"year": args.year, "issues": args.issues})
        rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        print(json.dumps([{k: r.get(k) for k in ("source", "ok", "skipped", "records", "new", "updated", "invalid",
                                                  "requests", "earliest", "latest", "seconds", "error")}
                          for r in results], ensure_ascii=False, indent=2))
        logging.info("backfill done, peak RSS %.1f MB", rss_mb)
        return 0 if all(r["ok"] or r.get("skipped") for r in results) else 1
    if args.cmd == "export":
        cfg = runner.load_config(args.config)
        store = Store(Path(args.data_dir) / "health_sources.sqlite")
        try:
            st = runner.export(store, Path(args.data_dir), **runner.export_opts(cfg))
        finally:
            store.close()
        print(json.dumps(st["sources"], ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "push":
        from . import push as push_mod
        store = Store(Path(args.data_dir) / "health_sources.sqlite", readonly=True)
        try:
            if args.dry_run:
                sub = push_mod.Subscriber(name=args.subscriber, transport="dry-run")
                try:
                    loaded = push_mod.load_subscriber(args.subscriber, args.config_dir)
                    sub.window_days, sub.batch_size = loaded.window_days, loaded.batch_size
                    sub.max_requests_per_run, sub.max_body_bytes = 10_000, loaded.max_body_bytes
                except FileNotFoundError:
                    sub.max_requests_per_run = 10_000
                transport = push_mod.DryRunTransport()
            else:
                sub = push_mod.load_subscriber(args.subscriber, args.config_dir)
                transport = push_mod.make_transport(sub)
            if args.reconcile:
                stats = push_mod.reconcile(store, transport, sub)
            else:
                stats = push_mod.push(store, transport, sub, push_mod.read_export_generated_at(Path(args.data_dir)))
        finally:
            store.close()
        if args.dry_run:
            stats["calls"] = len(transport.calls)
            stats["maxBodyBytes"] = max((c["bytes"] for c in transport.calls), default=0)
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 0 if args.reconcile or stats.get("complete") else 1
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
