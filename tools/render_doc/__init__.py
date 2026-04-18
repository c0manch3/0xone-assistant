"""tools.render_doc — PDF/DOCX renderer CLI for phase-7 media artefacts.

Phase 7 introduces a thin CLI that renders plain-text bodies to PDF (via
`fpdf2`) or DOCX (via `python-docx`) into the daemon-managed outbox so
the dispatch-reply layer can deliver them through the messenger adapter.

Invocation:
    python tools/render_doc/main.py --body-file PATH --out PATH [--title T] [--font DejaVu]

The package ships an empty ``__init__`` so ``tools.render_doc.main`` is
importable both via ``python -m`` and via direct script launch (the
``main`` module installs the project-root ``sys.path`` pragma required
for the latter).
"""
