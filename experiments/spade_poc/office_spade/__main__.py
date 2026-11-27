import argparse
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Office-kun isolated SPADE prototype")
    subparsers = parser.add_subparsers(dest="role", required=True)
    for role in ("xmpp", "room", "coordinator", "building"):
        worker = subparsers.add_parser(role)
        worker.add_argument("--run-dir", type=Path, required=True)
        worker.add_argument("--port", type=int, default=15222)
        if role == "xmpp":
            worker.add_argument("--s2s-port", type=int, default=15269)
        if role == "room":
            worker.add_argument("--room", choices=["K1315", "K1316"], required=True)
    for role in ("demo", "acceptance"):
        runner = subparsers.add_parser(role)
        runner.add_argument("--output", type=Path, default=Path("artifacts"))
        if role == "acceptance":
            runner.add_argument("--repeat", type=int, default=3)
    for role in ("building-demo", "building-acceptance", "llm-demo"):
        runner = subparsers.add_parser(role)
        runner.add_argument("--output", type=Path, default=Path("artifacts"))
        runner.add_argument("--repeat", type=int, default=1)
    subparsers.add_parser("model-readiness")
    args = parser.parse_args()
    if args.role == "model-readiness":
        import json

        from .model import public_readiness

        print(json.dumps(public_readiness(), ensure_ascii=False))
        return 0
    if args.role in {"building-demo", "building-acceptance", "llm-demo"}:
        from .building_acceptance import run

        return run(args)
    if args.role in {"demo", "acceptance"}:
        from .acceptance import run

        return run(args)
    from .worker import run_worker

    run_worker(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
