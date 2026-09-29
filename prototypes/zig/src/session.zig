//! S0 v0 session for the Zig candidate (oracle/SESSION-SEMANTICS.md, SESSION-BINDING-JSONL.md,
//! adr/0008-mutation-semantics.md).
//!
//! Layers
//! * `Engine` (public, reusable): all S0 semantics, independent of stdin/stdout and of chunk framing.
//!   `Engine.init(repository, strategy)`, `Engine.handle(request_arena, request_line) Reply`,
//!   `Engine.deinit()`. A front end (this file's stdin/stdout `serve`, or the persistent Hybrid
//!   boundary) passes one complete request line (no trailing newline) and gets a `Reply` with the
//!   complete canonical single-line response (`line`), or for a query result too large for one line
//!   (`chunked`) the canonical result `payload` plus the envelope fields (`generation`,
//!   `service_ns`) needed to emit `begin/chunk/end` frames itself.
//! * `serve` (public): the pure-Zig driver: reads JSONL, reassembles chunked requests, writes replies,
//!   chunks large results.
//!
//! Mutation strategies (`--strategy`, reported at `open`)
//! * `full-rebuild` (default): one `Gen` per generation (own arena, counting allocator, logical rows
//!   as a `sem.Fixture`, the existing `sem.Store`). `mutate` validates and applies the batch to
//!   multiset working maps, then rebuilds every derived structure into a new `Gen`.
//! * `incremental`: one long-lived `Inc` store. Entities live in a hash map with reference counters
//!   (`refs` = relation and evidence endpoints plus containers pointing at the entity); outgoing and
//!   incoming adjacency lists are kept sorted (binary-search insert/remove of one occurrence);
//!   evidence lives in slots (tombstones plus a free list) indexed by row; the name index is updated in
//!   place. A batch is parsed, validated against an overlay sized by the batch (existence,
//!   multiplicities, reference-count deltas), and only then applied, so a rejected batch touches
//!   nothing. Queries reuse `sem.Store` over these structures (`Store.slots` set), so results are
//!   byte-identical to the full-rebuild path. No mutate ever rebuilds; `derived_rebuilds_total`
//!   counts only the bulk builds at `open` and `restore`.
//!
//! Snapshot file layout (little endian), named `<repository>/<snapshot_id>.snap` (same for both
//! strategies, so a snapshot restores under either):
//!     magic "CSLZSNAP" | u32 version(1) | u32 len + artifact | u32 len + context.snapshot |
//!     u8 epoch_present | u64 epoch | u8 complete | u64 entities | u64 relations | u64 evidence |
//!     entities: (u64 id, u8 kind, u8 has_container, u64 container, u32 len + name)* |
//!     relations: (u64 subject, u8 relation, u64 object)* |
//!     evidence: (u64 proposition, u64 subject, u8 relation, u64 object, u8 polarity, u8 quality,
//!                u64 freshness_epoch, u32 lineage)* | sha256 of everything before it.
const std = @import("std");
/// Re-exported so a build that imports this file as a module uses the same `semantic.zig` types
/// (a file may belong to only one module: do not also declare a separate `semantic` module for it).
pub const sem = @import("semantic.zig");
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
const Counters = struct { allocations: u64 = 0, total_requested: u64 = 0, total_freed: u64 = 0, live: u64 = 0, peak_live: u64 = 0, rebuilds: u64 = 0 };

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
        self.counting.counters.rebuilds += 1;
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

// ---- logical rows --------------------------------------------------------------------------
/// A strategy-independent view of the logical state, used by the digest and by snapshots.
const Rows = struct { ctx: Context, ents: []const LEnt, rels: []const sem.Edge, evs: []const sem.Evidence };

fn genRows(ra: A, g: *const Gen) !Rows {
    const ents = try ra.alloc(LEnt, g.fx.entities.len);
    for (g.fx.entities, 0..) |e, i| ents[i] = .{ .id = e.id, .kind = e.kind, .name = g.entityName(e), .container = e.container };
    return .{ .ctx = g.ctx, .ents = ents, .rels = g.fx.relations, .evs = g.fx.evidence };
}

const Counts = struct { entities: u64, relations: u64, evidence: u64, unique: u64 };

// ---- incremental store -----------------------------------------------------------------------
const IEnt = struct { kind: sem.Kind, name: []u8, container: ?u64, refs: u32 };
const EdgeList = std.ArrayList(sem.Edge);

fn edgeLower(list: []const sem.Edge, e: sem.Edge) usize {
    var lo: usize = 0;
    var hi: usize = list.len;
    while (lo < hi) {
        const mid = lo + (hi - lo) / 2;
        if (sem.edgeLess({}, list[mid], e)) lo = mid + 1 else hi = mid;
    }
    return lo;
}
fn edgeEq(a: sem.Edge, b: sem.Edge) bool {
    return a.subject == b.subject and a.relation == b.relation and a.object == b.object;
}
fn edgeCount(list: []const sem.Edge, e: sem.Edge) u32 {
    var i = edgeLower(list, e);
    var n: u32 = 0;
    while (i < list.len and edgeEq(list[i], e)) : (i += 1) n += 1;
    return n;
}
fn idLower(list: []const u64, id: u64) usize {
    var lo: usize = 0;
    var hi: usize = list.len;
    while (lo < hi) {
        const mid = lo + (hi - lo) / 2;
        if (list[mid] < id) lo = mid + 1 else hi = mid;
    }
    return lo;
}
fn addRef(m: *std.AutoHashMap(u64, i64), id: u64, delta: i64) Err!void {
    const g = m.getOrPut(id) catch return error.OutOfMemory;
    if (!g.found_existing) g.value_ptr.* = 0;
    g.value_ptr.* += delta;
}
/// Entity state after a batch, for the ids the batch touches (the validation overlay).
const Ov = struct { present: bool, existed: bool, kind: sem.Kind, name: []const u8, container: ?u64 };

/// The `incremental` strategy: one long-lived store updated in place (see the module comment).
const Inc = struct {
    counting: GenAlloc,
    ctx: Context,
    ents: std.AutoHashMap(u64, IEnt),
    store: sem.Store,
    slots: std.ArrayList(sem.EvSlot),
    ev_index: std.AutoHashMap(sem.Evidence, std.ArrayList(u32)),
    free_slots: std.ArrayList(u32),
    rel_count: u64,
    ev_live: u64,

    fn alloc(self: *Inc) A {
        return self.counting.allocator();
    }

    /// Bulk build (open, restore): counts as one derived rebuild.
    fn fromRows(counters: *Counters, ctx: Context, ents: []const LEnt, rels: []const sem.Edge, evs: []const sem.Evidence) Err!*Inc {
        const self = std.heap.c_allocator.create(Inc) catch return error.OutOfMemory;
        self.counting = .{ .child = std.heap.c_allocator, .counters = counters };
        const a = self.alloc();
        const cache = sem.NameCache.create(a) catch {
            std.heap.c_allocator.destroy(self);
            return error.OutOfMemory;
        };
        cache.built = true;
        self.ctx = .{ .snapshot = &.{}, .epoch = ctx.epoch, .complete = ctx.complete };
        self.ents = std.AutoHashMap(u64, IEnt).init(a);
        self.slots = std.ArrayList(sem.EvSlot).init(a);
        self.ev_index = std.AutoHashMap(sem.Evidence, std.ArrayList(u32)).init(a);
        self.free_slots = std.ArrayList(u32).init(a);
        self.rel_count = 0;
        self.ev_live = 0;
        self.store = .{
            .a = a,
            .fx = .{ .schema = "csl.eval.fixture/v0.1", .snapshot = "", .epoch = ctx.epoch orelse 0, .strings = &.{}, .entities = &.{}, .relations = &.{}, .evidence = &.{}, .complete = ctx.complete },
            .entities = std.AutoHashMap(u64, sem.Entity).init(a),
            .out = sem.Index.init(a),
            .inc = sem.Index.init(a),
            .name_cache = cache,
        };
        errdefer self.destroy();
        self.ctx.snapshot = a.dupe(u8, ctx.snapshot) catch return error.OutOfMemory;
        self.store.fx.snapshot = self.ctx.snapshot;
        self.ents.ensureTotalCapacity(@intCast(ents.len)) catch return error.OutOfMemory;
        for (ents) |e| {
            if (e.id == 0) return error.InvalidInput;
            const gop = self.ents.getOrPut(e.id) catch return error.OutOfMemory;
            if (gop.found_existing) return error.InvalidInput;
            gop.value_ptr.* = .{ .kind = e.kind, .name = &.{}, .container = e.container, .refs = 0 };
            gop.value_ptr.name = a.dupe(u8, e.name) catch return error.OutOfMemory;
            self.store.entities.put(e.id, .{ .id = e.id, .kind = e.kind, .name_sid = 0, .container = e.container }) catch return error.OutOfMemory;
        }
        for (ents) |e| if (e.container) |c| {
            const p = self.ents.getPtr(c) orelse return error.InvalidInput;
            p.refs += 1;
        };
        for (rels) |r| {
            const sp = self.ents.getPtr(r.subject) orelse return error.InvalidInput;
            sp.refs += 1;
            const op = self.ents.getPtr(r.object) orelse return error.InvalidInput;
            op.refs += 1;
            for ([_]*sem.Index{ &self.store.out, &self.store.inc }, [_]u64{ r.subject, r.object }) |idx, id| {
                const gop = idx.getOrPut(id) catch return error.OutOfMemory;
                if (!gop.found_existing) gop.value_ptr.* = EdgeList.init(a);
                gop.value_ptr.append(r) catch return error.OutOfMemory;
            }
        }
        self.rel_count = rels.len;
        sem.Store.sortAdjacency(&self.store.out, &self.store.inc);
        self.slots.ensureTotalCapacity(evs.len) catch return error.OutOfMemory;
        for (evs, 0..) |r, i| {
            if (r.proposition == 0) return error.InvalidInput;
            const sp = self.ents.getPtr(r.subject) orelse return error.InvalidInput;
            sp.refs += 1;
            const op = self.ents.getPtr(r.object) orelse return error.InvalidInput;
            op.refs += 1;
            self.slots.appendAssumeCapacity(.{ .row = r, .live = true });
            const gop = self.ev_index.getOrPut(r) catch return error.OutOfMemory;
            if (!gop.found_existing) gop.value_ptr.* = std.ArrayList(u32).init(a);
            gop.value_ptr.append(@intCast(i)) catch return error.OutOfMemory;
        }
        self.ev_live = evs.len;
        var it = self.ents.iterator();
        while (it.next()) |kv| self.nameAppend(kv.value_ptr.name, kv.key_ptr.*) catch return error.OutOfMemory;
        var lists = cache.map.valueIterator();
        while (lists.next()) |list| std.mem.sort(u64, list.items, {}, std.sort.asc(u64));
        self.store.slots = self.slots.items;
        counters.rebuilds += 1;
        return self;
    }

    fn destroy(self: *Inc) void {
        const a = self.alloc();
        var eit = self.ents.valueIterator();
        while (eit.next()) |e| a.free(e.name);
        self.ents.deinit();
        self.store.entities.deinit();
        inline for (.{ &self.store.out, &self.store.inc }) |idx| {
            var it = idx.valueIterator();
            while (it.next()) |list| list.deinit();
            idx.deinit();
        }
        if (self.store.name_cache) |cache| {
            var it = cache.map.iterator();
            while (it.next()) |kv| {
                a.free(kv.key_ptr.*);
                kv.value_ptr.deinit();
            }
            cache.map.deinit();
            a.destroy(cache);
        }
        self.slots.deinit();
        var vit = self.ev_index.valueIterator();
        while (vit.next()) |list| list.deinit();
        self.ev_index.deinit();
        self.free_slots.deinit();
        a.free(self.ctx.snapshot);
        std.heap.c_allocator.destroy(self);
    }

    /// Bulk append (unsorted) into the name index; the caller sorts once afterwards.
    fn nameAppend(self: *Inc, name: []const u8, id: u64) !void {
        const a = self.alloc();
        const map = &self.store.name_cache.?.map;
        const gop = try map.getOrPut(name);
        if (!gop.found_existing) {
            gop.key_ptr.* = try a.dupe(u8, name);
            gop.value_ptr.* = std.ArrayList(u64).init(a);
        }
        try gop.value_ptr.append(id);
    }
    fn nameAdd(self: *Inc, name: []const u8, id: u64) !void {
        const a = self.alloc();
        const map = &self.store.name_cache.?.map;
        const gop = try map.getOrPut(name);
        if (!gop.found_existing) {
            gop.key_ptr.* = try a.dupe(u8, name);
            gop.value_ptr.* = std.ArrayList(u64).init(a);
        }
        try gop.value_ptr.insert(idLower(gop.value_ptr.items, id), id);
    }
    fn nameRemove(self: *Inc, name: []const u8, id: u64) void {
        const a = self.alloc();
        const map = &self.store.name_cache.?.map;
        const list = map.getPtr(name) orelse return;
        const i = idLower(list.items, id);
        if (i < list.items.len and list.items[i] == id) _ = list.orderedRemove(i);
        if (list.items.len == 0) {
            if (map.fetchRemove(name)) |kv| {
                a.free(kv.key);
                var l = kv.value;
                l.deinit();
            }
        }
    }

    fn view(self: *Inc, ra: A) sem.Store {
        var s = self.store;
        s.a = ra;
        s.slots = self.slots.items;
        return s;
    }

    fn rows(self: *Inc, ra: A) !Rows {
        const ents = try ra.alloc(LEnt, self.ents.count());
        var i: usize = 0;
        var it = self.ents.iterator();
        while (it.next()) |kv| : (i += 1) ents[i] = .{ .id = kv.key_ptr.*, .kind = kv.value_ptr.kind, .name = kv.value_ptr.name, .container = kv.value_ptr.container };
        var rels = try std.ArrayList(sem.Edge).initCapacity(ra, @intCast(self.rel_count));
        var oit = self.store.out.valueIterator();
        while (oit.next()) |list| try rels.appendSlice(list.items);
        var evs = try std.ArrayList(sem.Evidence).initCapacity(ra, @intCast(self.ev_live));
        for (self.slots.items) |s| if (s.live) try evs.append(s.row);
        return .{ .ctx = self.ctx, .ents = ents, .rels = rels.items, .evs = evs.items };
    }

    fn existsFinal(self: *const Inc, ov: *const std.AutoHashMap(u64, Ov), id: u64) bool {
        if (ov.get(id)) |o| return o.present;
        return self.ents.contains(id);
    }

    /// Validate `batch` against an overlay sized by the batch and, only if valid, apply it in place.
    /// Every failure is raised before the first structural change, so a rejected batch touches nothing.
    fn applyBatch(self: *Inc, ra: A, batch: Value) Err!void {
        var p = try parseBatch(ra, batch);
        var ov = std.AutoHashMap(u64, Ov).init(ra);
        var dref = std.AutoHashMap(u64, i64).init(ra);
        for (p.entity_ops.items) |op| {
            const o = op.obj;
            switch (op.kind) {
                .ADD_ENTITY => {
                    const id = if (op.id) |x| (if (x >= 1) x else return error.InvalidInput) else return error.InvalidInput;
                    const kind = asEnum(sem.Kind, o.get("kind").?) orelse return error.InvalidInput;
                    var container_ok = true;
                    const container = asContainer(o.get("container"), &container_ok);
                    if (!container_ok or o.get("name").? != .string) return error.InvalidInput;
                    if (self.ents.contains(id)) return error.InvalidInput;
                    ov.put(id, .{ .present = true, .existed = false, .kind = kind, .name = o.get("name").?.string, .container = container }) catch return error.OutOfMemory;
                    if (container) |c| try addRef(&dref, c, 1);
                },
                .REMOVE_ENTITY => {
                    const id = op.id orelse return error.InvalidInput;
                    const old = self.ents.get(id) orelse return error.InvalidInput;
                    ov.put(id, .{ .present = false, .existed = true, .kind = old.kind, .name = old.name, .container = old.container }) catch return error.OutOfMemory;
                    if (old.container) |c| try addRef(&dref, c, -1);
                },
                .UPDATE_ENTITY => {
                    const id = op.id orelse return error.InvalidInput;
                    const old = self.ents.get(id) orelse return error.InvalidInput;
                    const changes = o.get("set").?.object;
                    if (changes.count() == 0 or !onlyKeys(changes, &.{ "kind", "name", "container" })) return error.InvalidInput;
                    var merged: Ov = .{ .present = true, .existed = true, .kind = old.kind, .name = old.name, .container = old.container };
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
                    ov.put(id, merged) catch return error.OutOfMemory;
                    if (old.container != merged.container) {
                        if (old.container) |c| try addRef(&dref, c, -1);
                        if (merged.container) |c| try addRef(&dref, c, 1);
                    }
                },
                else => unreachable,
            }
        }
        // Multiplicities are checked against the pre-batch state.
        var rit = p.rel_rem.iterator();
        while (rit.next()) |e| {
            const have: u32 = if (self.store.out.get(e.key_ptr.subject)) |l| edgeCount(l.items, e.key_ptr.*) else 0;
            if (have < e.value_ptr.*) return error.InvalidInput;
        }
        var vit = p.ev_rem.iterator();
        while (vit.next()) |e| {
            const have: u32 = if (self.ev_index.get(e.key_ptr.*)) |l| @intCast(l.items.len) else 0;
            if (have < e.value_ptr.*) return error.InvalidInput;
        }
        // Reference-count deltas and final-state existence of every endpoint the batch adds.
        rit = p.rel_rem.iterator();
        while (rit.next()) |e| {
            try addRef(&dref, e.key_ptr.subject, -@as(i64, e.value_ptr.*));
            try addRef(&dref, e.key_ptr.object, -@as(i64, e.value_ptr.*));
        }
        rit = p.rel_add.iterator();
        while (rit.next()) |e| {
            if (!self.existsFinal(&ov, e.key_ptr.subject) or !self.existsFinal(&ov, e.key_ptr.object)) return error.InvalidInput;
            try addRef(&dref, e.key_ptr.subject, @as(i64, e.value_ptr.*));
            try addRef(&dref, e.key_ptr.object, @as(i64, e.value_ptr.*));
        }
        vit = p.ev_rem.iterator();
        while (vit.next()) |e| {
            try addRef(&dref, e.key_ptr.subject, -@as(i64, e.value_ptr.*));
            try addRef(&dref, e.key_ptr.object, -@as(i64, e.value_ptr.*));
        }
        vit = p.ev_add.iterator();
        while (vit.next()) |e| {
            if (!self.existsFinal(&ov, e.key_ptr.subject) or !self.existsFinal(&ov, e.key_ptr.object)) return error.InvalidInput;
            try addRef(&dref, e.key_ptr.subject, @as(i64, e.value_ptr.*));
            try addRef(&dref, e.key_ptr.object, @as(i64, e.value_ptr.*));
        }
        var oit = ov.iterator();
        while (oit.next()) |kv| {
            const o = kv.value_ptr.*;
            if (o.present) {
                if (o.container) |c| if (!self.existsFinal(&ov, c)) return error.InvalidInput;
            } else {
                const final = @as(i64, self.ents.get(kv.key_ptr.*).?.refs) + (dref.get(kv.key_ptr.*) orelse 0);
                if (final != 0) return error.InvalidInput;
            }
        }
        // ---- valid: apply in place (only allocation failure can interrupt from here on) ----
        try self.apply(&p, &ov, &dref);
    }

    fn apply(self: *Inc, p: *Parsed, ov: *std.AutoHashMap(u64, Ov), dref: *std.AutoHashMap(u64, i64)) Err!void {
        const a = self.alloc();
        var oit = ov.iterator();
        while (oit.next()) |kv| {
            const id = kv.key_ptr.*;
            const o = kv.value_ptr.*;
            if (!o.present) {
                const old = self.ents.get(id).?;
                self.nameRemove(old.name, id);
                _ = self.store.entities.remove(id);
                a.free(old.name);
                _ = self.ents.remove(id);
            } else if (!o.existed) {
                const name = a.dupe(u8, o.name) catch return error.OutOfMemory;
                self.ents.put(id, .{ .kind = o.kind, .name = name, .container = o.container, .refs = 0 }) catch return error.OutOfMemory;
                self.store.entities.put(id, .{ .id = id, .kind = o.kind, .name_sid = 0, .container = o.container }) catch return error.OutOfMemory;
                self.nameAdd(name, id) catch return error.OutOfMemory;
            } else {
                const e = self.ents.getPtr(id).?;
                const se = self.store.entities.getPtr(id).?;
                e.kind = o.kind;
                se.kind = o.kind;
                e.container = o.container;
                se.container = o.container;
                if (!std.mem.eql(u8, e.name, o.name)) {
                    self.nameRemove(e.name, id);
                    const name = a.dupe(u8, o.name) catch return error.OutOfMemory;
                    a.free(e.name);
                    e.name = name;
                    self.nameAdd(name, id) catch return error.OutOfMemory;
                }
            }
        }
        var rit = p.rel_rem.iterator();
        while (rit.next()) |kv| {
            const e = kv.key_ptr.*;
            var n = kv.value_ptr.*;
            while (n > 0) : (n -= 1) {
                removeEdge(&self.store.out, e.subject, e);
                removeEdge(&self.store.inc, e.object, e);
            }
            self.rel_count -= kv.value_ptr.*;
        }
        rit = p.rel_add.iterator();
        while (rit.next()) |kv| {
            const e = kv.key_ptr.*;
            var n = kv.value_ptr.*;
            while (n > 0) : (n -= 1) {
                try self.insertEdge(&self.store.out, e.subject, e);
                try self.insertEdge(&self.store.inc, e.object, e);
            }
            self.rel_count += kv.value_ptr.*;
        }
        var vit = p.ev_rem.iterator();
        while (vit.next()) |kv| {
            const list = self.ev_index.getPtr(kv.key_ptr.*).?;
            var n = kv.value_ptr.*;
            while (n > 0) : (n -= 1) {
                const pos = list.pop().?;
                self.slots.items[pos].live = false;
                self.free_slots.append(pos) catch return error.OutOfMemory;
            }
            self.ev_live -= kv.value_ptr.*;
            if (list.items.len == 0) {
                list.deinit();
                _ = self.ev_index.remove(kv.key_ptr.*);
            }
        }
        vit = p.ev_add.iterator();
        while (vit.next()) |kv| {
            const gop = self.ev_index.getOrPut(kv.key_ptr.*) catch return error.OutOfMemory;
            if (!gop.found_existing) gop.value_ptr.* = std.ArrayList(u32).init(a);
            var n = kv.value_ptr.*;
            while (n > 0) : (n -= 1) {
                const slot: sem.EvSlot = .{ .row = kv.key_ptr.*, .live = true };
                const pos: u32 = if (self.free_slots.pop()) |free_pos| free_pos else blk: {
                    self.slots.append(slot) catch return error.OutOfMemory;
                    break :blk @intCast(self.slots.items.len - 1);
                };
                self.slots.items[pos] = slot;
                gop.value_ptr.append(pos) catch return error.OutOfMemory;
            }
            self.ev_live += kv.value_ptr.*;
        }
        var dit = dref.iterator();
        while (dit.next()) |kv| {
            if (self.ents.getPtr(kv.key_ptr.*)) |e| e.refs = @intCast(@as(i64, e.refs) + kv.value_ptr.*);
        }
        self.store.slots = self.slots.items;
    }

    fn insertEdge(self: *Inc, idx: *sem.Index, id: u64, e: sem.Edge) Err!void {
        const gop = idx.getOrPut(id) catch return error.OutOfMemory;
        if (!gop.found_existing) gop.value_ptr.* = EdgeList.init(self.alloc());
        gop.value_ptr.insert(edgeLower(gop.value_ptr.items, e), e) catch return error.OutOfMemory;
    }
};

fn removeEdge(idx: *sem.Index, id: u64, e: sem.Edge) void {
    const list = idx.getPtr(id).?;
    const i = edgeLower(list.items, e);
    std.debug.assert(i < list.items.len and edgeEq(list.items[i], e));
    _ = list.orderedRemove(i);
    if (list.items.len == 0) {
        list.deinit();
        _ = idx.remove(id);
    }
}

// ---- backend: the two strategies behind one interface -----------------------------------------
const Backend = union(enum) {
    full: *Gen,
    inc: *Inc,

    fn ctx(self: Backend) Context {
        return switch (self) {
            .full => |g| g.ctx,
            .inc => |i| i.ctx,
        };
    }
    fn counts(self: Backend) Counts {
        return switch (self) {
            .full => |g| .{ .entities = g.fx.entities.len, .relations = g.fx.relations.len, .evidence = g.fx.evidence.len, .unique = g.store.name_cache.?.unique() },
            .inc => |i| .{ .entities = i.ents.count(), .relations = i.rel_count, .evidence = i.ev_live, .unique = i.store.name_cache.?.unique() },
        };
    }
    fn rows(self: Backend, ra: A) !Rows {
        return switch (self) {
            .full => |g| genRows(ra, g),
            .inc => |i| i.rows(ra),
        };
    }
    /// A query view whose scratch allocations come from the request arena.
    fn view(self: Backend, ra: A) sem.Store {
        return switch (self) {
            .full => |g| blk: {
                var s = g.store;
                s.a = ra;
                break :blk s;
            },
            .inc => |i| i.view(ra),
        };
    }
    fn destroy(self: Backend) void {
        switch (self) {
            .full => |g| g.destroy(),
            .inc => |i| i.destroy(),
        }
    }
};

/// Mutation strategy of a session (`session --strategy`), reported by `open`.
pub const Strategy = enum {
    @"full-rebuild",
    incremental,
    pub fn parse(text: []const u8) ?Strategy {
        return std.meta.stringToEnum(Strategy, text);
    }
};

fn buildBackend(strategy: Strategy, counters: *Counters, ctx: Context, ents: []const LEnt, rels: []const sem.Edge, evs: []const sem.Evidence) Err!Backend {
    return switch (strategy) {
        .@"full-rebuild" => .{ .full = try genFromRows(counters, ctx, ents, rels, evs) },
        .incremental => .{ .inc = try Inc.fromRows(counters, ctx, ents, rels, evs) },
    };
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

const Parsed = struct { entity_ops: std.ArrayList(EntityOp), rel_add: RelMap, rel_rem: RelMap, ev_add: EvMap, ev_rem: EvMap };

/// Validate `batch` per ADR-0008 (same order of checks as oracle/session_model.py) and return the new
/// generation, or an error leaving `cur` untouched. `ra` holds scratch data for this request.
fn parseBatch(ra: A, batch: Value) Err!Parsed {
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
    return .{ .entity_ops = entity_ops, .rel_add = rel_add, .rel_rem = rel_rem, .ev_add = ev_add, .ev_rem = ev_rem };
}

/// Full-rebuild strategy: validate `batch` and return the new generation, or an error leaving `cur` untouched.
fn applyFull(counters: *Counters, ra: A, cur: *const Gen, batch: Value) Err!*Gen {
    const parsed = try parseBatch(ra, batch);
    var entity_ops = parsed.entity_ops;
    var rel_add = parsed.rel_add;
    var rel_rem = parsed.rel_rem;
    var ev_add = parsed.ev_add;
    var ev_rem = parsed.ev_rem;
    _ = .{ &entity_ops, &rel_add, &ev_add };

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
fn lentLess(_: void, a: LEnt, b: LEnt) bool {
    return a.id < b.id;
}

/// SHA-256 over the canonical logical state (SESSION-SEMANTICS.md section 2).
fn stateDigest(ra: A, rows: Rows) !struct { digest: [32]u8, bytes: u64 } {
    const ents = try ra.dupe(LEnt, rows.ents);
    std.mem.sort(LEnt, ents, {}, lentLess);
    const rels = try ra.dupe(sem.Edge, rows.rels);
    std.mem.sort(sem.Edge, rels, {}, sem.edgeLess);
    const evs = try ra.dupe(sem.Evidence, rows.evs);
    std.mem.sort(sem.Evidence, evs, {}, sem.evLess);
    var s = Sink.init(ra, true);
    try s.raw("{\"complete\":");
    try s.raw(if (rows.ctx.complete) "true" else "false");
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
        try s.str(e.name);
        try s.raw("}");
    }
    try s.raw("],\"epoch\":");
    try s.optNum(rows.ctx.epoch);
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
    try s.str(rows.ctx.snapshot);
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

fn encodeSnapshot(ra: A, rows: Rows, artifact: []const u8) ![]u8 {
    var out = std.ArrayList(u8).init(ra);
    try out.appendSlice(SNAP_MAGIC);
    try putInt(&out, u32, SNAP_VERSION);
    try putBytes(&out, artifact);
    try putBytes(&out, rows.ctx.snapshot);
    try out.append(if (rows.ctx.epoch != null) 1 else 0);
    try putInt(&out, u64, rows.ctx.epoch orelse 0);
    try out.append(if (rows.ctx.complete) 1 else 0);
    try putInt(&out, u64, rows.ents.len);
    try putInt(&out, u64, rows.rels.len);
    try putInt(&out, u64, rows.evs.len);
    for (rows.ents) |e| {
        try putInt(&out, u64, e.id);
        try out.append(@intFromEnum(e.kind));
        try out.append(if (e.container != null) 1 else 0);
        try putInt(&out, u64, e.container orelse 0);
        try putBytes(&out, e.name);
    }
    for (rows.rels) |r| {
        try putInt(&out, u64, r.subject);
        try out.append(@intFromEnum(r.relation));
        try putInt(&out, u64, r.object);
    }
    for (rows.evs) |r| {
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

/// Decode a snapshot into logical rows (the caller builds the store); rejects a snapshot whose
/// artifact or context differs from the session's.
fn decodeSnapshot(ra: A, data: []const u8, ctx: Context, artifact: []const u8) Err!Rows {
    if (data.len < SNAP_MAGIC.len + 32) return error.InvalidInput;
    var hash: [32]u8 = undefined;
    Sha256.hash(data[0 .. data.len - 32], &hash, .{});
    if (!std.mem.eql(u8, &hash, data[data.len - 32 ..])) return error.InvalidInput;
    var r = Reader{ .data = data[0 .. data.len - 32] };
    if (!std.mem.eql(u8, try r.take(SNAP_MAGIC.len), SNAP_MAGIC)) return error.InvalidInput;
    if (try r.int(u32) != SNAP_VERSION) return error.InvalidInput;
    if (!std.mem.eql(u8, try r.bytes(), artifact)) return error.InvalidInput;
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
    return .{ .ctx = ctx, .ents = ents, .rels = rels, .evs = evs };
}

// ---- the engine: all S0 semantics, independent of stdin/stdout and chunk framing ----------------
const State = enum { NEW, OPEN, FAILED };
const Op = enum { open, query, mutate, state_digest, snapshot, restore, cancel, stats, close };

/// The outcome of one request. `line` is the complete canonical single-line response (without the
/// trailing newline). When `chunked` is set the result was too large for one line: `line` is empty and
/// the front end frames `payload` (the canonical result bytes) as begin/chunk/end, using `id`,
/// `generation` and `service_ns` for the `end` frame. `close` asks the front end to exit after writing.
pub const Reply = struct {
    ok: bool,
    id: ?u64 = null,
    generation: ?u64 = null,
    service_ns: u64 = 0,
    line: []const u8 = "",
    payload: ?[]const u8 = null,
    chunked: bool = false,
    close: bool = false,
};

fn isUserError(e: anyerror) bool {
    return e == error.InvalidRequest or e == error.InvalidState or e == error.InvalidInput or e == error.Unsupported or e == error.OutOfMemory;
}

/// A canonical error response line.
pub fn errorLine(ra: A, id: ?u64, code: []const u8, msg: []const u8, service_ns: u64) ![]const u8 {
    var s = Sink.init(ra, false);
    try s.raw("{\"code\":");
    try s.str(code);
    try s.raw(",\"id\":");
    try s.optNum(id);
    try s.raw(",\"message\":");
    try s.str(msg);
    try s.raw(",\"ok\":false,\"service_ns\":");
    try s.num(service_ns);
    try s.raw("}");
    return s.buf.items;
}

/// Module entry points for another front end (the persistent Hybrid boundary):
///   `Engine.init(repository, strategy)`; `engine.handle(request_arena, request_line) Reply`;
///   `engine.chunkBytes()` / `engine.max_line` for framing; `engine.deinit()`.
/// `request_arena` owns every byte the reply points to; free it after the reply has been written.
/// `handle` never returns an error: failures are encoded in `Reply.line` as S0 error responses.
pub const Engine = struct {
    repo: []const u8,
    strategy: Strategy,
    state: State = .NEW,
    backend: ?Backend = null,
    generation: u64 = 0,
    counters: Counters = .{},
    max_line: u64 = MIN_LINE,
    snapshots: u64 = 0,
    timer: std.time.Timer = undefined,
    cur_id: ?u64 = null,
    /// Build identifier reported at `open` and stamped into / required from snapshots. A front end whose
    /// snapshots must not be interchangeable with the pure-Zig candidate's (the Hybrid) overrides it.
    artifact: []const u8 = ARTIFACT,

    pub fn init(repository: []const u8, strategy: Strategy) Engine {
        return .{ .repo = repository, .strategy = strategy };
    }
    pub fn initWithArtifact(repository: []const u8, strategy: Strategy, artifact: []const u8) Engine {
        return .{ .repo = repository, .strategy = strategy, .artifact = artifact };
    }
    pub fn deinit(self: *Engine) void {
        if (self.backend) |b| b.destroy();
        self.backend = null;
    }
    pub fn chunkBytes(self: *const Engine) u64 {
        return ((self.max_line - FRAME_OVERHEAD) / 4) * 3;
    }

    /// Handle one complete request line. `service_ns` in the reply runs from here (the complete request
    /// is in hand) to the moment the response body is ready; it excludes I/O.
    pub fn handle(self: *Engine, ra: A, line: []const u8) Reply {
        self.timer = std.time.Timer.start() catch unreachable;
        self.cur_id = null;
        return self.route(ra, line) catch |e| self.errorReply(ra, e);
    }

    fn errorReply(self: *Engine, ra: A, e: anyerror) Reply {
        if (!isUserError(e)) self.state = .FAILED;
        const ns = self.timer.read();
        const text = errorLine(ra, self.cur_id, codeOf(e), @errorName(e), ns) catch "{\"code\":\"INTERNAL\",\"id\":null,\"message\":\"out of memory\",\"ok\":false,\"service_ns\":0}";
        return .{ .ok = false, .id = self.cur_id, .service_ns = ns, .line = text };
    }

    fn route(self: *Engine, ra: A, line: []const u8) Err!Reply {
        const parsed = std.json.parseFromSliceLeaky(Value, ra, line, .{}) catch return error.InvalidRequest;
        if (parsed != .object) return error.InvalidRequest;
        const obj = parsed.object;
        self.cur_id = if (obj.get("id")) |v| asU64(v) else null;
        const id = self.cur_id orelse return error.InvalidRequest;
        const op_v = obj.get("op") orelse return error.InvalidRequest;
        if (op_v != .string) return error.InvalidRequest;
        const op = std.meta.stringToEnum(Op, op_v.string) orelse return error.InvalidRequest;
        switch (op) {
            .open => if (self.state != .NEW) return error.InvalidState,
            .close => if (self.state == .NEW) return error.InvalidState,
            else => if (self.state != .OPEN) return error.InvalidState,
        }
        return switch (op) {
            .open => self.doOpen(ra, id, obj),
            .query => self.doQuery(ra, id, obj),
            .mutate => self.doMutate(ra, id, obj),
            .state_digest => self.doDigest(ra, id, obj),
            .snapshot => self.doSnapshot(ra, id, obj),
            .restore => self.doRestore(ra, id, obj),
            .cancel => blk: {
                if (!exactKeys(obj, &.{ "id", "op", "target" })) return error.InvalidRequest;
                break :blk error.Unsupported;
            },
            .stats => self.doStats(ra, id, obj),
            .close => blk: {
                if (!exactKeys(obj, &.{ "id", "op" })) return error.InvalidRequest;
                var s = Sink.init(ra, false);
                const ns = self.timer.read();
                try s.raw("{\"id\":");
                try s.num(id);
                try s.raw(",\"ok\":true,\"service_ns\":");
                try s.num(ns);
                try s.raw("}");
                break :blk .{ .ok = true, .id = id, .service_ns = ns, .line = s.buf.items, .close = true };
            },
        };
    }

    /// `{"generation":G,"id":N` : the sorted-key prefix shared by every success response.
    fn head(s: *Sink, id: u64, generation: u64) !void {
        try s.raw("{\"generation\":");
        try s.num(generation);
        try s.raw(",\"id\":");
        try s.num(id);
    }
    /// Success response of the simple shape `{"generation":G,"id":N,"ok":true,"service_ns":X}`.
    fn simpleOk(self: *Engine, ra: A, id: u64) Err!Reply {
        var s = Sink.init(ra, false);
        try head(&s, id, self.generation);
        const ns = self.timer.read();
        try s.raw(",\"ok\":true,\"service_ns\":");
        try s.num(ns);
        try s.raw("}");
        return .{ .ok = true, .id = id, .generation = self.generation, .service_ns = ns, .line = s.buf.items };
    }
    fn backendNow(self: *Engine) Backend {
        return self.backend.?;
    }

    fn doOpen(self: *Engine, ra: A, id: u64, obj: ObjectMap) Err!Reply {
        if (!exactKeys(obj, &.{ "id", "op", "binding", "max_line_bytes", "source" })) return error.InvalidRequest;
        const binding = obj.get("binding").?;
        if (binding != .string or !std.mem.eql(u8, binding.string, BINDING)) return error.InvalidRequest;
        const max_line = asU64(obj.get("max_line_bytes").?) orelse return error.InvalidRequest;
        if (max_line < MIN_LINE) return error.InvalidRequest;
        const src = obj.get("source").?;
        if (src != .object) return error.InvalidRequest;
        const kind = src.object.get("kind") orelse return error.InvalidRequest;
        if (kind != .string) return error.InvalidRequest;
        var backend: Backend = undefined;
        if (std.mem.eql(u8, kind.string, "fixture")) {
            if (!exactKeys(src.object, &.{ "kind", "path" })) return error.InvalidRequest;
            const path = src.object.get("path").?;
            if (path != .string) return error.InvalidRequest;
            const bytes = std.fs.cwd().readFileAlloc(ra, path.string, std.math.maxInt(usize)) catch |e| return oom(e);
            backend = switch (self.strategy) {
                .@"full-rebuild" => .{ .full = try genFromFixture(&self.counters, bytes) },
                .incremental => try incFromFixture(&self.counters, ra, bytes),
            };
        } else if (std.mem.eql(u8, kind.string, "empty")) {
            if (!exactKeys(src.object, &.{ "kind", "context" })) return error.InvalidRequest;
            const c = src.object.get("context").?;
            if (c != .object or !exactKeys(c.object, &.{ "snapshot", "epoch", "complete" })) return error.InvalidRequest;
            const snap = c.object.get("snapshot").?;
            const complete = c.object.get("complete").?;
            const epoch = c.object.get("epoch").?;
            if (snap != .string or complete != .bool or !(epoch == .null or asU64(epoch) != null)) return error.InvalidRequest;
            const ctx = Context{ .snapshot = snap.string, .epoch = if (epoch == .null) null else asU64(epoch), .complete = complete.bool };
            backend = try buildBackend(self.strategy, &self.counters, ctx, &.{}, &.{}, &.{});
        } else return error.InvalidRequest;
        self.backend = backend;
        self.generation = 0;
        self.state = .OPEN;
        self.max_line = max_line;
        var s = Sink.init(ra, false);
        try s.raw("{\"artifact\":");
        try s.str(self.artifact);
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
        const ns = self.timer.read();
        try s.raw(",\"service_ns\":");
        try s.num(ns);
        try s.raw(",\"strategy\":{\"mutation\":");
        try s.str(@tagName(self.strategy));
        try s.raw("}}");
        return .{ .ok = true, .id = id, .generation = 0, .service_ns = ns, .line = s.buf.items };
    }

    fn doQuery(self: *Engine, ra: A, id: u64, obj: ObjectMap) Err!Reply {
        if (!exactKeys(obj, &.{ "id", "op", "query" })) return error.InvalidRequest;
        const text = std.json.stringifyAlloc(ra, obj.get("query").?, .{}) catch return error.OutOfMemory;
        const q = std.json.parseFromSliceLeaky(sem.Query, ra, text, .{}) catch |e| return oom(e);
        // Scratch memory for this query comes from the request arena, not the store.
        var store = self.backendNow().view(ra);
        const result = store.execute(q) catch |e| return oom(e);
        const ns = self.timer.read();
        // Envelope: at most ~130 bytes around the result; anything near the limit is chunked.
        if (result.len + 160 <= self.max_line) {
            var s = Sink.init(ra, false);
            try head(&s, id, self.generation);
            try s.raw(",\"ok\":true,\"result\":");
            try s.raw(result);
            try s.raw(",\"service_ns\":");
            try s.num(ns);
            try s.raw("}");
            if (s.buf.items.len + 1 <= self.max_line) {
                return .{ .ok = true, .id = id, .generation = self.generation, .service_ns = ns, .line = s.buf.items };
            }
        }
        return .{ .ok = true, .id = id, .generation = self.generation, .service_ns = ns, .payload = result, .chunked = true };
    }

    fn doMutate(self: *Engine, ra: A, id: u64, obj: ObjectMap) Err!Reply {
        if (!exactKeys(obj, &.{ "id", "op", "batch" })) return error.InvalidRequest;
        const batch = obj.get("batch").?;
        switch (self.backendNow()) {
            .full => |old| {
                const fresh = try applyFull(&self.counters, ra, old, batch);
                old.destroy();
                self.backend = .{ .full = fresh };
            },
            .inc => |inc| try inc.applyBatch(ra, batch),
        }
        self.generation += 1;
        return self.simpleOk(ra, id);
    }

    fn doDigest(self: *Engine, ra: A, id: u64, obj: ObjectMap) Err!Reply {
        if (!exactKeys(obj, &.{ "id", "op" })) return error.InvalidRequest;
        var timer = std.time.Timer.start() catch unreachable;
        const rows = try self.backendNow().rows(ra);
        const d = try stateDigest(ra, rows);
        const digest_ns = timer.read();
        var s = Sink.init(ra, false);
        try s.raw("{\"bytes_processed\":");
        try s.num(d.bytes);
        try s.raw(",\"generation\":");
        try s.num(self.generation);
        try s.raw(",\"id\":");
        try s.num(id);
        const ns = self.timer.read();
        try s.raw(",\"ok\":true,\"service_ns\":");
        try s.num(ns);
        try s.raw(",\"state_digest\":");
        try s.str(try std.fmt.allocPrint(ra, "sha256:{s}", .{std.fmt.fmtSliceHexLower(&d.digest)}));
        try s.raw(",\"state_digest_ms\":");
        try s.raw(try std.fmt.allocPrint(ra, "{d:.6}", .{@as(f64, @floatFromInt(digest_ns)) / 1e6}));
        try s.raw("}");
        return .{ .ok = true, .id = id, .generation = self.generation, .service_ns = ns, .line = s.buf.items };
    }

    fn snapshotPath(ra: A, id: []const u8) ![]const u8 {
        return std.fmt.allocPrint(ra, "{s}.snap", .{id});
    }

    fn doSnapshot(self: *Engine, ra: A, id: u64, obj: ObjectMap) Err!Reply {
        if (!exactKeys(obj, &.{ "id", "op" })) return error.InvalidRequest;
        const rows = try self.backendNow().rows(ra);
        const data = try encodeSnapshot(ra, rows, self.artifact);
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
        const ns = self.timer.read();
        try s.raw(",\"ok\":true,\"service_ns\":");
        try s.num(ns);
        try s.raw(",\"snapshot_id\":");
        try s.str(snap_id);
        try s.raw("}");
        return .{ .ok = true, .id = id, .generation = self.generation, .service_ns = ns, .line = s.buf.items };
    }

    fn doRestore(self: *Engine, ra: A, id: u64, obj: ObjectMap) Err!Reply {
        if (!exactKeys(obj, &.{ "id", "op", "snapshot_id" })) return error.InvalidRequest;
        const sid = obj.get("snapshot_id").?;
        if (sid != .string) return error.InvalidRequest;
        if (sid.string.len == 0 or sid.string.len > 64) return error.InvalidInput;
        for (sid.string) |c| if (!(std.ascii.isAlphanumeric(c) or c == '-' or c == '_')) return error.InvalidInput;
        var dir = std.fs.cwd().openDir(self.repo, .{}) catch return error.InvalidInput;
        defer dir.close();
        const data = dir.readFileAlloc(ra, try snapshotPath(ra, sid.string), std.math.maxInt(usize)) catch return error.InvalidInput;
        const old = self.backendNow();
        const rows = try decodeSnapshot(ra, data, old.ctx(), self.artifact);
        const fresh = try buildBackend(self.strategy, &self.counters, old.ctx(), rows.ents, rows.rels, rows.evs);
        old.destroy();
        self.backend = fresh;
        self.generation += 1;
        return self.simpleOk(ra, id);
    }

    fn doStats(self: *Engine, ra: A, id: u64, obj: ObjectMap) Err!Reply {
        if (!exactKeys(obj, &.{ "id", "op" })) return error.InvalidRequest;
        const c = self.backendNow().counts();
        var s = Sink.init(ra, false);
        try head(&s, id, self.generation);
        const ns = self.timer.read();
        try s.raw(",\"ok\":true,\"service_ns\":");
        try s.num(ns);
        try s.raw(",\"stats\":{\"allocations_total\":");
        try s.num(self.counters.allocations);
        try s.raw(",\"derived_rebuilds_total\":");
        try s.num(self.counters.rebuilds);
        try s.raw(",\"entities\":");
        try s.num(c.entities);
        try s.raw(",\"evidence\":");
        try s.num(c.evidence);
        try s.raw(",\"generation\":");
        try s.num(self.generation);
        try s.raw(",\"heap_breakdown\":null,\"live_heap_bytes\":");
        try s.num(self.counters.live);
        try s.raw(",\"peak_heap_bytes\":");
        try s.num(self.counters.peak_live);
        try s.raw(",\"relations\":");
        try s.num(c.relations);
        try s.raw(",\"schema\":\"csl.eval.session.stats/v0.1\",\"unique_strings\":");
        try s.num(c.unique);
        try s.raw("}}");
        return .{ .ok = true, .id = id, .generation = self.generation, .service_ns = ns, .line = s.buf.items };
    }
};

/// `open` of a fixture under the incremental strategy: parse (scratch memory from the request arena)
/// and bulk-build the incremental store.
fn incFromFixture(counters: *Counters, ra: A, bytes: []const u8) Err!Backend {
    const fx = std.json.parseFromSliceLeaky(SFix, ra, bytes, .{ .allocate = .alloc_always }) catch |e| return oom(e);
    if (!std.mem.eql(u8, fx.schema, "csl.eval.fixture/v0.1")) return error.InvalidInput;
    const ents = ra.alloc(LEnt, fx.entities.len) catch return error.OutOfMemory;
    for (fx.entities, 0..) |e, i| {
        if (e.name_sid >= fx.strings.len) return error.InvalidInput;
        ents[i] = .{ .id = e.id, .kind = e.kind, .name = fx.strings[e.name_sid], .container = e.container };
    }
    const ctx = Context{ .snapshot = fx.snapshot, .epoch = fx.epoch, .complete = fx.complete };
    return .{ .inc = try Inc.fromRows(counters, ctx, ents, fx.relations, fx.evidence) };
}

// ---- the stdin/stdout driver (framing, chunked requests, chunked results) ------------------------
const Pending = struct { id: u64, op: []const u8, data: std.ArrayList(u8), hasher: Sha256, chunks: u64 };

fn looksLikeFrame(line: []const u8) bool {
    const marker = "\"frame\":\"";
    if (std.mem.indexOf(u8, line[0..@min(line.len, 160)], marker) != null) return true;
    return std.mem.indexOf(u8, line[line.len - @min(line.len, 96) ..], marker) != null;
}

const Driver = struct {
    engine: Engine,
    out: std.io.BufferedWriter(1 << 16, std.fs.File.Writer),
    pending: ?Pending = null,
    pending_arena: std.heap.ArenaAllocator,

    fn writeLine(self: *Driver, s: []const u8) !void {
        const w = self.out.writer();
        try w.writeAll(s);
        try w.writeByte('\n');
    }
    fn flush(self: *Driver) void {
        self.out.flush() catch std.process.exit(1);
    }
    fn dropPending(self: *Driver) void {
        self.pending = null;
        _ = self.pending_arena.reset(.free_all);
    }
    fn fail(self: *Driver, ra: A, id: ?u64, e: anyerror, started: *std.time.Timer) void {
        const line = errorLine(ra, id, codeOf(e), @errorName(e), started.read()) catch return;
        self.writeLine(line) catch std.process.exit(1);
    }

    fn emit(self: *Driver, ra: A, r: Reply) void {
        if (r.chunked) {
            self.writeChunked(ra, r) catch std.process.exit(1);
        } else {
            self.writeLine(r.line) catch std.process.exit(1);
        }
        if (r.close) {
            self.flush();
            std.process.exit(0);
        }
    }

    /// begin / chunk* / end frames for a result too large for one line. `service_ns` of the end frame
    /// adds the time spent producing the frames (encoding and hashing), never the pipe writes.
    fn writeChunked(self: *Driver, ra: A, r: Reply) !void {
        const payload = r.payload.?;
        const id = r.id.?;
        var timer = try std.time.Timer.start();
        var spent: u64 = r.service_ns;
        var b = Sink.init(ra, false);
        try b.raw("{\"frame\":\"begin\",\"id\":");
        try b.num(id);
        try b.raw("}");
        spent += timer.read();
        try self.writeLine(b.buf.items);
        const step = self.engine.chunkBytes();
        var hash = Sha256.init(.{});
        var seq: u64 = 0;
        var pos: usize = 0;
        while (pos < payload.len) : (seq += 1) {
            timer.reset();
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
            spent += timer.read();
            try self.writeLine(c.buf.items);
            ra.free(enc);
            pos = end;
        }
        timer.reset();
        var out: [32]u8 = undefined;
        hash.final(&out);
        var e = Sink.init(ra, false);
        try e.raw("{\"bytes\":");
        try e.num(payload.len);
        try e.raw(",\"chunks\":");
        try e.num(seq);
        try e.raw(",\"frame\":\"end\",\"generation\":");
        try e.num(r.generation.?);
        try e.raw(",\"id\":");
        try e.num(id);
        try e.raw(",\"ok\":true,\"service_ns\":");
        try e.num(spent + timer.read());
        try e.raw(",\"sha256\":");
        try e.str(try hexLower(ra, &out));
        try e.raw("}");
        try self.writeLine(e.buf.items);
    }

    fn onLine(self: *Driver, ra: A, line: []const u8) void {
        var started = std.time.Timer.start() catch unreachable;
        if (looksLikeFrame(line)) {
            const merged = self.onFrame(ra, line, &started) catch |e| {
                self.fail(ra, frameId(ra, line), e, &started);
                self.dropPending();
                return;
            } orelse return;
            return self.emit(ra, self.engine.handle(ra, merged));
        }
        if (self.pending != null) self.dropPending();
        self.emit(ra, self.engine.handle(ra, line));
    }

    fn frameId(ra: A, line: []const u8) ?u64 {
        const parsed = std.json.parseFromSliceLeaky(Value, ra, line, .{}) catch return null;
        if (parsed != .object) return null;
        return if (parsed.object.get("id")) |v| asU64(v) else null;
    }

    /// Process one transport frame; returns the reassembled request line on a verified `end`.
    fn onFrame(self: *Driver, ra: A, line: []const u8, started: *std.time.Timer) !?[]const u8 {
        _ = started;
        const parsed = std.json.parseFromSliceLeaky(Value, ra, line, .{}) catch return error.InvalidRequest;
        if (parsed != .object) return error.InvalidRequest;
        const obj = parsed.object;
        const kind_v = obj.get("frame") orelse return error.InvalidRequest;
        const kind = if (kind_v == .string) kind_v.string else return error.InvalidRequest;
        const rid = (if (obj.get("id")) |v| asU64(v) else null) orelse return error.InvalidRequest;
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
            const rest = p.data.items;
            const body = std.json.parseFromSliceLeaky(Value, ra, rest, .{}) catch return error.InvalidRequest;
            if (body != .object or body.object.contains("id") or body.object.contains("op")) return error.InvalidRequest;
            // Reassembled request = {"id":N,"op":"<op>", <the remaining fields>}.
            var m = Sink.init(ra, false);
            try m.raw("{\"id\":");
            try m.num(p.id);
            try m.raw(",\"op\":");
            try m.str(p.op);
            if (rest.len > 2) {
                try m.raw(",");
                try m.raw(rest[1..]);
            } else try m.raw("}");
            self.dropPending();
            return m.buf.items;
        }
        return error.InvalidRequest;
    }
};

/// Entry point for `csl-eval-zig session --repository DIR [--strategy full-rebuild|incremental]`.
pub fn serve(repository: []const u8, strategy: Strategy) void {
    var driver = Driver{
        .engine = Engine.init(repository, strategy),
        .out = .{ .unbuffered_writer = std.io.getStdOut().writer() },
        .pending_arena = std.heap.ArenaAllocator.init(std.heap.page_allocator),
    };
    // Buffered: an unbuffered reader would issue one read syscall per byte of a multi-megabyte request line.
    var buffered = std.io.bufferedReader(std.io.getStdIn().reader());
    const stdin = buffered.reader();
    while (true) {
        var request = std.heap.ArenaAllocator.init(std.heap.page_allocator);
        defer request.deinit();
        const ra = request.allocator();
        const line = stdin.readUntilDelimiterOrEofAlloc(ra, '\n', std.math.maxInt(usize)) catch {
            std.process.exit(1);
        } orelse {
            driver.flush();
            std.process.exit(0);
        };
        driver.onLine(ra, line);
        driver.flush();
    }
}

// ---- tests ---------------------------------------------------------------------------------------
test "canonical string escaping matches json.dumps(ensure_ascii=False)" {
    var s = Sink.init(std.testing.allocator, false);
    defer s.buf.deinit();
    try s.str("a\"b\\c\x08\x0c\n\r\t\x01\x1f\x7f\u{e9}\u{1F600}");
    try std.testing.expectEqualStrings("\"a\\\"b\\\\c\\b\\f\\n\\r\\t\\u0001\\u001f\x7f\u{e9}\u{1F600}\"", s.buf.items);
}

test "chunk size keeps a base64 chunk line within the negotiated line length" {
    var engine = Engine.init(".", .@"full-rebuild");
    engine.max_line = MIN_LINE;
    const raw = engine.chunkBytes();
    try std.testing.expect(((raw + 2) / 3) * 4 + FRAME_OVERHEAD <= MIN_LINE);
}

fn between(text: []const u8, start: []const u8, end: []const u8) []const u8 {
    const i = std.mem.indexOf(u8, text, start).? + start.len;
    const j = std.mem.indexOfPos(u8, text, i, end).?;
    return text[i..j];
}

test "both strategies agree on digest and results through the engine" {
    var arena = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena.deinit();
    const ra = arena.allocator();
    const open = "{\"binding\":\"csl.eval.session.jsonl/v0\",\"id\":1,\"max_line_bytes\":65536,\"op\":\"open\",\"source\":{\"context\":{\"complete\":false,\"epoch\":1,\"snapshot\":\"T\"},\"kind\":\"empty\"}}";
    const script = [_][]const u8{
        "{\"batch\":[{\"op\":\"ADD_ENTITY\",\"id\":1,\"kind\":\"TYPE\",\"name\":\"a\",\"container\":null},{\"op\":\"ADD_ENTITY\",\"id\":2,\"kind\":\"METHOD\",\"name\":\"a\",\"container\":1},{\"op\":\"ADD_ENTITY\",\"id\":3,\"kind\":\"FIELD\",\"name\":\"b\",\"container\":null},{\"op\":\"ADD_RELATION\",\"subject\":1,\"relation\":\"CALLS\",\"object\":2},{\"op\":\"ADD_RELATION\",\"subject\":1,\"relation\":\"CALLS\",\"object\":2},{\"op\":\"ADD_EVIDENCE\",\"proposition\":5,\"subject\":1,\"relation\":\"CALLS\",\"object\":2,\"polarity\":\"POSITIVE\",\"quality\":\"EXACT\",\"freshness_epoch\":1,\"lineage\":0}],\"id\":2,\"op\":\"mutate\"}",
        "{\"batch\":[{\"op\":\"UPDATE_ENTITY\",\"id\":3,\"set\":{\"name\":\"a\",\"kind\":\"TYPE\"}},{\"op\":\"REMOVE_RELATION\",\"subject\":1,\"relation\":\"CALLS\",\"object\":2}],\"id\":3,\"op\":\"mutate\"}",
        "{\"batch\":[{\"op\":\"REMOVE_ENTITY\",\"id\":2}],\"id\":4,\"op\":\"mutate\"}",
        "{\"batch\":[{\"op\":\"REMOVE_RELATION\",\"subject\":1,\"relation\":\"CALLS\",\"object\":2},{\"op\":\"REMOVE_EVIDENCE\",\"proposition\":5,\"subject\":1,\"relation\":\"CALLS\",\"object\":2,\"polarity\":\"POSITIVE\",\"quality\":\"EXACT\",\"freshness_epoch\":1,\"lineage\":0},{\"op\":\"REMOVE_ENTITY\",\"id\":2}],\"id\":5,\"op\":\"mutate\"}",
    };
    const query = "{\"id\":9,\"op\":\"query\",\"query\":{\"op\":\"RESOLVE\",\"name\":\"a\",\"query_id\":\"q\",\"schema\":\"csl.eval.query/v0.1\"}}";
    const digest = "{\"id\":8,\"op\":\"state_digest\"}";
    var digests: [2][]const u8 = undefined;
    var results: [2][]const u8 = undefined;
    for ([_]Strategy{ .@"full-rebuild", .incremental }, 0..) |strategy, i| {
        var engine = Engine.init(".", strategy);
        defer engine.deinit();
        try std.testing.expect(engine.handle(ra, open).ok);
        // ids 1..3 exist; the second and third batches remove/rename; the third (remove referenced) must fail.
        try std.testing.expect(engine.handle(ra, script[0]).ok);
        try std.testing.expect(engine.handle(ra, script[1]).ok);
        const rejected = engine.handle(ra, script[2]);
        try std.testing.expect(!rejected.ok);
        try std.testing.expect(std.mem.indexOf(u8, rejected.line, "INVALID_INPUT") != null);
        try std.testing.expectEqual(@as(u64, 2), engine.generation); // a rejected batch changes nothing
        try std.testing.expect(engine.handle(ra, script[3]).ok);
        digests[i] = between(engine.handle(ra, digest).line, "\"state_digest\":\"", "\"");
        results[i] = between(engine.handle(ra, query).line, "\"result\":", ",\"service_ns\"");
    }
    try std.testing.expectEqualStrings(digests[0], digests[1]);
    try std.testing.expectEqualStrings(results[0], results[1]);
}
