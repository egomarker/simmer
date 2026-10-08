#!/bin/bash

cd "$(dirname "$0")" || exit 1

./.venv/bin/python -m py_compile simmer/server.py

./.venv/bin/simmer --kill
./.venv/bin/simmer --mode fast --fps 60 --quality 20
