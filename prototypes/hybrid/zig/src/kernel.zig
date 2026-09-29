const std = @import("std");
const sem = @import("session").sem;
pub const Kernel = struct { reserved: u8 = 0 };
// Bootstrap protocol: empty=no-op, 0x00 + bytes=echo, 0x01 + JSON=semantic batch.
// Echo is explicitly not a CSL query or a semantic benchmark.
pub fn execute(a: std.mem.Allocator, query: []const u8) ![]const u8 {
    if (query.len == 0) return "";
    if (query[0] == 0) return query[1..];
    if (query[0] != 1) return error.InvalidQuery;
    const request = try std.json.parseFromSlice(struct { fixture: sem.Fixture, query: sem.Query }, a, query[1..], .{});
    const store = try sem.Store.init(a, request.value.fixture);
    return store.execute(request.value.query);
}
