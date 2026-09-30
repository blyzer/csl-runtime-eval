use std::{env, path::PathBuf, process::Command};
fn main() {
    for p in [
        "../zig/src",
        "../zig/build.zig",
        "../abi",
        "../../zig/src/semantic.zig",
        "../../zig/src/session.zig",
    ] {
        println!("cargo:rerun-if-changed={p}");
    }
    println!("cargo:rerun-if-env-changed=ZIG");
    println!("cargo:rerun-if-env-changed=CSL_ZIG_CACHE_DIR");
    println!("cargo:rerun-if-env-changed=CSL_ZIG_GLOBAL_CACHE_DIR");
    let dir = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").unwrap()).join("../zig");
    let out = PathBuf::from(env::var_os("OUT_DIR").unwrap());
    let zig = env::var_os("ZIG").unwrap_or_else(|| "zig".into());
    let installed = out.join("lib/libcsl_kernel.a");
    if installed.exists() {
        std::fs::remove_file(&installed).expect("remove previous generated archive");
    }
    let mut command = Command::new(zig);
    command
        .current_dir(&dir)
        .args(["build", "-Doptimize=ReleaseFast", "--prefix"])
        .arg(&out);
    if let Some(path) = env::var_os("CSL_ZIG_CACHE_DIR") {
        command.args(["--cache-dir"]).arg(path);
    }
    if let Some(path) = env::var_os("CSL_ZIG_GLOBAL_CACHE_DIR") {
        command.args(["--global-cache-dir"]).arg(path);
    }
    let status = command
        .status()
        .expect("Zig is required: install 0.14.1 or set ZIG");
    assert!(status.success(), "Zig kernel compilation failed");
    // Apple's recent linker requires 8-byte archive member alignment.
    // Zig 0.14's archive writer can emit compiler_rt members at 2-byte alignment.
    // Repack with the host archiver; no object code is changed.
    if env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("macos") {
        let library = out.join("lib/libcsl_kernel.a");
        let objects = out.join("repack");
        std::fs::create_dir_all(&objects).expect("create archive staging");
        let status = Command::new("/usr/bin/ar")
            .current_dir(&objects)
            .arg("-x")
            .arg(&library)
            .status()
            .expect("Apple ar required");
        assert!(status.success(), "extract kernel objects");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            for name in ["libcsl_kernel.a.o", "compiler_rt.o"] {
                std::fs::set_permissions(
                    objects.join(name),
                    std::fs::Permissions::from_mode(0o644),
                )
                .expect("set extracted object permissions");
            }
        }
        let repacked = out.join("lib/libcsl_kernel-aligned.a");
        let status = Command::new("/usr/bin/libtool")
            .args(["-static", "-o"])
            .arg(&repacked)
            .arg(objects.join("libcsl_kernel.a.o"))
            .arg(objects.join("compiler_rt.o"))
            .status()
            .expect("Apple libtool required");
        assert!(status.success(), "kernel archive alignment failed");
        std::fs::rename(repacked, library).expect("replace aligned archive");
    }
    println!(
        "cargo:rustc-link-search=native={}",
        out.join("lib").display()
    );
    println!("cargo:rustc-link-lib=static=csl_kernel");
    println!("cargo:rustc-link-lib=c");
}
