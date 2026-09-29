const std = @import("std");
pub fn build(b: *std.Build) void {
    const target = b.standardTargetOptions(.{});
    const optimize = b.standardOptimizeOption(.{});
    // The pure-Zig S0 session module. It imports semantic.zig by relative path, and a file can belong to
    // only one module, so the hybrid reaches the semantic types as `@import("session").sem`.
    const session = b.createModule(.{ .root_source_file = b.path("../../zig/src/session.zig"), .target = target, .optimize = optimize, .link_libc = true });
    const root = b.createModule(.{ .root_source_file = b.path("src/lib.zig"), .target = target, .optimize = optimize, .link_libc = true });
    root.addImport("session", session);
    const lib = b.addLibrary(.{ .name = "csl_kernel", .linkage = .static, .root_module = root });
    lib.bundle_compiler_rt = true;
    b.installArtifact(lib);
    const tests = b.addTest(.{ .root_module = root });
    b.step("test", "Run ABI tests with testing allocator").dependOn(&b.addRunArtifact(tests).step);
}
