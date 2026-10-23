#!/bin/sh
# Build the OCR helper next to this script (binary is git-ignored).
set -e
cd "$(dirname "$0")"
swiftc -O -o ocr ocr.swift
echo "built $(pwd)/ocr"
