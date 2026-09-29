//! Root of the static library: the stable v1 kernel ABI (abi.zig) plus the experimental persistent
//! session ABI (session_abi.zig). Kept as a separate root so abi.zig itself stays untouched.
comptime {
    _ = @import("abi.zig");
    _ = @import("session_abi.zig");
}
test {
    _ = @import("abi.zig");
    _ = @import("session_abi.zig");
}
