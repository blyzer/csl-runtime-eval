const std = @import("std");
const sem = @import("semantic.zig");
const session = @import("session.zig");
fn arg(args: [][:0]u8, key: []const u8) ![]const u8 {
    for (args, 0..) |v, i| {
        if (std.mem.eql(u8, v, key) and i + 1 < args.len) return args[i + 1];
    }
    return error.MissingArgument;
}
fn hasFlag(args: [][:0]u8, flag: []const u8) bool {
    for (args) |v| {
        if (std.mem.eql(u8, v, flag)) return true;
    }
    return false;
}
/// Wraps an allocator and counts bytes requested at the allocator interface.
/// Only ever constructed under `--stats`; normal runs use the plain allocator.
const CountingAllocator = struct {
    child: std.mem.Allocator,
    allocations: u64 = 0,
    total_requested: u64 = 0,
    total_freed: u64 = 0,
    live: u64 = 0,
    peak_live: u64 = 0,
    fn allocator(self: *CountingAllocator) std.mem.Allocator {
        return .{ .ptr = self, .vtable = &.{ .alloc = alloc, .resize = resize, .remap = remap, .free = free } };
    }
    fn grow(self: *CountingAllocator, old_len: usize, new_len: usize) void {
        if (new_len >= old_len) {
            self.total_requested += new_len - old_len;
            self.live += new_len - old_len;
            if (self.live > self.peak_live) self.peak_live = self.live;
        } else {
            self.total_freed += old_len - new_len;
            self.live -= old_len - new_len;
        }
    }
    fn alloc(ctx: *anyopaque, len: usize, alignment: std.mem.Alignment, ret_addr: usize) ?[*]u8 {
        const self: *CountingAllocator = @ptrCast(@alignCast(ctx));
        const ptr = self.child.rawAlloc(len, alignment, ret_addr) orelse return null;
        self.allocations += 1;
        self.grow(0, len);
        return ptr;
    }
    fn resize(ctx: *anyopaque, memory: []u8, alignment: std.mem.Alignment, new_len: usize, ret_addr: usize) bool {
        const self: *CountingAllocator = @ptrCast(@alignCast(ctx));
        if (!self.child.rawResize(memory, alignment, new_len, ret_addr)) return false;
        self.grow(memory.len, new_len);
        return true;
    }
    fn remap(ctx: *anyopaque, memory: []u8, alignment: std.mem.Alignment, new_len: usize, ret_addr: usize) ?[*]u8 {
        const self: *CountingAllocator = @ptrCast(@alignCast(ctx));
        const ptr = self.child.rawRemap(memory, alignment, new_len, ret_addr) orelse return null;
        if (ptr != memory.ptr) self.allocations += 1;
        self.grow(memory.len, new_len);
        return ptr;
    }
    fn free(ctx: *anyopaque, memory: []u8, alignment: std.mem.Alignment, ret_addr: usize) void {
        const self: *CountingAllocator = @ptrCast(@alignCast(ctx));
        self.child.rawFree(memory, alignment, ret_addr);
        self.grow(memory.len, 0);
    }
};
fn run(arena: *std.heap.ArenaAllocator) ![]const u8 {
    const base = arena.allocator();
    const args = try std.process.argsAlloc(base);
    const stats = hasFlag(args, "--stats");
    var counting = CountingAllocator{ .child = base };
    const a = if (stats) counting.allocator() else base;
    if (args.len < 2 or std.mem.eql(u8, args[1], "info")) return "{\"candidate\":\"zig\",\"status\":\"typed-hash-v1\",\"representation\":\"typed-hash-v1\"}";
    if (std.mem.eql(u8, args[1], "boundary")) {
        const size = try std.fmt.parseInt(usize, try arg(args, "--size"), 10);
        const repeat = try std.fmt.parseInt(usize, try arg(args, "--repeat"), 10);
        if (repeat == 0) return error.InvalidRepeat;
        const input = try a.alloc(u8, size);
        @memset(input, 42);
        const times = try a.alloc(u64, repeat);
        var hash: [32]u8 = undefined;
        for (times) |*ns| {
            var timer = try std.time.Timer.start();
            const output = try std.heap.c_allocator.dupe(u8, input);
            std.mem.doNotOptimizeAway(output.ptr);
            ns.* = timer.read();
            std.crypto.hash.sha2.Sha256.hash(output, &hash, .{});
            std.heap.c_allocator.free(output);
        }
        const digest = try std.fmt.allocPrint(a, "sha256:{s}", .{std.fmt.fmtSliceHexLower(&hash)});
        return std.json.stringifyAlloc(a, .{ .strategy = "pure-zig", .payload_bytes = size, .latency_ns = times, .calls_per_query = 0, .bytes_copied = size, .output_allocations_per_call = @as(u8, if (size == 0) 0 else 1), .result_digest = digest }, .{});
    }
    const workload = std.mem.eql(u8, args[1], "workload");
    if (stats and !(workload and hasFlag(args, "--profile"))) return error.InvalidStats;
    var timer = try std.time.Timer.start();
    const fixture = try std.fs.cwd().readFileAlloc(a, try arg(args, if (workload) "--corpus" else "--fixture"), std.math.maxInt(usize));
    const read_ns = timer.lap();
    const fx = try std.json.parseFromSlice(sem.Fixture, a, fixture, .{});
    const parse_ns = timer.lap();
    const load_ns = read_ns + parse_ns;
    const live_after_load = counting.live;
    try sem.Store.checkSchema(fx.value);
    const entities = try sem.Store.buildEntities(a, fx.value);
    const entities_ns = timer.lap();
    var adjacency = try sem.Store.buildAdjacency(a, fx.value, &entities);
    const adjacency_ns = timer.lap();
    sem.Store.sortAdjacency(&adjacency.out, &adjacency.inc);
    const sort_ns = timer.read();
    const index_ns = entities_ns + adjacency_ns + sort_ns;
    const live_after_index = counting.live;
    const store = sem.Store{ .a = a, .fx = fx.value, .entities = entities, .out = adjacency.out, .inc = adjacency.inc, .name_cache = try sem.NameCache.create(a) };
    if (std.mem.eql(u8, args[1], "load")) return std.json.stringifyAlloc(a, .{ .entities = fx.value.entities.len, .snapshot = fx.value.snapshot }, .{});
    if (workload) {
        const id = try arg(args, "--id");
        if (!std.mem.eql(u8, id, "W1") and !std.mem.eql(u8, id, "W2") and !std.mem.eql(u8, id, "W3") and !std.mem.eql(u8, id, "W4")) return error.InvalidWorkload;
        const params = try std.json.parseFromSlice(struct { query: sem.Query, resolve: ?struct { names: []const []const u8, rounds: u64 } = null }, a, try arg(args, "--params"), .{});
        if (params.value.resolve) |r| {
            if (!hasFlag(args, "--profile")) return error.InvalidResolve;
            if (r.names.len == 0 or r.rounds == 0 or r.names.len * r.rounds > 1_000_000) return error.InvalidResolve;
        }
        for (args) |value| {
            if (std.mem.eql(u8, value, "--profile")) {
                timer.reset();
                const outcome = try store.query(params.value.query);
                const query_ns = timer.lap();
                const live_after_query = counting.live;
                const selection = try store.materialize(params.value.query, outcome);
                const materialize_ns = timer.lap();
                const result = try store.encode(params.value.query, selection);
                const encode_ns = timer.read();
                const result_ns = materialize_ns + encode_ns;
                // Optional extras, all outside the recorded phases: repeated name
                // resolution against the name index, then heap accounting.
                var extras: []const u8 = "";
                // Heap counters are read once, before the lookup-latency array and the
                // digest/JSON blocks below are allocated, so they describe the store (and
                // the name index when one was requested), not measurement bookkeeping.
                var heap_snapshot = counting;
                if (params.value.resolve) |r| {
                    const cache = store.name_cache.?;
                    try cache.ensure(fx.value);
                    heap_snapshot = counting;
                    const lookups = r.names.len * r.rounds;
                    const lookup_ns = try a.alloc(u64, lookups);
                    var hasher = std.crypto.hash.sha2.Sha256.init(.{});
                    var ids_total: u64 = 0;
                    var lookup_timer = try std.time.Timer.start();
                    var n: usize = 0;
                    for (0..r.rounds) |round| {
                        for (r.names) |name| {
                            lookup_timer.reset();
                            const ids = cache.resolve(name);
                            lookup_ns[n] = lookup_timer.read();
                            std.mem.doNotOptimizeAway(ids.len);
                            n += 1;
                            ids_total += ids.len;
                            if (round == 0) {
                                var buf: [24]u8 = undefined;
                                hasher.update("[");
                                for (ids, 0..) |v, i| {
                                    if (i > 0) hasher.update(",");
                                    hasher.update(try std.fmt.bufPrint(&buf, "{d}", .{v}));
                                }
                                hasher.update("]\n");
                            }
                        }
                    }
                    var digest_bytes: [32]u8 = undefined;
                    hasher.final(&digest_bytes);
                    const digest = try std.fmt.allocPrint(a, "sha256:{s}", .{std.fmt.fmtSliceHexLower(&digest_bytes)});
                    const block = try std.json.stringifyAlloc(a, .{ .intern_ns = cache.intern_ns, .unique_strings = cache.unique(), .lookups = lookups, .lookup_ns = lookup_ns, .lookup_digest = digest, .lookup_ids_total = ids_total }, .{});
                    extras = try std.fmt.allocPrint(a, ",\"name_index\":{s}", .{block});
                }
                if (stats) {
                    const block = try std.json.stringifyAlloc(a, .{ .model = "requested-bytes", .allocations = heap_snapshot.allocations, .total_requested = heap_snapshot.total_requested, .total_freed = heap_snapshot.total_freed, .live = heap_snapshot.live, .peak_live = heap_snapshot.peak_live, .live_after_load = live_after_load, .live_after_index = live_after_index, .live_after_query = live_after_query, .retained = arena.queryCapacity() }, .{});
                    extras = try std.fmt.allocPrint(a, "{s},\"heap\":{s}", .{ extras, block });
                }
                // Embed the already serialized result without allocating a second JSON tree.
                // decode/construct are fused: std.json.parseFromSlice parses bytes directly
                // into the typed Fixture, with no separate construction pass to isolate.
                const metadata = try std.json.stringifyAlloc(a, .{
                    .profile_schema = "csl.eval.profile/v0.1",
                    .representation = "typed-hash-v1",
                    .phases_ns = .{ .load = load_ns, .index = index_ns, .query = query_ns, .result = result_ns },
                    .phase_detail_ns = .{ .decode = load_ns, .construct = @as(u64, 0), .materialize = materialize_ns, .encode = encode_ns },
                    .phase_subdetail_ns = .{ .read = read_ns, .parse = parse_ns, .entities = entities_ns, .adjacency = adjacency_ns, .sort = sort_ns },
                }, .{});
                return std.fmt.allocPrint(a, "{s}{s},\"result\":{s}}}", .{ metadata[0 .. metadata.len - 1], extras, result });
            }
        }
        return store.execute(params.value.query);
    }
    if (!std.mem.eql(u8, args[1], "query")) return error.InvalidCommand;
    const bytes = try std.fs.cwd().readFileAlloc(a, try arg(args, "--query"), 1024 * 1024);
    const q = try std.json.parseFromSlice(sem.Query, a, bytes, .{});
    return store.execute(q.value);
}
pub fn main() void {
    // S0 session mode is a separate command; every one-shot command below is unchanged.
    var it = std.process.args();
    _ = it.skip();
    if (it.next()) |first| {
        if (std.mem.eql(u8, first, "session")) {
            var repo: []const u8 = ".";
            var strategy: session.Strategy = .@"full-rebuild";
            while (it.next()) |value| {
                if (std.mem.eql(u8, value, "--repository")) repo = it.next() orelse "";
                if (std.mem.eql(u8, value, "--strategy")) {
                    strategy = session.Strategy.parse(it.next() orelse "") orelse {
                        std.debug.print("unknown --strategy (full-rebuild | incremental)\n", .{});
                        std.process.exit(2);
                    };
                }
            }
            session.serve(repo, strategy);
            return;
        }
    }
    var arena = std.heap.ArenaAllocator.init(std.heap.page_allocator);
    defer arena.deinit();
    const output = run(&arena) catch |e| {
        std.debug.print("{s}\n", .{@errorName(e)});
        std.process.exit(1);
    };
    std.io.getStdOut().writer().print("{s}\n", .{output}) catch std.process.exit(1);
}
