"""python -m smartoffice.visualization --port 8000"""

import argparse


def main():
    parser = argparse.ArgumentParser(description="SmartOffice browser workbench (local, single user)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    import uvicorn
    uvicorn.run("smartoffice.visualization.server:app", host=args.host, port=args.port, workers=1)


if __name__ == "__main__":
    main()
