#!/usr/bin/env python3
"""
repo_server.py — Minimal HTTP server for the AI-Agent OS local APK repository.

Features vs plain `python3 -m http.server`:
  - Correct Content-Type for .apk (application/x-tar) and APKINDEX.tar.gz
  - Access log to stdout (captured by OpenRC)
  - Graceful SIGTERM shutdown
  - Optional --bind and --port flags
  - CORS headers so the repo is reachable from the QEMU host via port-forward
"""

import argparse
import http.server
import os
import signal
import sys

MIME = {
    ".apk":    "application/x-tar",
    ".gz":     "application/gzip",
    ".tar.gz": "application/gzip",
    ".pub":    "text/plain",
    ".rsa":    "application/octet-stream",
}


class APKRepoHandler(http.server.SimpleHTTPRequestHandler):
    """Serve an APK repo directory with correct MIME types and access logging."""

    def __init__(self, *args, directory=None, **kwargs):
        super().__init__(*args, directory=directory, **kwargs)

    def end_headers(self):
        # Allow host-side curl/apk through QEMU port-forward
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def guess_type(self, path):
        for ext, mime in MIME.items():
            if path.endswith(ext):
                return mime
        return super().guess_type(path)

    def log_message(self, fmt, *args):
        # ISO-8601 timestamp + request info — captured by OpenRC to /var/log/apk-repo-server.log
        sys.stdout.write(
            f"[apk-repo] {self.log_date_time_string()} {fmt % args}\n"
        )
        sys.stdout.flush()


def main():
    parser = argparse.ArgumentParser(description="AI-Agent OS APK HTTP repo server")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--bind", default="0.0.0.0",
                        help="Bind address (0.0.0.0 = all interfaces)")
    parser.add_argument("--dir", default="/srv/apk-repo",
                        help="Repository root directory")
    args = parser.parse_args()

    if not os.path.isdir(args.dir):
        print(f"[apk-repo] ERROR: Repository directory not found: {args.dir}",
              file=sys.stderr)
        sys.exit(1)

    handler = lambda *a, **kw: APKRepoHandler(*a, directory=args.dir, **kw)
    server = http.server.ThreadingHTTPServer((args.bind, args.port), handler)

    def _shutdown(sig, frame):
        print("[apk-repo] Shutting down...", flush=True)
        server.shutdown()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    print(f"[apk-repo] Serving {args.dir} on http://{args.bind}:{args.port}",
          flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
