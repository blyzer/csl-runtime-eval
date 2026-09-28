const std = @import("std");
pub fn build(b: *std.Build) void {
    const target = b.standardTargetOptions(.{});
    const optimize = b.standardOptimizeOption(.{});
    const exe = b.addExecutable(.{ .name = "csl-eval-zig", .root_module = b.createModule(.{ .root_source_file = b.path("src/main.zig"), .link_libc = true, .target = target, .optimize = optimize }) });
    b.installArtifact(exe);
    const tests = b.addTest(.{ .root_source_file = b.path("src/semantic.zig"), .target = target, .optimize = optimize });
    b.step("test", "Run semantic unit tests").dependOn(&b.addRunArtifact(tests).step);
}
