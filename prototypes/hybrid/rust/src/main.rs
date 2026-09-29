use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{ffi::c_void, ptr, slice, time::Instant};
#[repr(C)]
#[derive(Default)]
struct CslBuffer {
    ptr: *mut u8,
    len: usize,
}
unsafe extern "C" {
    fn csl_kernel_abi_version() -> u32;
    fn csl_kernel_open(out: *mut *mut c_void) -> i32;
    fn csl_kernel_close(kernel: *mut c_void);
    fn csl_kernel_execute(
        kernel: *mut c_void,
        query: *const u8,
        len: usize,
        out: *mut CslBuffer,
    ) -> i32;
    fn csl_buffer_release(buffer: CslBuffer);
    fn csl_kernel_execute_into(
        kernel: *mut c_void,
        query: *const u8,
        len: usize,
        result: *mut u8,
        capacity: usize,
        written: *mut usize,
    ) -> i32;
}
struct Kernel(*mut c_void);
impl Kernel {
    fn open() -> Result<Self, String> {
        let mut p = ptr::null_mut();
        // SAFETY: valid writable out pointer; the version query takes no arguments.
        unsafe {
            if csl_kernel_abi_version() != 1 {
                return Err("ABI mismatch".into());
            }
            let s = csl_kernel_open(&mut p);
            if s != 0 {
                return Err(format!("open status {s}"));
            }
        }
        Ok(Self(p))
    }
    fn execute(&self, q: &[u8]) -> Result<Vec<u8>, String> {
        let mut b = CslBuffer::default();
        // SAFETY: kernel is live, slices and output remain valid throughout the call.
        let s = unsafe { csl_kernel_execute(self.0, q.as_ptr(), q.len(), &mut b) };
        if s != 0 {
            return Err(format!("execute status {s}"));
        }
        struct Owned(CslBuffer);
        impl Drop for Owned {
            fn drop(&mut self) {
                let b = std::mem::take(&mut self.0);
                // SAFETY: this guard is the only owner; release transfers ownership back to Zig.
                unsafe { csl_buffer_release(b) };
            }
        }
        let owned = Owned(b);
        // SAFETY: successful ABI result is readable for len bytes until guard drop.
        Ok(if owned.0.len == 0 {
            Vec::new()
        } else {
            unsafe { slice::from_raw_parts(owned.0.ptr, owned.0.len) }.to_vec()
        })
    }
    fn execute_into(&self, q: &[u8], out: &mut [u8]) -> Result<usize, String> {
        let mut n = 0;
        // SAFETY: nonoverlapping borrowed slices; Zig retains no pointers.
        let s = unsafe {
            csl_kernel_execute_into(
                self.0,
                q.as_ptr(),
                q.len(),
                out.as_mut_ptr(),
                out.len(),
                &mut n,
            )
        };
        if s != 0 {
            return Err(format!("execute_into status {s}, required {n}"));
        }
        Ok(n)
    }
}
impl Drop for Kernel {
    fn drop(&mut self) {
        // SAFETY: this is the unique live kernel owner.
        unsafe { csl_kernel_close(self.0) };
    }
}
fn run() -> Result<Value, Box<dyn std::error::Error>> {
    let args: Vec<String> = std::env::args().collect();
    let cmd = args.get(1).map(String::as_str).unwrap_or("info");
    let arg = |key: &str| -> Result<&str, String> {
        args.windows(2)
            .find(|w| w[0] == key)
            .map(|w| w[1].as_str())
            .ok_or_else(|| format!("missing {key}"))
    };
    let k = Kernel::open()?;
    if cmd == "info" {
        return Ok(
            json!({"candidate":"hybrid","abi_version":1,"semantic_kernel":"zig","representation":"typed-hash-v1"}),
        );
    }
    if cmd == "echo" {
        let mut q = vec![0];
        q.extend_from_slice(args.get(2).map(String::as_bytes).unwrap_or_default());
        return Ok(json!({"echo":String::from_utf8(k.execute(&q)?)?}));
    }
    if cmd == "boundary" {
        let size: usize = arg("--size")?.parse()?;
        let repeats: usize = arg("--repeat")?.parse()?;
        if repeats == 0 {
            return Err("repeat must be positive".into());
        }
        let strategy = arg("--strategy")?;
        let mut q = vec![42; size + 1];
        q[0] = 0;
        let mut output = vec![0; size];
        let mut ns = Vec::new();
        let mut digest = String::new();
        for _ in 0..repeats {
            let start = Instant::now();
            match strategy {
                "A" => {
                    output = k.execute(&q)?;
                }
                "B" => {
                    k.execute_into(&q, &mut output)?;
                }
                "rust" => {
                    output = q[1..].to_vec();
                }
                _ => return Err("invalid strategy".into()),
            };
            ns.push(start.elapsed().as_nanos());
            digest = format!("sha256:{:x}", Sha256::digest(&output));
        }
        return Ok(
            json!({"strategy":strategy,"payload_bytes":size,"latency_ns":ns,"calls_per_query":if strategy=="A"{2}else if strategy=="B"{1}else{0},"bytes_copied":size*if strategy=="A"{2}else{1},"output_allocations_per_call":if size==0{0}else if strategy=="A"{2}else if strategy=="B"{0}else{1},"result_digest":digest,"cancellation":"NOT IMPLEMENTED"}),
        );
    }
    let workload = cmd == "workload";
    let fx: Value = serde_json::from_slice(&std::fs::read(arg(if workload {
        "--corpus"
    } else {
        "--fixture"
    })?)?)?;
    let q: Value = if cmd == "load" {
        json!({"schema":"csl.eval.query/v0.1","query_id":"load","op":"FILTER"})
    } else if workload {
        let id = arg("--id")?;
        if !["W1", "W2"].contains(&id) {
            return Err("invalid workload".into());
        }
        serde_json::from_str::<Value>(arg("--params")?)?["query"].clone()
    } else if cmd == "query" {
        serde_json::from_slice(&std::fs::read(arg("--query")?)?)?
    } else {
        return Err("unknown command".into());
    };
    let mut request = vec![1];
    request.extend(serde_json::to_vec(&json!({"fixture":fx,"query":q}))?);
    let result: Value = serde_json::from_slice(&k.execute(&request)?)?;
    if cmd == "load" {
        return Ok(
            json!({"entities":result["entities"].as_array().ok_or("invalid response")?.len(),"snapshot":result["snapshot"]}),
        );
    }
    Ok(result)
}
fn main() {
    match run() {
        Ok(v) => println!("{v}"),
        Err(e) => {
            eprintln!("{e}");
            std::process::exit(1);
        }
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn ffi_roundtrip() {
        let k = Kernel::open().unwrap();
        assert_eq!(k.execute(b"\0CSL Rust + Zig").unwrap(), b"CSL Rust + Zig");
        assert!(k.execute(b"\x01broken").is_err());
        let mut out = [0; 3];
        for _ in 0..100 {
            assert_eq!(k.execute_into(b"\0abc", &mut out).unwrap(), 3);
            assert_eq!(&out, b"abc");
        }
    }
}
