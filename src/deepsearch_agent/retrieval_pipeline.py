"""Retrieval shared by quick QA and the evaluation runner (no answer generation)."""

def retrieve(kb, question, top_k=5, *, query_expander=None, reranker=None, history=None, trace=None):
    chunks = []
    queries = [question]
    if query_expander:
        try:
            queries.extend(query_expander.expand(question, history))
        except Exception:
            pass  # fallback: use original query only

    fetch_count = min(top_k * 4, 20) if reranker else top_k
    seen_keys: set[tuple] = set()
    searched_queries = []
    for q in queries:
        searched_queries.append(q)
        for c in kb.search(q, top_k=fetch_count):
            key = (c.source_path, c.page_number, c.chunk_index, c.text)
            if key not in seen_keys:
                seen_keys.add(key)
                chunks.append(c)
        if not reranker and len(chunks) >= top_k:
            break

    if trace is not None:
        trace.update(queries=searched_queries, generated_queries=queries, candidates=list(chunks))
    if reranker and len(chunks) > 1:
        chunks = reranker.rerank(question, chunks, top_k)
    elif not reranker:
        chunks = chunks[:top_k]
    return chunks
