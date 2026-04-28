"""Whisper sidecar package marker.

F17 (fix-pack): the sidecar code lives inside an importable package so
``uvicorn whisper_server.main:app`` works in dev (``cd whisper-server``)
identically to prod (``cd ~/whisper-server`` after setup-mac-sidecar.sh
copies the package into place). Previously the runtime layout differed
from the repo layout — we'd hit ImportErrors only post-deploy.
"""
