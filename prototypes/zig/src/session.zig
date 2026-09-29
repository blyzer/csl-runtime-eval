//! S0 v0 session mode for the Zig candidate (oracle/SESSION-SEMANTICS.md, SESSION-BINDING-JSONL.md,
//! adr/0008-mutation-semantics.md). `csl-eval-zig session --repository DIR` speaks the JSONL binding.
//!
//! Design notes
//! * One `Gen` per store generation: its own arena (so replacing a generation frees the old one),
//!   a counting allocator over that arena (heap statistics), the logical rows as a `sem.Fixture`
//!   and the existing `sem.Store` built over it. Queries reuse `Store.execute`, so result bytes are
//!   identical to the one-shot mode. Query scratch memory comes from a per-request arena.
//! * Mutation strategy is `full-rebuild`: the batch is validated and applied to multiset working
//!   maps, then every derived structure (entity map, adjacency, name index) is rebuilt into a new
//!   `Gen` before `mutate` responds.
//! * Snapshot file layout (little endian), named `<repository>/<snapshot_id>.snap`:
//!     magic "CSLZSNAP" | u32 version(1) | u32 len + artifact | u32 len + context.snapshot |
//!     u8 epoch_present | u64 epoch | u8 complete | u64 entities | u64 relations | u64 evidence |
//!     entities: (u64 id, u8 kind, u8 has_container, u64 container, u32 len + name)* |
//!     relations: (u64 subject, u8 relation, u64 object)* |
//!     evidence: (u64 proposition, u64 subject, u8 relation, u64 object, u8 polarity, u8 quality,
//!                u64 freshness_epoch, u32 lineage)* | sha256 of everything before it.
const std = @import("std");
const sem = @import("semantic.zig");
const A = std.mem.Allocator;
const Value = std.json.Value;
const ObjectMap = std.json.ObjectMap;
const Sha256 = std.crypto.hash.sha2.Sha256;

pub const ARTIFACT = "csl-eval-zig/s0-v0";
const BINDING = "csl.eval.session.jsonl/v0";
const SEMANTICS = "csl.eval.session/v0.1";
const MIN_LINE: u64 = 65536;
/// Bytes reserved for the JSON envelope of a chunk line (`{"data":"","frame":"chunk","id":N,"seq":K}\n`).
const FRAME_OVERHEAD: u64 = 128;
const SNAP_MAGIC = "CSLZSNAP";
const SNAP_VERSION: u32 = 1;
const MAX_U64 = std.math.maxInt(u64);

const Err = error{ InvalidRequest, InvalidState, InvalidInput, Unsupported, OutOfMemory };

fn codeOf(e: anyerror) []const u8 {
    return switch (e) {
        error.InvalidRequest => "INVALID_REQUEST",
        error.InvalidState => "INVALID_STATE",
        error.InvalidInput => "INVALID_INPUT",
        error.Unsupported => "UNSUPPORTED",
        error.OutOfMemory => "LIMIT_EXCEEDED",
        else => "INTERNAL",
    };
}

// ---- canonical JSON output -------------------------------------------------------------
/// Append-only canonical JSON sink; with a hasher it streams what it writes into SHA-256.
const Sink = struct {
    buf: std.ArrayList(u8),
    hasher: ?Sha256 = null,
    total: u64 = 0,
    fn init(a: A, hashing: bool) Sink {
        return .{ .buf = std.ArrayList(u8).init(a), .hasher = if (hashing) Sha256.init(.{}) else null };
    }
    fn spill(self: *Sink) void {
        if (self.hasher) |*h| {
            if (self.buf.items.len >= (1 << 20)) {
                h.update(self.buf.items);
                self.total += self.buf.items.len;
                self.buf.clearRetainingCapacity();
            }
        }
    }
    fn raw(self: *Sink, s: []const u8) !void {
        try self.buf.appendSlice(s);
        self.spill();
    }
    fn num(self: *Sink, v: u64) !void {
        var tmp: [24]u8 = undefined;
        try self.raw(std.fmt.bufPrint(&tmp, "{d}", .{v}) catch unreachable);
    }
    /// JSON string escaped like Python json.dumps(ensure_ascii=False).
    fn str(self: *Sink, s: []const u8) !void {
        try self.buf.append('"');
        for (s) |c| switch (c) {
            '"' => try self.buf.appendSlice("\\\""),
            '\\' => try self.buf.appendSlice("\\\\"),
            8 => try self.buf.appendSlice("\\b"),
            12 => try self.buf.appendSlice("\\f"),
            '\n' => try self.buf.appendSlice("\\n"),
            '\r' => try self.buf.appendSlice("\\r"),
            '\t' => try self.buf.appendSlice("\\t"),
            0...7, 11, 14...31 => {
                var tmp: [6]u8 = undefined;
                try self.buf.appendSlice(std.fmt.bufPrint(&tmp, "\\u{x:0>4}", .{c}) catch unreachable);
            },
            else => try self.buf.append(c),
        };
        try self.buf.append('"');
        self.spill();
    }
    fn optNum(self: *Sink, v: ?u64) !void {
        if (v) |x| try self.num(x) else try self.raw("null");
    }
    fn finish(self: *Sink) struct { digest: [32]u8, bytes: u64 } {
        var h = self.hasher.?;
        h.update(self.buf.items);
        self.total += self.buf.items.len;
        var out: [32]u8 = undefined;
        h.final(&out);
        return .{ .digest = out, .bytes = self.total };
    }
};

fn hexLower(a: A, bytes: []const u8) ![]u8 {
    return std.fmt.allocPrint(a, "{s}", .{std.fmt.fmtSliceHexLower(bytes)});
}

// ---- counting allocator per generation --------------------------------------------------
const Counters = struct { allocations: u64 = 0, total_requested: u64 = 0, total_freed: u64 = 0, live: u64 = 0, peak_live: u64 = 0 };

/// Counts bytes requested at the allocator interface into the session-wide counters, and remembers
/// this generation's share so retiring the generation (arena drop) releases exactly that.
const GenAlloc = struct {
    child: A,
    counters: *Counters,
    mine: u64 = 0,
    fn allocator(self: *GenAlloc) A {
        return .{ .ptr = self, .vtable = &.{ .alloc = alloc, .resize = resize, .remap = remap, .free = free } };
    }
    fn grow(self: *GenAlloc, old_len: usize, new_len: usize) void {
        const c = self.counters;
        if (new_len >= old_len) {
            c.total_requested += new_len - old_len;
            c.live += new_len - old_len;
            self.mine += new_len - old_len;
            if (c.live > c.peak_live) c.peak_live = c.live;
        } else {
            c.total_freed += old_len - new_len;
            c.live -= old_len - new_len;
            self.mine -= old_len - new_len;
        }
    }
    fn alloc(ctx: *anyopaque, len: usize, alignment: std.mem.Alignment, ret_addr: usize) ?[*]u8 {
        const self: *GenAlloc = @ptrCast(@alignCast(ctx));
        const ptr = self.child.rawAlloc(len, alignment, ret_addr) orelse return null;
        self.counters.allocations += 1;
        self.grow(0, len);
        return ptr;
    }
    fn resize(ctx: *anyopaque, memory: []u8, alignment: std.mem.Alignment, new_len: usize, ret_addr: usize) bool {
        const self: *GenAlloc = @ptrCast(@alignCast(ctx));
        if (!self.child.rawResize(memory, alignment, new_len, ret_addr)) return false;
        self.grow(memory.len, new_len);
        return true;
    }
    fn remap(ctx: *anyopaque, memory: []u8, alignment: std.mem.Alignment, new_len: usize, ret_addr: usize) ?[*]u8 {
        const self: *GenAlloc = @ptrCast(@alignCast(ctx));
        const ptr = self.child.rawRemap(memory, alignment, new_len, ret_addr) orelse return null;
        if (ptr != memory.ptr) self.counters.allocations += 1;
        self.grow(memory.len, new_len);
        return ptr;
    }
    fn free(ctx: *anyopaque, memory: []u8, alignment: std.mem.Alignment, ret_addr: usize) void {
        const self: *GenAlloc = @ptrCast(@alignCast(ctx));
        self.child.rawFree(memory, alignment, ret_addr);
        self.grow(memory.len, 0);
    }
};

// ---- state generations ------------------------------------------------------------------
const Context = struct { snapshot: []const u8, epoch: ?u64, complete: bool };
const LEnt = struct { id: u64, kind: sem.Kind, name: []const u8, container: ?u64 };
/// Fixture with an optional epoch: the context must distinguish "absent" (null) from 0.
const SFix = struct { schema: []const u8, snapshot: []const u8, epoch: ?u64 = null, strings: [][]const u8, entities: []sem.Entity, relations: []sem.Edge, evidence: []sem.Evidence, complete: bool = false };

const Gen = struct {
    arena: std.heap.ArenaAllocator,
    counting: GenAlloc,
    ctx: Context,
    fx: sem.Fixture,
    store: sem.Store,

    fn create(counters: *Counters) !*Gen {
        const g = try std.heap.page_allocator.create(Gen);
        g.arena = std.heap.ArenaAllocator.init(std.heap.page_allocator);
        g.counting = .{ .child = g.arena.allocator(), .counters = counters };
        return g;
    }
    fn alloc(self: *Gen) A {
        return self.counting.allocator();
    }
    fn destroy(self: *Gen) void {
        self.counting.counters.live -= self.counting.mine;
        self.counting.counters.total_freed += self.counting.mine;
        self.arena.deinit();
        std.heap.page_allocator.destroy(self);
    }
    /// Build every derived structure (entity map, adjacency, name index) over `fx`.
    fn finish(self: *Gen, ctx: Context, fx: sem.Fixture) Err!void {
        self.ctx = ctx;
        self.fx = fx;
        self.store = sem.Store.init(self.alloc(), self.fx) catch |e| return if (e == error.OutOfMemory) error.OutOfMemory else error.InvalidInput;
        self.store.name_cache.?.ensure(self.fx) catch return error.OutOfMemory;
    }
    fn entityName(self: *const Gen, e: sem.Entity) []const u8 {
        return self.fx.strings[e.name_sid];
    }
};

fn oom(e: anyerror) Err {
    return if (e == error.OutOfMemory) error.OutOfMemory else error.InvalidInput;
}

/// `open` from a fixture file: parse straight into the generation's arena (no second copy).
fn genFromFixture(counters: *Counters, bytes: []const u8) Err!*Gen {
    const g = Gen.create(counters) catch return error.OutOfMemory;
    errdefer g.destroy();
    const a = g.alloc();
    const fx = std.json.parseFromSliceLeaky(SFix, a, bytes, .{ .allocate = .alloc_always }) catch |e| return oom(e);
    const ctx = Context{ .snapshot = fx.snapshot, .epoch = fx.epoch, .complete = fx.complete };
    try g.finish(ctx, .{ .schema = fx.schema, .snapshot = fx.snapshot, .epoch = fx.epoch orelse 0, .strings = fx.strings, .entities = fx.entities, .relations = fx.relations, .evidence = fx.evidence, .complete = fx.complete });
    return g;
}

/// Build a generation from logical rows (mutate, restore, empty open). Names are interned into a string table.
fn genFromRows(counters: *Counters, ctx: Context, ents: []const LEnt, rels: []const sem.Edge, evs: []const sem.Evidence) Err!*Gen {
    const g = Gen.create(counters) catch return error.OutOfMemory;
    errdefer g.destroy();
    const a = g.alloc();
    var table = std.StringHashMap(u32).init(std.heap.page_allocator);
    defer table.deinit();
    var strings = std.ArrayList([]const u8).init(a);
    const entities = a.alloc(sem.Entity, ents.len) catch return error.OutOfMemory;
    for (ents, 0..) |e, i| {
        const found = table.getOrPut(e.name) catch return error.OutOfMemory;
        if (!found.found_existing) {
            const copy = a.dupe(u8, e.name) catch return error.OutOfMemory;
            found.key_ptr.* = copy;
            found.value_ptr.* = @intCast(strings.items.len);
            strings.append(copy) catch return error.OutOfMemory;
        }
        entities[i] = .{ .id = e.id, .kind = e.kind, .name_sid = found.value_ptr.*, .container = e.container };
    }
    const snapshot = a.dupe(u8, ctx.snapshot) catch return error.OutOfMemory;
    const relations = a.dupe(sem.Edge, rels) catch return error.OutOfMemory;
    const evidence = a.dupe(sem.Evidence, evs) catch return error.OutOfMemory;
    try g.finish(.{ .snapshot = snapshot, .epoch = ctx.epoch, .complete = ctx.complete }, .{
        .schema = "csl.eval.fixture/v0.1",
        .snapshot = snapshot,
        .epoch = ctx.epoch orelse 0,
        .strings = strings.items,
        .entities = entities,
        .relations = relations,
        .evidence = evidence,
        .complete = ctx.complete,
    });
    return g;
}

// ---- JSON value helpers ------------------------------------------------------------------
fn asU64(v: Value) ?u64 {
    return switch (v) {
        .integer => |i| if (i >= 0) @intCast(i) else null,
        .number_string => |s| std.fmt.parseInt(u64, s, 10) catch null,
        else => null,
    };
}
fn asId(v: Value) ?u64 {
    const x = asU64(v) orelse return null;
    return if (x >= 1) x else null;
}
fn asEnum(comptime T: type, v: Value) ?T {
    return if (v == .string) std.meta.stringToEnum(T, v.string) else null;
}
/// `null` or an id: `ok` false when the value is neither.
fn asContainer(v: ?Value, ok: *bool) ?u64 {
    const value = v orelse return null;
    if (value == .null) return null;
    if (asId(value)) |id| return id;
    ok.* = false;
    return null;
}
fn exactKeys(o: ObjectMap, comptime keys: []const []const u8) bool {
    if (o.count() != keys.len) return false;
    inline for (keys) |k| if (o.get(k) == null) return false;
    return true;
}
fn onlyKeys(o: ObjectMap, comptime allowed: []const []const u8) bool {
    var it = o.iterator();
    outer: while (it.next()) |e| {
        inline for (allowed) |k| if (std.mem.eql(u8, e.key_ptr.*, k)) continue :outer;
        return false;
    }
    return true;
}
fn parseEdge(o: ObjectMap) Err!sem.Edge {
    return .{
        .subject = asId(o.get("subject").?) orelse return error.InvalidInput,
        .relation = asEnum(sem.Relation, o.get("relation").?) orelse return error.InvalidInput,
        .object = asId(o.get("object").?) orelse return error.InvalidInput,
    };
}
fn parseEvidence(o: ObjectMap) Err!sem.Evidence {
    const lineage = asU64(o.get("lineage").?) orelse return error.InvalidInput;
    if (lineage > std.math.maxInt(u32)) return error.InvalidInput;
    return .{
        .proposition = asId(o.get("proposition").?) orelse return error.InvalidInput,
        .subject = asId(o.get("subject").?) orelse return error.InvalidInput,
        .relation = asEnum(sem.Relation, o.get("relation").?) orelse return error.InvalidInput,
        .object = asId(o.get("object").?) orelse return error.InvalidInput,
        .polarity = asEnum(sem.Polarity, o.get("polarity").?) orelse return error.InvalidInput,
        .quality = asEnum(sem.Quality, o.get("quality").?) orelse return error.InvalidInput,
        .freshness_epoch = asU64(o.get("freshness_epoch").?) orelse return error.InvalidInput,
        .lineage = @intCast(lineage),
    };
}

// ---- mutation ----------------------------------------------------------------------------
const OpKind = enum { ADD_ENTITY, REMOVE_ENTITY, UPDATE_ENTITY, ADD_RELATION, REMOVE_RELATION, ADD_EVIDENCE, REMOVE_EVIDENCE };
const EntityOp = struct { kind: OpKind, id: ?u64, obj: ObjectMap };
const RelMap = std.AutoHashMap(sem.Edge, u32);
const EvMap = std.AutoHashMap(sem.Evidence, u32);

fn bump(map: anytype, key: anytype) !void {
    const e = try map.getOrPut(key);
    if (!e.found_existing) e.value_ptr.* = 0;
    e.value_ptr.* += 1;
}

/// Validate `batch` per ADR-0008 (same order of checks as oracle/session_model.py) and return the new
/// generation, or an error leaving `cur` untouched. `ra` holds scratch data for this request.
fn applyBatch(counters: *Counters, ra: A, cur: *const Gen, batch: Value) Err!*Gen {
    if (batch != .array) return error.InvalidRequest;
    if (batch.array.items.len == 0) return error.InvalidInput;
    var entity_ops = std.ArrayList(EntityOp).init(ra);
    var seen_ids = std.AutoHashMap(u64, void).init(ra);
    var rel_add = RelMap.init(ra);
    var rel_rem = RelMap.init(ra);
    var ev_add = EvMap.init(ra);
    var ev_rem = EvMap.init(ra);
    for (batch.array.items) |item| {
        if (item != .object) return error.InvalidRequest;
        const o = item.object;
        const name = o.get("op") orelse return error.InvalidRequest;
        if (name != .string) return error.InvalidRequest;
        const kind = std.meta.stringToEnum(OpKind, name.string) orelse return error.InvalidRequest;
        switch (kind) {
            .ADD_ENTITY, .REMOVE_ENTITY, .UPDATE_ENTITY => {
                const ok = switch (kind) {
                    .ADD_ENTITY => onlyKeys(o, &.{ "op", "id", "kind", "name", "container" }) and o.get("id") != null and o.get("kind") != null and o.get("name") != null,
                    .REMOVE_ENTITY => onlyKeys(o, &.{ "op", "id" }) and o.get("id") != null,
                    else => onlyKeys(o, &.{ "op", "id", "set" }) and o.get("id") != null and o.get("set") != null and o.get("set").? == .object,
                };
                if (!ok) return error.InvalidRequest;
                const id = asU64(o.get("id").?);
                if (id) |x| {
                    const entry = seen_ids.getOrPut(x) catch return error.OutOfMemory;
                    if (entry.found_existing) return error.InvalidInput;
                }
                entity_ops.append(.{ .kind = kind, .id = id, .obj = o }) catch return error.OutOfMemory;
            },
            .ADD_RELATION, .REMOVE_RELATION => {
                if (!exactKeys(o, &.{ "op", "subject", "relation", "object" })) return error.InvalidRequest;
                const edge = try parseEdge(o);
                bump(if (kind == .ADD_RELATION) &rel_add else &rel_rem, edge) catch return error.OutOfMemory;
            },
            .ADD_EVIDENCE, .REMOVE_EVIDENCE => {
                if (!exactKeys(o, &.{ "op", "proposition", "subject", "relation", "object", "polarity", "quality", "freshness_epoch", "lineage" })) return error.InvalidRequest;
                const row = try parseEvidence(o);
                bump(if (kind == .ADD_EVIDENCE) &ev_add else &ev_rem, row) catch return error.OutOfMemory;
            },
        }
    }
    var kit = rel_add.keyIterator();
    while (kit.next()) |k| if (rel_rem.contains(k.*)) return error.InvalidInput;
    var eit = ev_add.keyIterator();
    while (eit.next()) |k| if (ev_rem.contains(k.*)) return error.InvalidInput;

    // Working multisets, seeded from the current generation.
    var ents = std.AutoHashMap(u64, LEnt).init(ra);
    for (cur.fx.entities) |e| ents.put(e.id, .{ .id = e.id, .kind = e.kind, .name = cur.entityName(e), .container = e.container }) catch return error.OutOfMemory;
    for (entity_ops.items) |op| {
        const o = op.obj;
        switch (op.kind) {
            .ADD_ENTITY => {
                const id = if (op.id) |x| (if (x >= 1) x else return error.InvalidInput) else return error.InvalidInput;
                const kind = asEnum(sem.Kind, o.get("kind").?) orelse return error.InvalidInput;
                var container_ok = true;
                const container = asContainer(o.get("container"), &container_ok);
                if (!container_ok or o.get("name").? != .string) return error.InvalidInput;
                if (findEntity(cur, id)) return error.InvalidInput;
                ents.put(id, .{ .id = id, .kind = kind, .name = o.get("name").?.string, .container = container }) catch return error.OutOfMemory;
            },
            .REMOVE_ENTITY => {
                const id = op.id orelse return error.InvalidInput;
                if (!findEntity(cur, id)) return error.InvalidInput;
                _ = ents.remove(id);
            },
            .UPDATE_ENTITY => {
                const id = op.id orelse return error.InvalidInput;
                if (!findEntity(cur, id)) return error.InvalidInput;
                const changes = o.get("set").?.object;
                if (changes.count() == 0 or !onlyKeys(changes, &.{ "kind", "name", "container" })) return error.InvalidInput;
                var merged = ents.get(id).?;
                if (changes.get("kind")) |v| merged.kind = asEnum(sem.Kind, v) orelse return error.InvalidInput;
                if (changes.get("name")) |v| {
                    if (v != .string) return error.InvalidInput;
                    merged.name = v.string;
                }
                if (changes.get("container")) |_| {
                    var ok = true;
                    merged.container = asContainer(changes.get("container"), &ok);
                    if (!ok) return error.InvalidInput;
                }
                ents.put(id, merged) catch return error.OutOfMemory;
            },
            else => unreachable,
        }
    }
    var rels = RelMap.init(ra);
    for (cur.fx.relations) |r| bump(&rels, r) catch return error.OutOfMemory;
    var evs = EvMap.init(ra);
    for (cur.fx.evidence) |r| bump(&evs, r) catch return error.OutOfMemory;
    var rit = rel_rem.iterator();
    while (rit.next()) |e| if ((rels.get(e.key_ptr.*) orelse 0) < e.value_ptr.*) return error.InvalidInput;
    var vit = ev_rem.iterator();
    while (vit.next()) |e| if ((evs.get(e.key_ptr.*) orelse 0) < e.value_ptr.*) return error.InvalidInput;
    rit = rel_rem.iterator();
    while (rit.next()) |e| rels.getPtr(e.key_ptr.*).?.* -= e.value_ptr.*;
    vit = ev_rem.iterator();
    while (vit.next()) |e| evs.getPtr(e.key_ptr.*).?.* -= e.value_ptr.*;
    rit = rel_add.iterator();
    while (rit.next()) |e| {
        const slot = rels.getOrPut(e.key_ptr.*) catch return error.OutOfMemory;
        if (!slot.found_existing) slot.value_ptr.* = 0;
        slot.value_ptr.* += e.value_ptr.*;
    }
    vit = ev_add.iterator();
    while (vit.next()) |e| {
        const slot = evs.getOrPut(e.key_ptr.*) catch return error.OutOfMemory;
        if (!slot.found_existing) slot.value_ptr.* = 0;
        slot.value_ptr.* += e.value_ptr.*;
    }
    // Final-state integrity, then the new rows.
    var rel_rows = std.ArrayList(sem.Edge).init(ra);
    var it = rels.iterator();
    while (it.next()) |e| {
        if (e.value_ptr.* == 0) continue;
        if (!ents.contains(e.key_ptr.subject) or !ents.contains(e.key_ptr.object)) return error.InvalidInput;
        var n = e.value_ptr.*;
        while (n > 0) : (n -= 1) rel_rows.append(e.key_ptr.*) catch return error.OutOfMemory;
    }
    var ev_rows = std.ArrayList(sem.Evidence).init(ra);
    var vit2 = evs.iterator();
    while (vit2.next()) |e| {
        if (e.value_ptr.* == 0) continue;
        if (!ents.contains(e.key_ptr.subject) or !ents.contains(e.key_ptr.object)) return error.InvalidInput;
        var n = e.value_ptr.*;
        while (n > 0) : (n -= 1) ev_rows.append(e.key_ptr.*) catch return error.OutOfMemory;
    }
    var ent_rows = std.ArrayList(LEnt).init(ra);
    var nit = ents.valueIterator();
    while (nit.next()) |e| {
        if (e.container) |c| if (!ents.contains(c)) return error.InvalidInput;
        ent_rows.append(e.*) catch return error.OutOfMemory;
    }
    return genFromRows(counters, cur.ctx, ent_rows.items, rel_rows.items, ev_rows.items);
}

fn findEntity(g: *const Gen, id: u64) bool {
    return g.store.entities.contains(id);
}

// ---- state digest ------------------------------------------------------------------------
fn entityLess(_: void, a: sem.Entity, b: sem.Entity) bool {
    return a.id < b.id;
}

/// SHA-256 over the canonical logical state (SESSION-SEMANTICS.md section 2).
fn stateDigest(ra: A, g: *const Gen) !struct { digest: [32]u8, bytes: u64 } {
    const ents = try ra.dupe(sem.Entity, g.fx.entities);
    std.mem.sort(sem.Entity, ents, {}, entityLess);
    const rels = try ra.dupe(sem.Edge, g.fx.relations);
    std.mem.sort(sem.Edge, rels, {}, sem.edgeLess);
    const evs = try ra.dupe(sem.Evidence, g.fx.evidence);
    std.mem.sort(sem.Evidence, evs, {}, sem.evLess);
    var s = Sink.init(ra, true);
    try s.raw("{\"complete\":");
    try s.raw(if (g.ctx.complete) "true" else "false");
    try s.raw(",\"entities\":[");
    for (ents, 0..) |e, i| {
        if (i > 0) try s.raw(",");
        try s.raw("{\"container\":");
        try s.optNum(e.container);
        try s.raw(",\"id\":");
        try s.num(e.id);
        try s.raw(",\"kind\":");
        try s.str(@tagName(e.kind));
        try s.raw(",\"name\":");
        try s.str(g.entityName(e));
        try s.raw("}");
    }
    try s.raw("],\"epoch\":");
    try s.optNum(g.ctx.epoch);
    try s.raw(",\"evidence\":[");
    for (evs, 0..) |e, i| {
        if (i > 0) try s.raw(",");
        try s.raw("{\"freshness_epoch\":");
        try s.num(e.freshness_epoch);
        try s.raw(",\"lineage\":");
        try s.num(e.lineage);
        try s.raw(",\"object\":");
        try s.num(e.object);
        try s.raw(",\"polarity\":");
        try s.str(@tagName(e.polarity));
        try s.raw(",\"proposition\":");
        try s.num(e.proposition);
        try s.raw(",\"quality\":");
        try s.str(@tagName(e.quality));
        try s.raw(",\"relation\":");
        try s.str(@tagName(e.relation));
        try s.raw(",\"subject\":");
        try s.num(e.subject);
        try s.raw("}");
    }
    try s.raw("],\"relations\":[");
    for (rels, 0..) |e, i| {
        if (i > 0) try s.raw(",");
        try s.raw("{\"object\":");
        try s.num(e.object);
        try s.raw(",\"relation\":");
        try s.str(@tagName(e.relation));
        try s.raw(",\"subject\":");
        try s.num(e.subject);
        try s.raw("}");
    }
    try s.raw("],\"snapshot\":");
    try s.str(g.ctx.snapshot);
    try s.raw("}");
    const done = s.finish();
    return .{ .digest = done.digest, .bytes = done.bytes };
}

// ---- snapshot files ----------------------------------------------------------------------
fn putInt(list: *std.ArrayList(u8), comptime T: type, v: T) !void {
    var tmp: [@sizeOf(T)]u8 = undefined;
    std.mem.writeInt(T, &tmp, v, .little);
    try list.appendSlice(&tmp);
}
fn putBytes(list: *std.ArrayList(u8), s: []const u8) !void {
    try putInt(list, u32, @intCast(s.len));
    try list.appendSlice(s);
}

const Reader = struct {
    data: []const u8,
    pos: usize = 0,
    fn take(self: *Reader, n: usize) Err![]const u8 {
        if (n > self.data.len - self.pos) return error.InvalidInput;
        const out = self.data[self.pos .. self.pos + n];
        self.pos += n;
        return out;
    }
    fn int(self: *Reader, comptime T: type) Err!T {
        return std.mem.readInt(T, (try self.take(@sizeOf(T)))[0..@sizeOf(T)], .little);
    }
    fn bytes(self: *Reader) Err![]const u8 {
        const n = try self.int(u32);
        return self.take(n);
    }
};

fn encodeSnapshot(ra: A, g: *const Gen) ![]u8 {
    var out = std.ArrayList(u8).init(ra);
    try out.appendSlice(SNAP_MAGIC);
    try putInt(&out, u32, SNAP_VERSION);
    try putBytes(&out, ARTIFACT);
    try putBytes(&out, g.ctx.snapshot);
    try out.append(if (g.ctx.epoch != null) 1 else 0);
    try putInt(&out, u64, g.ctx.epoch orelse 0);
    try out.append(if (g.ctx.complete) 1 else 0);
    try putInt(&out, u64, g.fx.entities.len);
    try putInt(&out, u64, g.fx.relations.len);
    try putInt(&out, u64, g.fx.evidence.len);
    for (g.fx.entities) |e| {
        try putInt(&out, u64, e.id);
        try out.append(@intFromEnum(e.kind));
        try out.append(if (e.container != null) 1 else 0);
        try putInt(&out, u64, e.container orelse 0);
        try putBytes(&out, g.entityName(e));
    }
    for (g.fx.relations) |r| {
        try putInt(&out, u64, r.subject);
        try out.append(@intFromEnum(r.relation));
        try putInt(&out, u64, r.object);
    }
    for (g.fx.evidence) |r| {
        try putInt(&out, u64, r.proposition);
        try putInt(&out, u64, r.subject);
        try out.append(@intFromEnum(r.relation));
        try putInt(&out, u64, r.object);
        try out.append(@intFromEnum(r.polarity));
        try out.append(@intFromEnum(r.quality));
        try putInt(&out, u64, r.freshness_epoch);
        try putInt(&out, u32, r.lineage);
    }
    var hash: [32]u8 = undefined;
    Sha256.hash(out.items, &hash, .{});
    try out.appendSlice(&hash);
    return out.toOwnedSlice();
}

fn enumFromByte(comptime T: type, b: u8) Err!T {
    inline for (@typeInfo(T).@"enum".fields) |f| if (f.value == b) return @enumFromInt(f.value);
    return error.InvalidInput;
}

fn decodeSnapshot(counters: *Counters, ra: A, data: []const u8, ctx: Context) Err!*Gen {
    if (data.len < SNAP_MAGIC.len + 32) return error.InvalidInput;
    var hash: [32]u8 = undefined;
    Sha256.hash(data[0 .. data.len - 32], &hash, .{});
    if (!std.mem.eql(u8, &hash, data[data.len - 32 ..])) return error.InvalidInput;
    var r = Reader{ .data = data[0 .. data.len - 32] };
    if (!std.mem.eql(u8, try r.take(SNAP_MAGIC.len), SNAP_MAGIC)) return error.InvalidInput;
    if (try r.int(u32) != SNAP_VERSION) return error.InvalidInput;
    if (!std.mem.eql(u8, try r.bytes(), ARTIFACT)) return error.InvalidInput;
    const snapshot = try r.bytes();
    const epoch_present = (try r.take(1))[0] == 1;
    const epoch = try r.int(u64);
    const complete = (try r.take(1))[0] == 1;
    if (!std.mem.eql(u8, snapshot, ctx.snapshot) or complete != ctx.complete or epoch_present != (ctx.epoch != null) or (epoch_present and epoch != ctx.epoch.?)) return error.InvalidInput;
    const ne = try r.int(u64);
    const nr = try r.int(u64);
    const nv = try r.int(u64);
    if (ne > data.len or nr > data.len or nv > data.len) return error.InvalidInput;
    const ents = ra.alloc(LEnt, @intCast(ne)) catch return error.OutOfMemory;
    for (ents) |*e| {
        const id = try r.int(u64);
        const kind = try enumFromByte(sem.Kind, (try r.take(1))[0]);
        const has = (try r.take(1))[0] == 1;
        const container = try r.int(u64);
        e.* = .{ .id = id, .kind = kind, .container = if (has) container else null, .name = try r.bytes() };
    }
    const rels = ra.alloc(sem.Edge, @intCast(nr)) catch return error.OutOfMemory;
    for (rels) |*e| {
        const subject = try r.int(u64);
        const relation = try enumFromByte(sem.Relation, (try r.take(1))[0]);
        e.* = .{ .subject = subject, .relation = relation, .object = try r.int(u64) };
    }
    const evs = ra.alloc(sem.Evidence, @intCast(nv)) catch return error.OutOfMemory;
    for (evs) |*e| {
        const proposition = try r.int(u64);
        const subject = try r.int(u64);
        const relation = try enumFromByte(sem.Relation, (try r.take(1))[0]);
        const object = try r.int(u64);
        const polarity = try enumFromByte(sem.Polarity, (try r.take(1))[0]);
        const quality = try enumFromByte(sem.Quality, (try r.take(1))[0]);
        const freshness_epoch = try r.int(u64);
        e.* = .{ .proposition = proposition, .subject = subject, .relation = relation, .object = object, .polarity = polarity, .quality = quality, .freshness_epoch = freshness_epoch, .lineage = try r.int(u32) };
    }
    if (r.pos != r.data.len) return error.InvalidInput;
    return genFromRows(counters, ctx, ents, rels, evs);
}

// ---- the session -------------------------------------------------------------------------
const State = enum { NEW, OPEN, FAILED };
const Op = enum { open, query, mutate, state_digest, snapshot, restore, cancel, stats, close };
const Pending = struct { id: u64, op: []const u8, data: std.ArrayList(u8), hasher: Sha256, chunks: u64 };

const Session = struct {
    repo: []const u8,
    state: State = .NEW,
    gen: ?*Gen = null,
    generation: u64 = 0,
    counters: Counters = .{},
    max_line: u64 = MIN_LINE,
    snapshots: u64 = 0,
    out: std.io.BufferedWriter(1 << 16, std.fs.File.Writer),
    pending: ?Pending = null,
    pending_arena: std.heap.ArenaAllocator,

    fn chunkBytes(self: *const Session) u64 {
        return ((self.max_line - FRAME_OVERHEAD) / 4) * 3;
    }
    fn writeLine(self: *Session, s: []const u8) !void {
        const w = self.out.writer();
        try w.writeAll(s);
        try w.writeByte('\n');
    }
    fn flush(self: *Session) void {
        self.out.flush() catch std.process.exit(1);
    }
    fn errorLine(ra: A, id: ?u64, code: []const u8, msg: []const u8) ![]const u8 {
        var s = Sink.init(ra, false);
        try s.raw("{\"code\":");
        try s.str(code);
        try s.raw(",\"id\":");
        try s.optNum(id);
        try s.raw(",\"message\":");
        try s.str(msg);
        try s.raw(",\"ok\":false}");
        return s.buf.items;
    }
    fn respondError(self: *Session, ra: A, id: ?u64, e: anyerror) void {
        const line = errorLine(ra, id, codeOf(e), @errorName(e)) catch return;
        self.writeLine(line) catch std.process.exit(1);
    }

    fn handleLine(self: *Session, ra: A, line: []const u8) !void {
        const parsed = std.json.parseFromSliceLeaky(Value, ra, line, .{}) catch {
            self.respondError(ra, null, error.InvalidRequest);
            return;
        };
        if (parsed != .object) return self.respondError(ra, null, error.InvalidRequest);
        var obj = parsed.object;
        const id: ?u64 = if (obj.get("id")) |v| asU64(v) else null;
        if (obj.get("frame")) |frame| {
            // Transport frames of a chunked request; only `end` produces a terminal response.
            const merged = self.readFrame(ra, obj, frame, id) catch |e| {
                self.pending = null;
                _ = self.pending_arena.reset(.free_all);
                return self.respondError(ra, id, e);
            };
            obj = merged orelse return;
        } else if (self.pending != null) {
            self.pending = null;
            _ = self.pending_arena.reset(.free_all);
        }
        const rid = if (obj.get("id")) |v| asU64(v) else null;
        self.dispatch(ra, obj) catch |e| {
            if (e != error.InvalidRequest and e != error.InvalidState and e != error.InvalidInput and e != error.Unsupported and e != error.OutOfMemory) self.state = .FAILED;
            self.respondError(ra, rid, e);
        };
    }

    /// Process one transport frame; returns the reassembled request object on a verified `end`.
    fn readFrame(self: *Session, ra: A, obj: ObjectMap, frame_v: Value, id: ?u64) !?ObjectMap {
        const kind = if (frame_v == .string) frame_v.string else return error.InvalidRequest;
        const rid = id orelse return error.InvalidRequest;
        if (std.mem.eql(u8, kind, "begin")) {
            if (self.pending != null) return error.InvalidRequest;
            if (!exactKeys(obj, &.{ "id", "op", "frame" })) return error.InvalidRequest;
            const op = obj.get("op").?;
            if (op != .string) return error.InvalidRequest;
            const pa = self.pending_arena.allocator();
            self.pending = .{ .id = rid, .op = try pa.dupe(u8, op.string), .data = std.ArrayList(u8).init(pa), .hasher = Sha256.init(.{}), .chunks = 0 };
            return null;
        }
        const p = &(self.pending orelse return error.InvalidRequest);
        if (p.id != rid) return error.InvalidRequest;
        if (std.mem.eql(u8, kind, "chunk")) {
            if (!exactKeys(obj, &.{ "id", "frame", "seq", "data" })) return error.InvalidRequest;
            const seq = asU64(obj.get("seq").?) orelse return error.InvalidRequest;
            const data = obj.get("data").?;
            if (seq != p.chunks or data != .string) return error.InvalidRequest;
            const n = std.base64.standard.Decoder.calcSizeForSlice(data.string) catch return error.InvalidRequest;
            const buf = try ra.alloc(u8, n);
            std.base64.standard.Decoder.decode(buf, data.string) catch return error.InvalidRequest;
            try p.data.appendSlice(buf);
            p.hasher.update(buf);
            p.chunks += 1;
            return null;
        }
        if (std.mem.eql(u8, kind, "end")) {
            if (!exactKeys(obj, &.{ "id", "frame", "chunks", "bytes", "sha256" })) return error.InvalidRequest;
            const chunks = asU64(obj.get("chunks").?) orelse return error.InvalidRequest;
            const total = asU64(obj.get("bytes").?) orelse return error.InvalidRequest;
            const want = obj.get("sha256").?;
            var out: [32]u8 = undefined;
            var h = p.hasher;
            h.final(&out);
            const hex = try hexLower(ra, &out);
            if (chunks != p.chunks or total != p.data.items.len or want != .string or !std.mem.eql(u8, want.string, hex)) return error.InvalidRequest;
            const body = std.json.parseFromSliceLeaky(Value, ra, p.data.items, .{}) catch return error.InvalidRequest;
            if (body != .object) return error.InvalidRequest;
            var merged = ObjectMap.init(ra);
            var it = body.object.iterator();
            while (it.next()) |e| try merged.put(e.key_ptr.*, e.value_ptr.*);
            if (merged.contains("id") or merged.contains("op")) return error.InvalidRequest;
            try merged.put("id", .{ .integer = @intCast(p.id) });
            try merged.put("op", .{ .string = try ra.dupe(u8, p.op) });
            self.pending = null;
            _ = self.pending_arena.reset(.free_all);
            return merged;
        }
        return error.InvalidRequest;
    }

    fn dispatch(self: *Session, ra: A, obj: ObjectMap) !void {
        const id = (if (obj.get("id")) |v| asU64(v) else null) orelse return error.InvalidRequest;
        const op_v = obj.get("op") orelse return error.InvalidRequest;
        if (op_v != .string) return error.InvalidRequest;
        const op = std.meta.stringToEnum(Op, op_v.string) orelse return error.InvalidRequest;
        switch (op) {
            .open => if (self.state != .NEW) return error.InvalidState,
            .close => if (self.state == .NEW) return error.InvalidState,
            else => if (self.state != .OPEN) return error.InvalidState,
        }
        switch (op) {
            .open => try self.doOpen(ra, id, obj),
            .query => try self.doQuery(ra, id, obj),
            .mutate => try self.doMutate(ra, id, obj),
            .state_digest => try self.doDigest(ra, id, obj),
            .snapshot => try self.doSnapshot(ra, id, obj),
            .restore => try self.doRestore(ra, id, obj),
            .cancel => {
                if (!exactKeys(obj, &.{ "id", "op", "target" })) return error.InvalidRequest;
                return error.Unsupported;
            },
            .stats => try self.doStats(ra, id, obj),
            .close => {
                if (!exactKeys(obj, &.{ "id", "op" })) return error.InvalidRequest;
                var s = Sink.init(ra, false);
                try s.raw("{\"id\":");
                try s.num(id);
                try s.raw(",\"ok\":true}");
                try self.writeLine(s.buf.items);
                self.flush();
                std.process.exit(0);
            },
        }
    }

    fn head(s: *Sink, id: u64, generation: ?u64) !void {
        if (generation) |g| {
            try s.raw("{\"generation\":");
            try s.num(g);
            try s.raw(",\"id\":");
        } else try s.raw("{\"id\":");
        try s.num(id);
    }

    fn doOpen(self: *Session, ra: A, id: u64, obj: ObjectMap) !void {
        if (!exactKeys(obj, &.{ "id", "op", "binding", "max_line_bytes", "source" })) return error.InvalidRequest;
        const binding = obj.get("binding").?;
        if (binding != .string or !std.mem.eql(u8, binding.string, BINDING)) return error.InvalidRequest;
        const max_line = asU64(obj.get("max_line_bytes").?) orelse return error.InvalidRequest;
        if (max_line < MIN_LINE) return error.InvalidRequest;
        const src = obj.get("source").?;
        if (src != .object) return error.InvalidRequest;
        const kind = src.object.get("kind") orelse return error.InvalidRequest;
        if (kind != .string) return error.InvalidRequest;
        var gen: *Gen = undefined;
        if (std.mem.eql(u8, kind.string, "fixture")) {
            if (!exactKeys(src.object, &.{ "kind", "path" })) return error.InvalidRequest;
            const path = src.object.get("path").?;
            if (path != .string) return error.InvalidRequest;
            const bytes = std.fs.cwd().readFileAlloc(ra, path.string, std.math.maxInt(usize)) catch |e| return oom(e);
            gen = try genFromFixture(&self.counters, bytes);
        } else if (std.mem.eql(u8, kind.string, "empty")) {
            if (!exactKeys(src.object, &.{ "kind", "context" })) return error.InvalidRequest;
            const c = src.object.get("context").?;
            if (c != .object or !exactKeys(c.object, &.{ "snapshot", "epoch", "complete" })) return error.InvalidRequest;
            const snap = c.object.get("snapshot").?;
            const complete = c.object.get("complete").?;
            const epoch = c.object.get("epoch").?;
            if (snap != .string or complete != .bool or !(epoch == .null or asU64(epoch) != null)) return error.InvalidRequest;
            gen = try genFromRows(&self.counters, .{ .snapshot = snap.string, .epoch = if (epoch == .null) null else asU64(epoch), .complete = complete.bool }, &.{}, &.{}, &.{});
        } else return error.InvalidRequest;
        self.gen = gen;
        self.generation = 0;
        self.state = .OPEN;
        self.max_line = max_line;
        var s = Sink.init(ra, false);
        try s.raw("{\"artifact\":");
        try s.str(ARTIFACT);
        try s.raw(",\"binding\":");
        try s.str(BINDING);
        try s.raw(",\"capabilities\":[],\"chunk_bytes\":");
        try s.num(self.chunkBytes());
        try s.raw(",\"generation\":0,\"id\":");
        try s.num(id);
        try s.raw(",\"max_line_bytes\":");
        try s.num(self.max_line);
        try s.raw(",\"ok\":true,\"semantics\":");
        try s.str(SEMANTICS);
        try s.raw(",\"strategy\":{\"mutation\":\"full-rebuild\"}}");
        try self.writeLine(s.buf.items);
    }

    fn doQuery(self: *Session, ra: A, id: u64, obj: ObjectMap) !void {
        if (!exactKeys(obj, &.{ "id", "op", "query" })) return error.InvalidRequest;
        const text = std.json.stringifyAlloc(ra, obj.get("query").?, .{}) catch return error.OutOfMemory;
        const q = std.json.parseFromSliceLeaky(sem.Query, ra, text, .{}) catch |e| return oom(e);
        // Scratch memory for this query comes from the request arena, not the store's generation.
        var store = self.gen.?.store;
        store.a = ra;
        const result = store.execute(q) catch |e| return oom(e);
        var s = Sink.init(ra, false);
        try head(&s, id, self.generation);
        try s.raw(",\"ok\":true,\"result\":");
        try s.raw(result);
        try s.raw("}");
        if (s.buf.items.len + 1 <= self.max_line) return self.writeLine(s.buf.items);
        try self.writeChunked(ra, id, result);
    }

    fn writeChunked(self: *Session, ra: A, id: u64, payload: []const u8) !void {
        var b = Sink.init(ra, false);
        try b.raw("{\"frame\":\"begin\",\"id\":");
        try b.num(id);
        try b.raw("}");
        try self.writeLine(b.buf.items);
        const step = self.chunkBytes();
        var hash = Sha256.init(.{});
        var seq: u64 = 0;
        var pos: usize = 0;
        while (pos < payload.len) : (seq += 1) {
            const end = @min(payload.len, pos + step);
            const piece = payload[pos..end];
            hash.update(piece);
            const enc = try ra.alloc(u8, std.base64.standard.Encoder.calcSize(piece.len));
            _ = std.base64.standard.Encoder.encode(enc, piece);
            var c = Sink.init(ra, false);
            try c.raw("{\"data\":\"");
            try c.raw(enc);
            try c.raw("\",\"frame\":\"chunk\",\"id\":");
            try c.num(id);
            try c.raw(",\"seq\":");
            try c.num(seq);
            try c.raw("}");
            try self.writeLine(c.buf.items);
            ra.free(enc);
            pos = end;
        }
        var out: [32]u8 = undefined;
        hash.final(&out);
        var e = Sink.init(ra, false);
        try e.raw("{\"bytes\":");
        try e.num(payload.len);
        try e.raw(",\"chunks\":");
        try e.num(seq);
        try e.raw(",\"frame\":\"end\",\"generation\":");
        try e.num(self.generation);
        try e.raw(",\"id\":");
        try e.num(id);
        try e.raw(",\"ok\":true,\"sha256\":");
        try e.str(try hexLower(ra, &out));
        try e.raw("}");
        try self.writeLine(e.buf.items);
    }

    fn doMutate(self: *Session, ra: A, id: u64, obj: ObjectMap) !void {
        if (!exactKeys(obj, &.{ "id", "op", "batch" })) return error.InvalidRequest;
        const old = self.gen.?;
        const fresh = try applyBatch(&self.counters, ra, old, obj.get("batch").?);
        old.destroy();
        self.gen = fresh;
        self.generation += 1;
        var s = Sink.init(ra, false);
        try head(&s, id, self.generation);
        try s.raw(",\"ok\":true}");
        try self.writeLine(s.buf.items);
    }

    fn doDigest(self: *Session, ra: A, id: u64, obj: ObjectMap) !void {
        if (!exactKeys(obj, &.{ "id", "op" })) return error.InvalidRequest;
        var timer = try std.time.Timer.start();
        const d = try stateDigest(ra, self.gen.?);
        const ns = timer.read();
        var sorted = Sink.init(ra, false);
        try sorted.raw("{\"bytes_processed\":");
        try sorted.num(d.bytes);
        try sorted.raw(",\"generation\":");
        try sorted.num(self.generation);
        try sorted.raw(",\"id\":");
        try sorted.num(id);
        try sorted.raw(",\"ok\":true,\"state_digest\":");
        try sorted.str(try std.fmt.allocPrint(ra, "sha256:{s}", .{std.fmt.fmtSliceHexLower(&d.digest)}));
        try sorted.raw(",\"state_digest_ms\":");
        try sorted.raw(try std.fmt.allocPrint(ra, "{d:.6}", .{@as(f64, @floatFromInt(ns)) / 1e6}));
        try sorted.raw("}");
        try self.writeLine(sorted.buf.items);
    }

    fn snapshotPath(ra: A, id: []const u8) ![]const u8 {
        return std.fmt.allocPrint(ra, "{s}.snap", .{id});
    }

    fn doSnapshot(self: *Session, ra: A, id: u64, obj: ObjectMap) !void {
        if (!exactKeys(obj, &.{ "id", "op" })) return error.InvalidRequest;
        const data = try encodeSnapshot(ra, self.gen.?);
        var dir = std.fs.cwd().makeOpenPath(self.repo, .{}) catch return error.InvalidState;
        defer dir.close();
        self.snapshots += 1;
        const snap_id = try std.fmt.allocPrint(ra, "zs{x}-{d}", .{ @as(u64, @intCast(std.time.nanoTimestamp() & 0xffffffffffff)), self.snapshots });
        const file = dir.createFile(try snapshotPath(ra, snap_id), .{}) catch return error.OutOfMemory;
        defer file.close();
        file.writeAll(data) catch return error.OutOfMemory;
        var s = Sink.init(ra, false);
        try s.raw("{\"captured_generation\":");
        try s.num(self.generation);
        try s.raw(",\"generation\":");
        try s.num(self.generation);
        try s.raw(",\"id\":");
        try s.num(id);
        try s.raw(",\"ok\":true,\"snapshot_id\":");
        try s.str(snap_id);
        try s.raw("}");
        try self.writeLine(s.buf.items);
    }

    fn doRestore(self: *Session, ra: A, id: u64, obj: ObjectMap) !void {
        if (!exactKeys(obj, &.{ "id", "op", "snapshot_id" })) return error.InvalidRequest;
        const sid = obj.get("snapshot_id").?;
        if (sid != .string) return error.InvalidRequest;
        if (sid.string.len == 0 or sid.string.len > 64) return error.InvalidInput;
        for (sid.string) |c| if (!(std.ascii.isAlphanumeric(c) or c == '-' or c == '_')) return error.InvalidInput;
        var dir = std.fs.cwd().openDir(self.repo, .{}) catch return error.InvalidInput;
        defer dir.close();
        const data = dir.readFileAlloc(ra, try snapshotPath(ra, sid.string), std.math.maxInt(usize)) catch return error.InvalidInput;
        const old = self.gen.?;
        const fresh = try decodeSnapshot(&self.counters, ra, data, old.ctx);
        old.destroy();
        self.gen = fresh;
        self.generation += 1;
        var s = Sink.init(ra, false);
        try head(&s, id, self.generation);
        try s.raw(",\"ok\":true}");
        try self.writeLine(s.buf.items);
    }

    fn doStats(self: *Session, ra: A, id: u64, obj: ObjectMap) !void {
        if (!exactKeys(obj, &.{ "id", "op" })) return error.InvalidRequest;
        const g = self.gen.?;
        var s = Sink.init(ra, false);
        try head(&s, id, self.generation);
        try s.raw(",\"ok\":true,\"stats\":{\"allocations_total\":");
        try s.num(self.counters.allocations);
        try s.raw(",\"entities\":");
        try s.num(g.fx.entities.len);
        try s.raw(",\"evidence\":");
        try s.num(g.fx.evidence.len);
        try s.raw(",\"generation\":");
        try s.num(self.generation);
        try s.raw(",\"heap_breakdown\":null,\"live_heap_bytes\":");
        try s.num(self.counters.live);
        try s.raw(",\"peak_heap_bytes\":");
        try s.num(self.counters.peak_live);
        try s.raw(",\"relations\":");
        try s.num(g.fx.relations.len);
        try s.raw(",\"schema\":\"csl.eval.session.stats/v0.1\",\"unique_strings\":");
        try s.num(g.store.name_cache.?.unique());
        try s.raw("}}");
        try self.writeLine(s.buf.items);
    }
};

/// Entry point for `csl-eval-zig session --repository DIR`.
pub fn serve(repository: []const u8) void {
    var session = Session{
        .repo = repository,
        .out = .{ .unbuffered_writer = std.io.getStdOut().writer() },
        .pending_arena = std.heap.ArenaAllocator.init(std.heap.page_allocator),
    };
    const stdin = std.io.getStdIn().reader();
    while (true) {
        var request = std.heap.ArenaAllocator.init(std.heap.page_allocator);
        defer request.deinit();
        const ra = request.allocator();
        const line = stdin.readUntilDelimiterOrEofAlloc(ra, '\n', std.math.maxInt(usize)) catch {
            std.process.exit(1);
        } orelse {
            session.flush();
            std.process.exit(0);
        };
        session.handleLine(ra, line) catch std.process.exit(1);
        session.flush();
    }
}

test "canonical string escaping matches json.dumps(ensure_ascii=False)" {
    var s = Sink.init(std.testing.allocator, false);
    defer s.buf.deinit();
    try s.str("a\"b\\c\x08\x0c\n\r\t\x01\x1f\x7f\u{e9}\u{1F600}");
    try std.testing.expectEqualStrings("\"a\\\"b\\\\c\\b\\f\\n\\r\\t\\u0001\\u001f\x7f\u{e9}\u{1F600}\"", s.buf.items);
}

test "chunk size keeps a base64 chunk line within the negotiated line length" {
    var session: Session = undefined;
    session.max_line = MIN_LINE;
    const raw = session.chunkBytes();
    try std.testing.expect(((raw + 2) / 3) * 4 + FRAME_OVERHEAD <= MIN_LINE);
}
