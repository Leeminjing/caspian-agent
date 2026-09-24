## Apply evidence

### Context7 gate and dependency contract

- Context7 entry points: `mcp__context7__resolve_library_id` and `mcp__context7__query_docs`.
- Successful library resolutions: `/langchain-ai/langgraph` and `/websites/langchain_oss_python` / `/websites/reference_langchain`.
- Context7 returned the official `BaseStore.put` / `aput` contract: `index=None` uses the Store default fields, `index=False` disables indexing for that write, and `index=[...]` selects JSON field paths. It also returned `BaseStore.abatch`, `PutOp`, same-key batch deduplication with last-write-wins, and the model token-counting APIs.
- Installed versions cross-checked locally: LangGraph `1.2.6`, LangChain `1.3.10`, langchain-core `1.4.7`. The project declares `langgraph>=0.3.0`, `langchain>=0.3.0`, and `langchain-core>=0.3.0`.
- Installed LangGraph `1.2.6` confirms field paths support top-level names such as `retrieval_text`, nested dotted paths, array indices/wildcards, and `"$"`. `get_text_at_path(value, "$")` serializes the complete JSON value; `get_text_at_path(value, "retrieval_text")` returns only that field.
- Installed LangGraph `1.2.6` confirms `aput` delegates to `abatch([PutOp(...)])`; both InMemoryStore and Postgres Store upsert the same `(namespace, key)`. Multiple PutOps for the same key in one batch collapse to the last operation.
- Updating with `index=["retrieval_text"]` recomputes that vector. Updating with `index=False` changes the value without requesting a new embedding and retains the existing vector in the inspected InMemoryStore and Postgres implementations. This is valid only when `retrieval_text` is unchanged.
- `abatch` preserves one result position per submitted operation. The base API does not expose a portable per-operation partial-success report. InMemoryStore embeds before applying values. AsyncPostgresStore groups the SQL in one pipeline/transaction execution context, but adapter/connection failure guarantees are implementation-specific; application code therefore must propagate the batch exception and must not claim success or invent a portable transaction guarantee.
- Selected token-counting contract: inject a `TokenCounter = Callable[[str], int]` and bind it to the configured chat model's `get_num_tokens(text)`. LangChain documents this API and model-specific overrides; the same callable is used by the atomicity gate and hard-max validator. Tests use deterministic fake counters for Chinese, English, and code, while production follows the configured model tokenizer. The policy is ideal `150..400`, hard maximum `600`, and no hard minimum.

### Pre-change baseline

- Command: `python -m pytest backend\\packages\\harness\\tests\\test_knowledge_store_client.py backend\\packages\\harness\\tests\\test_knowledge_rating.py backend\\packages\\harness\\tests\\test_knowledge_query_endpoint.py backend\\packages\\harness\\tests\\test_knowledge_provenance.py backend\\packages\\harness\\tests\\test_knowledge_judge.py backend\\packages\\harness\\tests\\test_knowledge_governance.py backend\\packages\\harness\\tests\\test_benchmark_rag.py -m "not live" -q`
- Result: `72 passed` in `2.73s`.
- Existing environment warnings: unknown pytest option `asyncio_mode`; pytest cache directory could not be created because Windows denied access. Neither warning failed the suite.

### Final verification

- Targeted Evidence Unit, Store, API, judge, governance, and benchmark suite: `93 passed, 2 warnings`.
- Mechanical Evidence Unit benchmark passed with six units and zero embedding-isolation, identity-collision, source-overwrite, overlap, hard-max, or invalid-span violations; expected conflict coverage was two full conflicts and one partial conflict.
- Full non-live suite with a workspace-local base temp directory: `608 passed, 27 failed, 2 skipped, 4 deselected, 25 subtests passed`. All 27 failures are pre-existing environment gaps outside this change: 24 require the absent `aiosqlite` package and 3 require the absent `duckduckgo_search` package. No Evidence Unit or knowledge regression failed.
- `python -m compileall -q caspian` completed successfully.
- Loading `config.yaml` through `get_app_config` confirmed `langgraph_store.fields == ["retrieval_text"]`.
- `git diff --check` completed without whitespace errors; Git only reported repository line-ending conversion warnings.
- `openspec validate add-evidence-unit-knowledge-ingestion --strict` passed.
- Manual design review confirmed the implementation is split into evidence models, identity, retrieval rendering, structure/span chunking, model segmentation, ingestion orchestration, Store projection/persistence, API boundary, and benchmark integrity modules. Public contracts are declared in file-header docstrings; the LangChain tool description remains the only required callable docstring.

### Post-verification repair

- Re-ran the Context7 gate through `mcp__context7__resolve_library_id` and `mcp__context7__query_docs` against LangGraph `/langchain-ai/langgraph/1.0.8`. The documented Store contract confirms that namespace/key is the item identity, value updates do not migrate keys, and `PutOp.index=False` only controls indexing. New Evidence Unit source URLs are therefore immutable under PATCH; callers must ingest a new source-bound unit, while legacy source enrichment remains compatible.
- The conservative atomicity gate now routes paragraphs with multiple sentence boundaries or semicolon-delimited clauses to semantic span segmentation. A regression fixture proves `A 功能已废弃。B 默认值是 20。` no longer bypasses segmentation, while 60-token and 450-token single-cluster fixtures remain on the direct path.
- The Evidence Unit benchmark now declares a validated governance probe, runs the production `govern` function before and after a governance-only level change, proves the changed level changes the winner, and verifies a second unit from the same multi-topic document retains identical content and governance status.
- The same probe mechanically confirms that changing level, level basis, provenance, and source count leaves the recomputed retrieval text unchanged. The report now exposes governance-metadata embedding pollution, governance-level usage, and unrelated-unit mutation counters.
- Post-repair targeted suite: `95 passed, 2 warnings`.
- Post-repair full non-live suite: `610 passed, 27 failed, 2 skipped, 4 deselected, 25 subtests passed`. The same 27 environment-only failures remain: 24 require `aiosqlite`, and 3 require `duckduckgo_search`; there are no new implementation failures.
- Post-repair mechanical benchmark: seven units, two full conflicts, one partial conflict, and zero violations for identity, source overwrite, overlap, hard max, span validity, embedding isolation, governance metadata embedding isolation, governance level usage, or unrelated-unit mutation.
