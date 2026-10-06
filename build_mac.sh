#!/bin/bash
# Builds the app. Run from the project folder inside your venv.
set -e
APP="BPMtoOSC RXv2 Mossad Spyware.app"

echo "1/3 Building system audio helper..."
swiftc -O -swift-version 5 sckaudio.swift -o sckaudio

echo "2/3 Building app..."
rm -rf build dist
pyinstaller --noconfirm BPMtoOSC.spec

echo "3/3 Signing..."
codesign --force --deep --sign - "dist/$APP"
echo "Done: dist/$APP"
