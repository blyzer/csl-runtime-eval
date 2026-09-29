const std = @import("std");
pub fn build(b: *std.Build) void {
    const target = b.standardTargetOptions(.{});
    const optimize = b.standardOptimizeOption(.{});
    const exe = b.addExecutable(.{ .name = "csl-eval-zig", .root_module = b.createModule(.{ .root_source_file = b.path("src/main.zig"), .link_libc = true, .target = target, .optimize = optimize }) });
    b.installArtifact(exe);
    const tests = b.addTest(.{ .root_source_file = b.path("src/semantic.zig"), .target = target, .optimize = optimize, .link_libc = true });
    // session.zig uses std.heap.c_allocator, which needs libc on Linux (macOS links it implicitly).
    const session_tests = b.addTest(.{ .root_source_file = b.path("src/session.zig"), .target = target, .optimize = optimize, .link_libc = true });
    const test_step = b.step("test", "Run semantic and session unit tests");
    test_step.dependOn(&b.addRunArtifact(tests).step);
    test_step.dependOn(&b.addRunArtifact(session_tests).step);
}
