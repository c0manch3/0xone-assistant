"""Phase 6a — file-attachment ingestion package.

Public surface:

* :func:`assistant.files.extract.extract_docx`
* :func:`assistant.files.extract.extract_xlsx`
* :func:`assistant.files.extract.extract_txt`
* :func:`assistant.files.extract.extract_md`
* :func:`assistant.files.extract.extract_pdf` (fallback only)
* :data:`assistant.files.extract.EXTRACTORS` (suffix → callable dispatch)
* :class:`assistant.files.extract.ExtractionError`
"""
