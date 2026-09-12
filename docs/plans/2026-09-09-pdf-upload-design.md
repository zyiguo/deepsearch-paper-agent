# Pure-text PDF upload loop

Approved scope: upload a text PDF, parse/index, show status, ask questions and inspect cited pages. No OCR, tables, report generation or deletion.

Use a bounded raw PDF upload (10 MiB), avoiding multipart buffering. Only local browser origins; no arbitrary filesystem path input. SHA-256 IDs and fixed storage paths, sanitized display names. At most 100 pages / 100,000 extracted characters. Extract in a child process with a timeout. Render original pages to PNG with PDFium; don't execute or embed uploaded PDF active content.

One background worker, bounded queue. Metadata per document is atomically written; status queued/parsing/embedding/ready/failed. Restart marks unfinished jobs interrupted, without automatic paid retries. Uploading identical bytes is idempotent when the current model's index contains them. No automatic deletion; failed files can be retried explicitly. Original PDFs and extracted text live alongside the index directory under documents/.

Expensive PDF/embedding work occurs outside the knowledge-base lock. Commit merges into a copy of the latest KB under the lock, saves snapshot, then publishes memory. Source IDs use pdf://<sha256>/<encoded filename>. Commit failure preserves existing KB; ready status is checked against actual current chunks. Existing sessions and demo snapshots remain untouched.

UI: file upload with explicit cloud-processing notice, status list and manual retry, knowledge-base reconciliation, page viewer with previous/next/page entry and extracted-text fallback. Citations open the matching document/page. Current retrieval remains global across indexed documents.

Verification: offline integration tests for PDF parsing, ordering, duplicates, failures, bounds, reload/restart, original-page access; frontend API/state tests/build; real-browser smoke test with original synthetic story. No private PDFs or secret contents printed.
