const std = @import("std");
pub fn build(b: *std.Build) void {
    const target = b.standardTargetOptions(.{});
    const optimize = b.standardOptimizeOption(.{});
    const semantic = b.createModule(.{ .root_source_file = b.path("../../zig/src/semantic.zig"), .target = target, .optimize = optimize });
    const root = b.createModule(.{ .root_source_file = b.path("src/abi.zig"), .target = target, .optimize = optimize, .link_libc = true });
    root.addImport("semantic", semantic);
    const lib = b.addLibrary(.{ .name = "csl_kernel", .linkage = .static, .root_module = root });
    lib.bundle_compiler_rt = true;
    b.installArtifact(lib);
    const tests = b.addTest(.{ .root_module = root });
    b.step("test", "Run ABI tests with testing allocator").dependOn(&b.addRunArtifact(tests).step);
}
