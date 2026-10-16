use crate::config::SyncConfig;
use crate::runtime::{Host, Runtime};
use serde_json::{json, Value};
use std::ffi::{c_char, c_void, CStr, CString};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::time::Duration;

type HostCall = unsafe extern "C" fn(*mut c_void, *const c_char) -> *mut c_char;
type HostRelease = unsafe extern "C" fn(*mut c_void, *mut c_char);
struct CallbackHost {
    context: *mut c_void,
    call: HostCall,
    release: HostRelease,
}
// The C++ host retains context and adapters until its worker has stopped and joined,
// then destroys Runtime before releasing them. Host calls occur on that worker only.
unsafe impl Send for CallbackHost {}
unsafe impl Sync for CallbackHost {}
struct HostOutput<'a> {
    host: &'a CallbackHost,
    pointer: *mut c_char,
}
impl Drop for HostOutput<'_> {
    fn drop(&mut self) {
        unsafe { (self.host.release)(self.host.context, self.pointer) };
    }
}
impl Host for CallbackHost {
    fn call(&self, request: Value) -> Result<Value, String> {
        let input = CString::new(request.to_string()).map_err(|e| e.to_string())?;
        // Host exceptions are caught by the C++ trampoline. The release callback is same-side.
        let output = unsafe { (self.call)(self.context, input.as_ptr()) };
        if output.is_null() {
            return Err("host callback returned no response".into());
        }
        let output = HostOutput {
            host: self,
            pointer: output,
        };
        let parsed =
            unsafe { serde_json::from_slice::<Value>(CStr::from_ptr(output.pointer).to_bytes()) };
        let response = parsed.map_err(|e| e.to_string())?;
        if response["ok"].as_bool() != Some(true) {
            return Err(response["error"]
                .as_str()
                .unwrap_or("host callback failed")
                .into());
        }
        Ok(response["result"].clone())
    }
}
fn allocated(value: Value) -> *mut c_char {
    // serde_json escapes embedded NUL bytes, so serialized values always fit CString.
    CString::new(value.to_string()).unwrap().into_raw()
}
fn parse(input: *const c_char) -> Result<Value, String> {
    if input.is_null() {
        return Err("missing runtime request".into());
    }
    unsafe { serde_json::from_slice(CStr::from_ptr(input).to_bytes()) }.map_err(|e| e.to_string())
}
fn guarded(operation: impl FnOnce() -> Result<Value, String>) -> Value {
    match catch_unwind(AssertUnwindSafe(operation)) {
        Ok(Ok(result)) => json!({"ok": true, "result": result}),
        Ok(Err(error)) => json!({"ok": false, "error": error}),
        Err(_) => json!({"ok": false, "error": "Rust synchronization runtime panicked"}),
    }
}
/// # Safety
/// `input` must remain readable and NUL-terminated for this call; `error` must be null or exclusively writable pointer storage. The host context and callbacks must remain valid until runtime destruction. Callbacks must not unwind and must return null or a readable NUL-terminated allocation that `release` can free. Release an error string with `bcamsync_runtime_release`.
#[no_mangle]
pub unsafe extern "C" fn bcamsync_runtime_create(
    input: *const c_char,
    context: *mut c_void,
    call: Option<HostCall>,
    release: Option<HostRelease>,
    error: *mut *mut c_char,
) -> *mut Runtime {
    let result = catch_unwind(AssertUnwindSafe(|| -> Result<Runtime, String> {
        let request = parse(input)?;
        let config: SyncConfig =
            serde_json::from_value(request["config"].clone()).map_err(|e| e.to_string())?;
        let local = request["local"]
            .as_str()
            .ok_or("missing local node")?
            .to_owned();
        let identity = request["identity"]
            .as_str()
            .ok_or("missing control identity")?
            .to_owned();
        let units = serde_json::from_value(request["units"].clone()).map_err(|e| e.to_string())?;
        let host = CallbackHost {
            context,
            call: call.ok_or("missing host callback")?,
            release: release.ok_or("missing host release")?,
        };
        Runtime::new(config, local, identity, units, Box::new(host))
    }));
    match result {
        Ok(Ok(runtime)) => Box::into_raw(Box::new(runtime)),
        failure => {
            let reason = match failure {
                Ok(Err(error)) => error,
                _ => "Rust synchronization runtime panicked".into(),
            };
            if !error.is_null() {
                unsafe {
                    *error = allocated(json!({"ok": false, "error": reason}));
                }
            }
            std::ptr::null_mut()
        }
    }
}
/// # Safety
/// `runtime` must be null or a live handle returned by `bcamsync_runtime_create`. Stop and join its worker and all other callers before destruction, then never access the handle again.
#[no_mangle]
pub unsafe extern "C" fn bcamsync_runtime_destroy(runtime: *mut Runtime) {
    if !runtime.is_null() {
        let _ = catch_unwind(AssertUnwindSafe(|| unsafe {
            drop(Box::from_raw(runtime));
        }));
    }
}
/// # Safety
/// `runtime` must remain live and `input` readable and NUL-terminated throughout the call. Its owner must serialize begin/run/finish and address changes; host operations must run on the owning worker. Stop/status/clock/wait calls may run concurrently. Release the returned string with `bcamsync_runtime_release`.
#[no_mangle]
pub unsafe extern "C" fn bcamsync_runtime_call(
    runtime: *mut Runtime,
    input: *const c_char,
) -> *mut c_char {
    allocated(guarded(|| {
        let runtime = unsafe { runtime.as_ref() }.ok_or("missing synchronization runtime")?;
        let request = parse(input)?;
        match request["op"].as_str().ok_or("missing runtime operation")? {
            "begin" => runtime.begin(),
            "set_node_addresses" => {
                runtime.set_node_addresses(
                    serde_json::from_value(request["addresses"].clone())
                        .map_err(|e| e.to_string())?,
                )?;
                Ok(Value::Null)
            }
            "run" => {
                runtime.run();
                Ok(Value::Null)
            }
            "finish" => {
                runtime.finish();
                Ok(Value::Null)
            }
            "request_stop" => {
                runtime.request_stop();
                Ok(Value::Null)
            }
            "stopping" => Ok(json!(runtime.stopping())),
            "wait_for_stop" => {
                runtime.wait_for_stop();
                Ok(Value::Null)
            }
            "fail" => {
                runtime.fail(request["reason"].as_str().ok_or("missing failure reason")?);
                Ok(Value::Null)
            }
            "status" => Ok(json!(runtime.status())),
            "clock" => Ok(json!(runtime.clock())),
            "wait_ready" => {
                let millis = request["milliseconds"]
                    .as_i64()
                    .ok_or("invalid readiness timeout")?;
                Ok(json!(
                    runtime.wait_ready(Duration::from_millis(millis.max(0) as u64))?
                ))
            }
            _ => Err("unknown runtime operation".into()),
        }
    }))
}
/// # Safety
/// `response` must be null or an unreleased string returned by this runtime ABI. Release it once and do not access it afterward.
#[no_mangle]
pub unsafe extern "C" fn bcamsync_runtime_release(response: *mut c_char) {
    if !response.is_null() {
        unsafe {
            drop(CString::from_raw(response));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    unsafe extern "C" fn invalid_response(_: *mut c_void, _: *const c_char) -> *mut c_char {
        CString::new("invalid-json").unwrap().into_raw()
    }
    unsafe extern "C" fn release_response(context: *mut c_void, response: *mut c_char) {
        let released = unsafe { &*(context as *const AtomicUsize) };
        released.fetch_add(1, Ordering::Relaxed);
        unsafe {
            drop(CString::from_raw(response));
        }
    }
    #[test]
    fn malformed_host_response_is_released() {
        let released = AtomicUsize::new(0);
        let host = CallbackHost {
            context: &released as *const _ as *mut _,
            call: invalid_response,
            release: release_response,
        };
        assert!(host.call(json!({"op": "snapshot"})).is_err());
        assert_eq!(released.load(Ordering::Relaxed), 1);
    }
}
