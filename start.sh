#!/bin/bash
# Run TV Box in the foreground and open the launcher in Safari.
cd "$(dirname "$0")"
exec python3 server.py --open "$@"
