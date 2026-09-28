# E0 evaluation contract

Resolve selects by ID or exact string-table name (all matches, including
ambiguity). Related includes all one-hop endpoints, including self edges.
Traverse is breadth-first over visited entity IDs; seed IDs are excluded from
its result. Adjacency and initial seeds are sorted before visiting, so a capped
query is insertion-order independent. The cap counts matching edges inspected;
reaching it conservatively marks TRUNCATED, including exact-boundary cases.
Filter without input scans the entity store; with input it selects from that set.

Relations describe the observed graph. Evidence predicates select result evidence;
they do not silently erase graph reachability or promote missing evidence to truth.
Normalized propositions retain subject/relation/object, proposition ID, polarity,
quality, freshness epoch, and lineage. Positive and negative support coexist;
conflicts are retained. Quality ordering is VERIFIED > EXACT > DERIVED > PROBABLE
> LEXICAL. Freshness selection is exact epoch equality, independently of quality.

Entity IDs sort ascending. Evidence sorts by
(subject, relation, object, proposition, lineage, polarity, quality, freshness_epoch).
All fields use schema-defined uppercase strings. Duplicate evidence rows remain
visible. Conflicting rows with otherwise identical keys have a deterministic tie
break. An empty OBSERVED/TRUNCATED result never establishes absence. COMPLETE is
only emitted for an explicitly complete fixture whose traversal did not hit a cap.

Canonical bytes preserve the initial repository convention: UTF-8 JSON, sorted
object keys, compact separators, no ASCII escaping. SHA-256 covers exactly
`{entities,propositions,knowledge,completeness}`. The snapshot and query ID are
context alongside this content digest, as in the original golden contract; compare
full normalized results (including snapshot), not just hashes, across candidates.
No timings, allocator metadata, candidate identity, or toolchains enter the digest.
The golden result remains byte-content compatible.

JSON persistence/reload is tested by independent process invocations. This is not
a claim of a native on-disk index format or crash-consistent persistence. E0 has no
mutation API; W1 incremental 1% invalidation remains explicitly unsupported.
