#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
exec venv/bin/python -m stl_repair "$@"
