#!/usr/bin/env bash
# Installs the official Ollama Linux build into /usr during the image build.
# Ollama has used both .tar.zst and .tgz archives, so try both.
set -euo pipefail

BASE="https://ollama.com/download"

if curl -fsSL "${BASE}/ollama-linux-amd64.tar.zst" -o /tmp/ollama.tar.zst; then
  tar --zstd -xf /tmp/ollama.tar.zst -C /usr
  rm -f /tmp/ollama.tar.zst
else
  curl -fsSL "${BASE}/ollama-linux-amd64.tgz" -o /tmp/ollama.tgz
  tar -xzf /tmp/ollama.tgz -C /usr
  rm -f /tmp/ollama.tgz
fi

/usr/bin/ollama --version || true
