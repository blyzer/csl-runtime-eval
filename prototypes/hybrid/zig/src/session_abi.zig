//! Experimental persistent session boundary (abi/csl_session_experimental.h): a thin C ABI over the
//! pure-Zig S0 `Engine`. One call per S0 request; the engine is not reimplemented here.
//!
//! Per call: a request arena owns everything the engine produces; the reply bytes are then COPIED
//! into caller-owned allocations (private-header layout identical to ABI v1, so `csl_buffer_release`
//! from abi.zig frees them) before the arena is dropped. Those copies are the boundary's price for
//! the arena lifetime and are counted by the Rust side (`boundary.copies`).
const std = @import("std");
const builtin = @import("builtin");
const session = @import("session");

const allocator = if (builtin.is_test) std.testing.allocator else std.heap.c_allocator;
pub const Buffer = extern struct { ptr: ?[*]u8 = null, len: usize = 0 };
pub const Reply = extern struct {
    line: Buffer = .{},
    payload: Buffer = .{},
    service_ns: u64 = 0,
    generation: u64 = 0,
    id: u64 = 0,
    flags: u32 = 0,
    reserved: u32 = 0,
};
const FLAG_OK: u32 = 0x01;
const FLAG_CHUNKED: u32 = 0x02;
const FLAG_CLOSE: u32 = 0x04;
const FLAG_HAS_ID: u32 = 0x08;
const FLAG_HAS_GENERATION: u32 = 0x10;
const header_len = @sizeOf(usize);
const HYBRID_ARTIFACT = "csl-eval-hybrid/s0-v0";

pub const Session = struct {
    engine: session.Engine,
    repository: []u8,
};

const ok: i32 = 0;
const invalid_argument: i32 = 1;
const out_of_memory: i32 = 2;
const invalid_query: i32 = 3;

export fn csl_session_abi_version() u32 {
    return 1;
}

export fn csl_session_create(repository: ?[*]const u8, repository_len: usize, strategy: ?[*]const u8, strategy_len: usize, out: ?*?*Session) i32 {
    const output = out orelse return invalid_argument;
    output.* = null;
    if (repository == null or repository_len == 0) return invalid_argument;
    if (strategy == null and strategy_len != 0) return invalid_argument;
    const chosen = session.Strategy.parse(if (strategy_len == 0) "full-rebuild" else strategy.?[0..strategy_len]) orelse return invalid_query;
    const s = allocator.create(Session) catch return out_of_memory;
    const repo = allocator.dupe(u8, repository.?[0..repository_len]) catch {
        allocator.destroy(s);
        return out_of_memory;
    };
    s.* = .{ .engine = session.Engine.initWithArtifact(repo, chosen, HYBRID_ARTIFACT), .repository = repo };
    output.* = s;
    return ok;
}

export fn csl_session_destroy(s: ?*Session) void {
    if (s) |live| {
        live.engine.deinit();
        allocator.free(live.repository);
        allocator.destroy(live);
    }
}

fn copyBuffer(bytes: []const u8) !Buffer {
    if (bytes.len == 0) return .{};
    const total = try std.math.add(usize, header_len, bytes.len);
    const block = try allocator.alloc(u8, total);
    std.mem.writeInt(usize, block[0..header_len], total, .little);
    @memcpy(block[header_len..], bytes);
    return .{ .ptr = block.ptr + header_len, .len = bytes.len };
}

fn freeBuffer(buffer: Buffer) void {
    if (buffer.ptr) |p| {
        const start = p - header_len;
        const total = std.mem.readInt(usize, start[0..header_len], .little);
        allocator.free(start[0..total]);
    }
}

fn call(s: ?*Session, line: ?[*]const u8, len: usize, out: ?*Reply) i32 {
    const reply = out orelse return invalid_argument;
    reply.* = .{};
    const live = s orelse return invalid_argument;
    if (line == null and len != 0) return invalid_argument;
    var arena = std.heap.ArenaAllocator.init(allocator);
    defer arena.deinit();
    const r = live.engine.handle(arena.allocator(), if (len == 0) "" else line.?[0..len]);
    const line_copy = copyBuffer(r.line) catch return out_of_memory;
    const payload_copy = copyBuffer(r.payload orelse "") catch {
        freeBuffer(line_copy);
        return out_of_memory;
    };
    reply.* = .{
        .line = line_copy,
        .payload = payload_copy,
        .service_ns = r.service_ns,
        .generation = r.generation orelse 0,
        .id = r.id orelse 0,
        .flags = (if (r.ok) FLAG_OK else 0) | (if (r.chunked) FLAG_CHUNKED else 0) | (if (r.close) FLAG_CLOSE else 0) |
            (if (r.id != null) FLAG_HAS_ID else 0) | (if (r.generation != null) FLAG_HAS_GENERATION else 0),
    };
    return ok;
}

export fn csl_session_open(s: ?*Session, line: ?[*]const u8, len: usize, out: ?*Reply) i32 {
    return call(s, line, len, out);
}
export fn csl_session_query(s: ?*Session, line: ?[*]const u8, len: usize, out: ?*Reply) i32 {
    return call(s, line, len, out);
}
export fn csl_session_mutate(s: ?*Session, line: ?[*]const u8, len: usize, out: ?*Reply) i32 {
    return call(s, line, len, out);
}
export fn csl_session_state_digest(s: ?*Session, line: ?[*]const u8, len: usize, out: ?*Reply) i32 {
    return call(s, line, len, out);
}
export fn csl_session_snapshot(s: ?*Session, line: ?[*]const u8, len: usize, out: ?*Reply) i32 {
    return call(s, line, len, out);
}
export fn csl_session_restore(s: ?*Session, line: ?[*]const u8, len: usize, out: ?*Reply) i32 {
    return call(s, line, len, out);
}
export fn csl_session_stats(s: ?*Session, line: ?[*]const u8, len: usize, out: ?*Reply) i32 {
    return call(s, line, len, out);
}
export fn csl_session_cancel(s: ?*Session, line: ?[*]const u8, len: usize, out: ?*Reply) i32 {
    return call(s, line, len, out);
}
export fn csl_session_close(s: ?*Session, line: ?[*]const u8, len: usize, out: ?*Reply) i32 {
    return call(s, line, len, out);
}
export fn csl_session_request(s: ?*Session, line: ?[*]const u8, len: usize, out: ?*Reply) i32 {
    return call(s, line, len, out);
}

fn release(reply: Reply) void {
    freeBuffer(reply.line);
    freeBuffer(reply.payload);
}

test "create, open, query, snapshot-less lifecycle and buffer ownership" {
    const t = std.testing;
    var tmp = t.tmpDir(.{});
    defer tmp.cleanup();
    const repo = try tmp.dir.realpathAlloc(t.allocator, ".");
    defer t.allocator.free(repo);
    var s: ?*Session = null;
    try t.expectEqual(invalid_argument, csl_session_create(null, 0, "full-rebuild", 12, &s));
    try t.expectEqual(invalid_query, csl_session_create(repo.ptr, repo.len, "nope", 4, &s));
    try t.expect(s == null);
    try t.expectEqual(invalid_argument, csl_session_create(repo.ptr, repo.len, "full-rebuild", 12, null));
    try t.expectEqual(ok, csl_session_create(repo.ptr, repo.len, "incremental", 11, &s));
    var reply = Reply{};
    try t.expectEqual(invalid_argument, csl_session_open(null, "x", 1, &reply));
    try t.expectEqual(invalid_argument, csl_session_open(s, null, 1, &reply));
    try t.expectEqual(invalid_argument, csl_session_open(s, "x", 1, null));
    // a query before open is an S0 INVALID_STATE response, not a boundary fault
    const early = "{\"id\":1,\"op\":\"stats\"}";
    try t.expectEqual(ok, csl_session_stats(s, early.ptr, early.len, &reply));
    try t.expect(reply.flags & FLAG_OK == 0);
    try t.expect(std.mem.indexOf(u8, reply.line.ptr.?[0..reply.line.len], "INVALID_STATE") != null);
    release(reply);
    const open_line = "{\"binding\":\"csl.eval.session.jsonl/v0\",\"id\":2,\"max_line_bytes\":65536,\"op\":\"open\",\"source\":{\"context\":{\"complete\":false,\"epoch\":1,\"snapshot\":\"S0\"},\"kind\":\"empty\"}}";
    try t.expectEqual(ok, csl_session_open(s, open_line.ptr, open_line.len, &reply));
    try t.expect(reply.flags & FLAG_OK != 0);
    try t.expect(std.mem.indexOf(u8, reply.line.ptr.?[0..reply.line.len], "csl-eval-hybrid/s0-v0") != null);
    try t.expect(std.mem.indexOf(u8, reply.line.ptr.?[0..reply.line.len], "\"mutation\":\"incremental\"") != null);
    release(reply);
    const query_line = "{\"id\":3,\"op\":\"query\",\"query\":{\"op\":\"FILTER\",\"query_id\":\"q\",\"schema\":\"csl.eval.query/v0.1\"}}";
    try t.expectEqual(ok, csl_session_query(s, query_line.ptr, query_line.len, &reply));
    try t.expect(reply.flags & FLAG_OK != 0 and reply.flags & FLAG_CHUNKED == 0);
    try t.expectEqual(@as(u64, 0), reply.generation);
    release(reply);
    csl_session_destroy(s);
    csl_session_destroy(null);
}

test "malformed requests reach the engine's error path through the fallback entry" {
    const t = std.testing;
    var tmp = t.tmpDir(.{});
    defer tmp.cleanup();
    const repo = try tmp.dir.realpathAlloc(t.allocator, ".");
    defer t.allocator.free(repo);
    var s: ?*Session = null;
    try t.expectEqual(ok, csl_session_create(repo.ptr, repo.len, "full-rebuild", 12, &s));
    defer csl_session_destroy(s);
    var reply = Reply{};
    try t.expectEqual(ok, csl_session_request(s, "{not json", 9, &reply));
    try t.expect(reply.flags & FLAG_OK == 0);
    try t.expect(std.mem.indexOf(u8, reply.line.ptr.?[0..reply.line.len], "INVALID_REQUEST") != null);
    release(reply);
    try t.expectEqual(ok, csl_session_request(s, "", 0, &reply));
    release(reply);
}
