const std = @import("std");
const A = std.mem.Allocator;
pub const Kind = enum { TYPE, METHOD, FUNCTION, FIELD, MODULE, FILE, VARIABLE };
pub const Relation = enum { CALLS, REFERENCES, IMPLEMENTS, OVERRIDES, CONTAINS };
pub const Quality = enum { LEXICAL, PROBABLE, DERIVED, EXACT, VERIFIED };
pub const Polarity = enum { POSITIVE, NEGATIVE };
pub const Entity = struct { id: u64, kind: Kind, name_sid: u32, container: ?u64 = null };
pub const Edge = struct { subject: u64, relation: Relation, object: u64 };
// Alphabetical field order is canonical JSON order. Never add measurement fields here.
pub const Evidence = struct { freshness_epoch: u64, lineage: u32, object: u64, polarity: Polarity, proposition: u64, quality: Quality, relation: Relation, subject: u64 };
pub const Fixture = struct { schema: []const u8, snapshot: []const u8, epoch: u64 = 0, strings: [][]const u8, entities: []Entity, relations: []Edge, evidence: []Evidence, complete: bool = false };
const Op = enum { RESOLVE, RELATED, TRAVERSE, FILTER };
pub const Query = struct {
    schema: ?[]const u8 = null,
    query_id: ?[]const u8 = null,
    op: Op,
    entity_id: ?u64 = null,
    name: ?[]const u8 = null,
    input: ?*Query = null,
    relation: ?Relation = null,
    relations: []const Relation = &.{},
    direction: enum { OUT, IN } = .OUT,
    max_depth: u64 = 1,
    max_paths: u64 = 100000,
    kind: ?Kind = null,
    evidence: struct { min_quality: ?Quality = null, freshness_epoch: ?u64 = null } = .{},
};
fn validateQuery(q: Query) anyerror!void {
    if (q.schema) |schema| {
        if (!std.mem.eql(u8, schema, "csl.eval.query/v0.1")) return error.InvalidQuery;
    }
    if (q.max_depth == 0 or q.max_depth > 64 or q.max_paths == 0) return error.InvalidQuery;
    if (q.entity_id) |id| {
        if (id == 0) return error.InvalidQuery;
    }
    if (q.op == .RESOLVE and ((q.name == null) == (q.entity_id == null))) return error.InvalidQuery;
    if ((q.op == .RELATED or q.op == .TRAVERSE) and q.input == null) return error.InvalidQuery;
    if (q.input) |input| try validateQuery(input.*);
}
const Set = std.AutoHashMap(u64, void);
const Index = std.AutoHashMap(u64, std.ArrayList(Edge));
pub fn edgeLess(_: void, a: Edge, b: Edge) bool {
    if (a.subject != b.subject) return a.subject < b.subject;
    const order = std.mem.order(u8, @tagName(a.relation), @tagName(b.relation));
    if (order != .eq) return order == .lt;
    return a.object < b.object;
}
pub fn evLess(_: void, a: Evidence, b: Evidence) bool {
    inline for (.{ "subject", "relation", "object", "proposition", "lineage", "polarity", "quality", "freshness_epoch" }) |field| {
        const x = @field(a, field);
        const y = @field(b, field);
        const order = switch (@typeInfo(@TypeOf(x))) {
            .@"enum" => std.mem.order(u8, @tagName(x), @tagName(y)),
            else => std.math.order(x, y),
        };
        if (order != .eq) return order == .lt;
    }
    return false;
}
fn sorted(a: A, set: Set) ![]u64 {
    const ids = try a.alloc(u64, set.count());
    var it = set.keyIterator();
    var i: usize = 0;
    while (it.next()) |v| {
        ids[i] = v.*;
        i += 1;
    }
    std.mem.sort(u64, ids, {}, std.sort.asc(u64));
    return ids;
}
/// Lazy name index: interned name string -> entity ids in ascending order.
/// Keys borrow the fixture's string slices (no per-string copy); two string
/// table entries with equal content share one key (that is the interning).
/// Built on first use, so W1/W2 runs that never resolve by name pay nothing.
pub const NameCache = struct {
    a: A,
    built: bool = false,
    intern_ns: u64 = 0,
    map: std.StringHashMap(std.ArrayList(u64)),
    pub fn create(a: A) !*NameCache {
        const cache = try a.create(NameCache);
        cache.* = .{ .a = a, .map = std.StringHashMap(std.ArrayList(u64)).init(a) };
        return cache;
    }
    pub fn ensure(self: *NameCache, fx: Fixture) !void {
        if (self.built) return;
        var timer = try std.time.Timer.start();
        for (fx.entities) |e| {
            const entry = try self.map.getOrPut(fx.strings[e.name_sid]);
            if (!entry.found_existing) entry.value_ptr.* = std.ArrayList(u64).init(self.a);
            try entry.value_ptr.append(e.id);
        }
        var it = self.map.valueIterator();
        while (it.next()) |list| {
            if (list.items.len > 1) std.mem.sort(u64, list.items, {}, std.sort.asc(u64));
        }
        self.intern_ns = timer.read();
        self.built = true;
    }
    /// Ascending entity ids whose name equals `name`; empty when unknown.
    pub fn resolve(self: *const NameCache, name: []const u8) []const u64 {
        return if (self.map.get(name)) |list| list.items else &.{};
    }
    /// Distinct strings referenced by at least one entity.
    pub fn unique(self: *const NameCache) u64 {
        return self.map.count();
    }
};
pub const Store = struct {
    a: A,
    fx: Fixture,
    entities: std.AutoHashMap(u64, Entity),
    out: Index,
    inc: Index,
    name_cache: ?*NameCache = null,
    pub fn checkSchema(fx: Fixture) !void {
        if (!std.mem.eql(u8, fx.schema, "csl.eval.fixture/v0.1")) return error.InvalidFixture;
    }
    /// Entity map build + container-reference validation: the "entities"
    /// index sub-phase (STEP 3 investigation).
    pub fn buildEntities(a: A, fx: Fixture) !std.AutoHashMap(u64, Entity) {
        var entities = std.AutoHashMap(u64, Entity).init(a);
        for (fx.entities) |e| {
            if (e.id == 0 or e.name_sid >= fx.strings.len or entities.contains(e.id)) return error.InvalidEntity;
            try entities.put(e.id, e);
        }
        for (fx.entities) |e| {
            if (e.container) |id| {
                if (!entities.contains(id)) return error.InvalidReference;
            }
        }
        return entities;
    }
    pub const Adjacency = struct { out: Index, inc: Index };
    /// Outgoing/incoming adjacency build + edge/evidence-reference
    /// validation: the "adjacency" index sub-phase.
    pub fn buildAdjacency(a: A, fx: Fixture, entities: *const std.AutoHashMap(u64, Entity)) !Adjacency {
        var out = Index.init(a);
        var inc = Index.init(a);
        for (fx.relations) |e| {
            if (!entities.contains(e.subject) or !entities.contains(e.object)) return error.InvalidReference;
            for ([_]*Index{ &out, &inc }, [_]u64{ e.subject, e.object }) |idx, id| {
                const entry = try idx.getOrPut(id);
                if (!entry.found_existing) entry.value_ptr.* = std.ArrayList(Edge).init(a);
                try entry.value_ptr.append(e);
            }
        }
        for (fx.evidence) |e| {
            if (e.proposition == 0 or !entities.contains(e.subject) or !entities.contains(e.object)) return error.InvalidEvidence;
        }
        return .{ .out = out, .inc = inc };
    }
    /// Final adjacency-list sort: the "sort" index sub-phase.
    pub fn sortAdjacency(out: *Index, inc: *Index) void {
        for ([_]*Index{ out, inc }) |idx| {
            var it = idx.valueIterator();
            while (it.next()) |rows| std.mem.sort(Edge, rows.items, {}, edgeLess);
        }
    }
    pub fn init(a: A, fx: Fixture) !Store {
        try checkSchema(fx);
        const entities = try buildEntities(a, fx);
        var adjacency = try buildAdjacency(a, fx, &entities);
        sortAdjacency(&adjacency.out, &adjacency.inc);
        return Store{ .a = a, .fx = fx, .entities = entities, .out = adjacency.out, .inc = adjacency.inc, .name_cache = try NameCache.create(a) };
    }
    fn eval(self: *const Store, q: Query, truncated: *bool) anyerror!Set {
        var result = Set.init(self.a);
        if (q.max_depth == 0 or q.max_depth > 64 or q.max_paths == 0) return error.InvalidQuery;
        if (q.op == .RESOLVE) {
            if ((q.name == null) == (q.entity_id == null)) return error.InvalidQuery;
            if (q.name) |name| {
                if (self.name_cache) |cache| {
                    try cache.ensure(self.fx);
                    for (cache.resolve(name)) |id| try result.put(id, {});
                } else {
                    for (self.fx.entities) |e| {
                        if (std.mem.eql(u8, name, self.fx.strings[e.name_sid])) try result.put(e.id, {});
                    }
                }
            } else if (q.entity_id) |id| {
                if (id == 0) return error.InvalidQuery;
                if (self.entities.contains(id)) try result.put(id, {});
            }
            return result;
        }
        var base = Set.init(self.a);
        if (q.input) |input| {
            base = try self.eval(input.*, truncated);
        } else if (q.op == .FILTER) {
            for (self.fx.entities) |e| try base.put(e.id, {});
        } else return error.InvalidQuery;
        const ids = try sorted(self.a, base);
        if (q.op == .FILTER) {
            for (ids) |id| {
                if (q.kind == null or self.entities.get(id).?.kind == q.kind.?) try result.put(id, {});
            }
            return result;
        }
        const idx = if (q.direction == .IN) &self.inc else &self.out;
        const Item = struct { id: u64, depth: u64 };
        var queue = std.ArrayList(Item).init(self.a);
        for (ids) |id| try queue.append(.{ .id = id, .depth = 0 });
        var pos: usize = 0;
        var steps: u64 = 0;
        while (pos < queue.items.len) : (pos += 1) {
            const item = queue.items[pos];
            if (q.op == .TRAVERSE and item.depth >= q.max_depth) continue;
            if (idx.get(item.id)) |rows| {
                for (rows.items) |e| {
                    if (q.op == .RELATED and q.relation != null and q.relation.? != e.relation) continue;
                    if (q.op == .TRAVERSE and q.relations.len > 0 and std.mem.indexOfScalar(Relation, q.relations, e.relation) == null) continue;
                    const y = if (q.direction == .IN) e.subject else e.object;
                    steps += 1;
                    if (q.op == .RELATED) {
                        try result.put(y, {});
                    } else if (!base.contains(y)) {
                        try base.put(y, {});
                        try result.put(y, {});
                        try queue.append(.{ .id = y, .depth = item.depth + 1 });
                    }
                    if (q.op == .TRAVERSE and steps >= q.max_paths) {
                        truncated.* = true;
                        return result;
                    }
                }
            }
        }
        return result;
    }
    pub const Selection = struct { ids: []u64, props: []Evidence, truncated: bool };
    /// Validate once and run the traversal/set logic: the "query" phase,
    /// excluding result construction (owned by `materialize`).
    pub const QueryOutcome = struct { selected: Set, truncated: bool };
    pub fn query(self: *const Store, q: Query) !QueryOutcome {
        if (q.schema == null or !std.mem.eql(u8, q.schema.?, "csl.eval.query/v0.1") or q.query_id == null) return error.InvalidQuery;
        try validateQuery(q);
        var truncated = false;
        const selected = try self.eval(q, &truncated);
        return .{ .selected = selected, .truncated = truncated };
    }
    /// Build the normalized in-memory result (sorted ids, filtered/sorted
    /// evidence) from a raw query outcome: the "materialize" phase.
    pub fn materialize(self: *const Store, q: Query, outcome: QueryOutcome) !Selection {
        const ids = try sorted(self.a, outcome.selected);
        var props = std.ArrayList(Evidence).init(self.a);
        for (self.fx.evidence) |e| {
            if (!outcome.selected.contains(e.subject) and !outcome.selected.contains(e.object)) continue;
            if (q.evidence.min_quality) |qual| {
                if (@intFromEnum(e.quality) < @intFromEnum(qual)) continue;
            }
            if (q.evidence.freshness_epoch) |epoch| {
                if (e.freshness_epoch != epoch) continue;
            }
            try props.append(e);
        }
        std.mem.sort(Evidence, props.items, {}, evLess);
        return .{ .ids = ids, .props = props.items, .truncated = outcome.truncated };
    }
    pub fn select(self: *const Store, q: Query) !Selection {
        return self.materialize(q, try self.query(q));
    }
    pub fn encode(self: *const Store, q: Query, selection: Selection) ![]const u8 {
        const ids = selection.ids;
        const completeness = .{ .entity_set = @as([]const u8, if (selection.truncated) "TRUNCATED" else if (self.fx.complete) "COMPLETE" else "OBSERVED") };
        const knowledge = .{ .model = "open-world" };
        const payload = .{ .completeness = completeness, .entities = ids, .knowledge = knowledge, .propositions = selection.props };
        const bytes = try std.json.stringifyAlloc(self.a, payload, .{ .emit_null_optional_fields = false });
        var hash: [32]u8 = undefined;
        std.crypto.hash.sha2.Sha256.hash(bytes, &hash, .{});
        const digest = try std.fmt.allocPrint(self.a, "sha256:{s}", .{std.fmt.fmtSliceHexLower(&hash)});
        return std.json.stringifyAlloc(self.a, .{ .completeness = completeness, .digest = digest, .entities = ids, .knowledge = knowledge, .propositions = selection.props, .query_id = q.query_id.?, .schema = "csl.eval.result/v0.1", .snapshot = self.fx.snapshot }, .{});
    }
    pub fn execute(self: *const Store, q: Query) ![]const u8 {
        return self.encode(q, try self.select(q));
    }
};
pub fn run(a: A, fixture: []const u8, query: []const u8) ![]const u8 {
    const fx = try std.json.parseFromSlice(Fixture, a, fixture, .{ .allocate = .alloc_always });
    const q = try std.json.parseFromSlice(Query, a, query, .{ .allocate = .alloc_always });
    const store = try Store.init(a, fx.value);
    return store.execute(q.value);
}
test "ordering includes polarity" {
    const a = Evidence{ .freshness_epoch = 1, .lineage = 1, .object = 2, .polarity = .POSITIVE, .proposition = 1, .quality = .EXACT, .relation = .CALLS, .subject = 1 };
    var b = a;
    b.polarity = .NEGATIVE;
    try std.testing.expect(evLess({}, b, a));
}
test "name index interns equal strings and returns ascending ids" {
    var arena = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena.deinit();
    const a = arena.allocator();
    var strings = [_][]const u8{ "x", "y", "x" };
    var entities = [_]Entity{
        .{ .id = 9, .kind = .TYPE, .name_sid = 0 },
        .{ .id = 3, .kind = .TYPE, .name_sid = 2 },
        .{ .id = 5, .kind = .TYPE, .name_sid = 1 },
    };
    var fx = Fixture{ .schema = "csl.eval.fixture/v0.1", .snapshot = "S", .strings = &strings, .entities = &entities, .relations = &.{}, .evidence = &.{} };
    fx.epoch = 0;
    const cache = try NameCache.create(a);
    try cache.ensure(fx);
    try std.testing.expectEqual(@as(u64, 2), cache.unique());
    try std.testing.expectEqualSlices(u64, &.{ 3, 9 }, cache.resolve("x"));
    try std.testing.expectEqualSlices(u64, &.{5}, cache.resolve("y"));
    try std.testing.expectEqual(@as(usize, 0), cache.resolve("absent").len);
}
