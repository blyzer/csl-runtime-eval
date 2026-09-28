const std = @import("std");
const sem = @import("semantic.zig");
fn arg(args: [][:0]u8, key: []const u8) ![]const u8 {
    for (args, 0..) |v, i| {
        if (std.mem.eql(u8, v, key) and i + 1 < args.len) return args[i + 1];
    }
    return error.MissingArgument;
}
fn run(a: std.mem.Allocator) ![]const u8 {
    const args = try std.process.argsAlloc(a);
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
    var timer = try std.time.Timer.start();
    const fixture = try std.fs.cwd().readFileAlloc(a, try arg(args, if (workload) "--corpus" else "--fixture"), std.math.maxInt(usize));
    const fx = try std.json.parseFromSlice(sem.Fixture, a, fixture, .{});
    const load_ns = timer.lap();
    const store = try sem.Store.init(a, fx.value);
    const index_ns = timer.read();
    if (std.mem.eql(u8, args[1], "load")) return std.json.stringifyAlloc(a, .{ .entities = fx.value.entities.len, .snapshot = fx.value.snapshot }, .{});
    if (workload) {
        const id = try arg(args, "--id");
        if (!std.mem.eql(u8, id, "W1") and !std.mem.eql(u8, id, "W2")) return error.InvalidWorkload;
        const params = try std.json.parseFromSlice(struct { query: sem.Query }, a, try arg(args, "--params"), .{});
        for (args) |value| {
            if (std.mem.eql(u8, value, "--profile")) {
                timer.reset();
                const outcome = try store.query(params.value.query);
                const query_ns = timer.lap();
                const selection = try store.materialize(params.value.query, outcome);
                const materialize_ns = timer.lap();
                const result = try store.encode(params.value.query, selection);
                const encode_ns = timer.read();
                const result_ns = materialize_ns + encode_ns;
                // Embed the already serialized result without allocating a second JSON tree.
                // decode/construct are fused: std.json.parseFromSlice parses bytes directly
                // into the typed Fixture, with no separate construction pass to isolate.
                const metadata = try std.json.stringifyAlloc(a, .{
                    .profile_schema = "csl.eval.profile/v0.1",
                    .representation = "typed-hash-v1",
                    .phases_ns = .{ .load = load_ns, .index = index_ns, .query = query_ns, .result = result_ns },
                    .phase_detail_ns = .{ .decode = load_ns, .construct = @as(u64, 0), .materialize = materialize_ns, .encode = encode_ns },
                }, .{});
                return std.fmt.allocPrint(a, "{s},\"result\":{s}}}", .{ metadata[0 .. metadata.len - 1], result });
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
    var arena = std.heap.ArenaAllocator.init(std.heap.page_allocator);
    defer arena.deinit();
    const output = run(arena.allocator()) catch |e| {
        std.debug.print("{s}\n", .{@errorName(e)});
        std.process.exit(1);
    };
    std.io.getStdOut().writer().print("{s}\n", .{output}) catch std.process.exit(1);
}
