const std = @import("std");
const builtin = @import("builtin");
const kernel_mod = @import("kernel.zig");
const allocator = if (builtin.is_test) std.testing.allocator else std.heap.c_allocator;
const Kernel = kernel_mod.Kernel;
pub const Buffer = extern struct { ptr: ?[*]u8 = null, len: usize = 0 };
const header_len = @sizeOf(usize);
export fn csl_kernel_abi_version() u32 {
    return 1;
}
export fn csl_kernel_open(out: ?*?*Kernel) i32 {
    const output = out orelse return 1;
    output.* = null;
    const k = allocator.create(Kernel) catch return 2;
    k.* = .{};
    output.* = k;
    return 0;
}
export fn csl_kernel_close(kernel: ?*Kernel) void {
    if (kernel) |k| allocator.destroy(k);
}
fn payload(query: ?[*]const u8, len: usize) []const u8 {
    return if (len == 0) "" else query.?[0..len];
}
fn status(err: anyerror) i32 {
    return if (err == error.OutOfMemory) 2 else 3;
}
export fn csl_kernel_execute(kernel: ?*Kernel, query: ?[*]const u8, len: usize, out: ?*Buffer) i32 {
    const output = out orelse return 1;
    output.* = .{};
    if (kernel == null or (len > 0 and query == null)) return 1;
    var arena = std.heap.ArenaAllocator.init(allocator);
    defer arena.deinit();
    const result = kernel_mod.execute(arena.allocator(), payload(query, len)) catch |e| return status(e);
    if (result.len == 0) return 0;
    const total = std.math.add(usize, header_len, result.len) catch return 2;
    const allocation = allocator.alloc(u8, total) catch return 2;
    std.mem.writeInt(usize, allocation[0..header_len], total, .little);
    @memcpy(allocation[header_len..], result);
    output.* = .{ .ptr = allocation.ptr + header_len, .len = result.len };
    return 0;
}
export fn csl_buffer_release(buffer: Buffer) void {
    if (buffer.ptr) |p| {
        const start = p - header_len;
        const total = std.mem.readInt(usize, start[0..header_len], .little);
        allocator.free(start[0..total]);
    }
}
export fn csl_kernel_execute_into(kernel: ?*Kernel, query: ?[*]const u8, len: usize, result: ?[*]u8, capacity: usize, result_len: ?*usize) i32 {
    const written = result_len orelse return 1;
    written.* = 0;
    if (kernel == null or (len > 0 and query == null) or (capacity > 0 and result == null)) return 1;
    var arena = std.heap.ArenaAllocator.init(allocator);
    defer arena.deinit();
    const bytes = kernel_mod.execute(arena.allocator(), payload(query, len)) catch |e| return status(e);
    written.* = bytes.len;
    if (capacity < bytes.len) return 4;
    if (bytes.len > 0) @memcpy(result.?[0..bytes.len], bytes);
    return 0;
}
test "ABI version, null, empty, errors, ownership and B bounds" {
    const t = std.testing;
    try t.expectEqual(@as(u32, 1), csl_kernel_abi_version());
    csl_kernel_close(null);
    csl_buffer_release(.{});
    try t.expectEqual(@as(i32, 1), csl_kernel_open(null));
    var k: ?*Kernel = null;
    try t.expectEqual(@as(i32, 0), csl_kernel_open(&k));
    var buf = Buffer{};
    try t.expectEqual(@as(i32, 1), csl_kernel_execute(null, null, 0, &buf));
    try t.expectEqual(@as(i32, 1), csl_kernel_execute(k, null, 1, &buf));
    try t.expectEqual(@as(i32, 1), csl_kernel_execute(k, null, 0, null));
    try t.expectEqual(@as(i32, 0), csl_kernel_execute(k, null, 0, &buf));
    csl_buffer_release(buf);
    try t.expectEqual(@as(i32, 3), csl_kernel_execute(k, "\x01bad", 4, &buf));
    for (0..100) |_| {
        try t.expectEqual(@as(i32, 0), csl_kernel_execute(k, "\x00hello", 6, &buf));
        try t.expectEqualStrings("hello", buf.ptr.?[0..buf.len]);
        csl_buffer_release(buf);
        var output: [5]u8 = undefined;
        var n: usize = 0;
        try t.expectEqual(@as(i32, 4), csl_kernel_execute_into(k, "\x00hello", 6, null, 0, &n));
        try t.expectEqual(@as(usize, 5), n);
        @memset(&output, 99);
        try t.expectEqual(@as(i32, 4), csl_kernel_execute_into(k, "\x00hello", 6, &output, 4, &n));
        try t.expectEqual(@as(u8, 99), output[0]);
        try t.expectEqual(@as(i32, 0), csl_kernel_execute_into(k, "\x00hello", 6, &output, 5, &n));
        try t.expectEqualStrings("hello", &output);
        try t.expectEqual(@as(i32, 1), csl_kernel_execute_into(k, null, 1, &output, 5, &n));
        try t.expectEqual(@as(i32, 1), csl_kernel_execute_into(k, null, 0, null, 1, &n));
        try t.expectEqual(@as(i32, 1), csl_kernel_execute_into(k, null, 0, null, 0, null));
    }
    const large = try allocator.alloc(u8, 16 * 1024 * 1024 + 1);
    defer allocator.free(large);
    @memset(large, 42);
    large[0] = 0;
    try t.expectEqual(@as(i32, 0), csl_kernel_execute(k, large.ptr, large.len, &buf));
    csl_kernel_close(k); // Result lifetime is independent of kernel lifetime.
    try t.expectEqualSlices(u8, large[1..], buf.ptr.?[0..buf.len]);
    csl_buffer_release(buf);
}
