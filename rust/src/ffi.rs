use crate::actuator::{PhaseActuator, Submission};
use crate::phase::{self, PhaseController, PhaseError, PhasePolicy};
use crate::timing::TimingSnapshot;
use serde::de::DeserializeOwned;
use serde_json::{json, Value};
use std::ffi::{c_char, c_void, CStr, CString};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::Mutex;

pub fn string(value: String) -> *mut c_char {
    CString::new(value)
        .expect("JSON contains no nul")
        .into_raw()
}
/// # Safety
/// `value` must be null or an unreleased string returned by this module; release it exactly once and do not access it afterward.
#[no_mangle]
pub unsafe extern "C" fn bcamsync_rust_string_free(value: *mut c_char) {
    if !value.is_null() {
        drop(CString::from_raw(value));
    }
}
pub fn json_call(f: impl FnOnce() -> Result<Value, PhaseError>) -> *mut c_char {
    let value = match catch_unwind(AssertUnwindSafe(f)) {
        Ok(Ok(v)) => json!({"ok":v}),
        Ok(Err(e)) => json!({"error":e.message,"kind":e.kind}),
        Err(_) => json!({"error":"Rust core panic","kind":2}),
    };
    string(value.to_string())
}
/// # Safety
/// A non-null `value` must reference a NUL-terminated string that remains readable and unchanged for the returned borrow's lifetime.
pub unsafe fn text<'a>(value: *const c_char) -> Result<&'a str, PhaseError> {
    if value.is_null() {
        return Err(PhaseError {
            kind: 1,
            message: "null FFI input".into(),
        });
    }
    CStr::from_ptr(value).to_str().map_err(|e| PhaseError {
        kind: 1,
        message: e.to_string(),
    })
}
fn decode<T: DeserializeOwned>(value: Value) -> Result<T, PhaseError> {
    serde_json::from_value(value).map_err(|e| PhaseError {
        kind: 1,
        message: e.to_string(),
    })
}
/// # Safety
/// Input strings must remain readable and NUL-terminated for this call. Stateful operations require exclusively borrowed writable handle storage and a live phase handle; `new`/`clone` require empty storage, and a clone source must remain live and unmodified. Free each created handle with `bcamsync_phase_free` and returned string with `bcamsync_rust_string_free`.
#[no_mangle]
pub unsafe extern "C" fn bcamsync_phase_call(
    handle: *mut *mut c_void,
    operation: *const c_char,
    input: *const c_char,
) -> *mut c_char {
    json_call(|| {
        let op = text(operation)?;
        let v: Value = serde_json::from_str(text(input)?).map_err(|e| PhaseError {
            kind: 1,
            message: e.to_string(),
        })?;
        let policy =
            || decode::<PhasePolicy>(v.get("policy").cloned().unwrap_or_else(|| json!({})));
        if op == "validate_policy" {
            policy()?.validate()?;
            return Ok(Value::Null);
        }
        if op == "validate_timing" {
            phase::validate_timing(
                &decode(v["snapshot"].clone())?,
                decode(v["now"].clone())?,
                &policy()?,
            )?;
            return Ok(Value::Null);
        }
        if op == "estimate" {
            return Ok(serde_json::to_value(phase::estimate_phase(
                &decode(v["master"].clone())?,
                &decode(v["slave"].clone())?,
                decode(v["now"].clone())?,
                &policy()?,
                decode(v["after"].clone())?,
                decode(v["previous"].clone())?,
                decode(v["nominal"].clone())?,
            )?)
            .unwrap());
        }
        if handle.is_null() {
            return Err(PhaseError {
                kind: 1,
                message: "null phase handle".into(),
            });
        }
        if op == "new" {
            *handle = Box::into_raw(Box::new(PhaseController::new(
                policy()?,
                decode(v["nominal"].clone())?,
            )?))
            .cast();
            return Ok(Value::Null);
        }
        if op == "clone" {
            let source = decode::<usize>(v["source"].clone())? as *const PhaseController;
            let source = source.as_ref().ok_or_else(|| PhaseError {
                kind: 1,
                message: "null phase controller".into(),
            })?;
            *handle = Box::into_raw(Box::new(source.clone())).cast();
            return Ok(Value::Null);
        }
        if op == "ready" {
            let controller = (*handle as *const PhaseController)
                .as_ref()
                .ok_or_else(|| PhaseError {
                    kind: 1,
                    message: "null phase controller".into(),
                })?;
            return Ok(json!(controller.ready()));
        }
        let controller = (*handle as *mut PhaseController)
            .as_mut()
            .ok_or_else(|| PhaseError {
                kind: 1,
                message: "null phase controller".into(),
            })?;
        match op {
            "reset" => {
                controller.reset(decode(v["mono"].clone())?, decode(v["after"].clone())?)?;
                Ok(Value::Null)
            }
            "tick" => Ok(serde_json::to_value(controller.tick(
                &decode::<TimingSnapshot>(v["master"].clone())?,
                &decode::<TimingSnapshot>(v["slave"].clone())?,
                decode(v["now"].clone())?,
                decode(v["mono"].clone())?,
                decode(v["clock_valid"].clone())?,
            ))
            .unwrap()),
            _ => Err(PhaseError {
                kind: 1,
                message: "unknown phase operation".into(),
            }),
        }
    })
}
/// # Safety
/// `handle` must be null or an unreleased phase handle. No calls or borrows may remain active, and the handle cannot be used after release.
#[no_mangle]
pub unsafe extern "C" fn bcamsync_phase_free(handle: *mut c_void) {
    if !handle.is_null() {
        drop(Box::from_raw(handle as *mut PhaseController));
    }
}
#[no_mangle]
pub extern "C" fn bcamsync_actuator_new() -> *mut c_void {
    Box::into_raw(Box::new(Mutex::new(PhaseActuator::default()))).cast()
}
/// # Safety
/// `handle` must be null or an unreleased actuator handle. Join all callers before release and never use the handle afterward.
#[no_mangle]
pub unsafe extern "C" fn bcamsync_actuator_free(handle: *mut c_void) {
    if !handle.is_null() {
        drop(Box::from_raw(handle as *mut Mutex<PhaseActuator>));
    }
}
/// # Safety
/// `handle` must be null or a live actuator handle for this call. Input strings must remain readable and NUL-terminated. Release the returned string with `bcamsync_rust_string_free`.
#[no_mangle]
pub unsafe extern "C" fn bcamsync_actuator_call(
    handle: *mut c_void,
    operation: *const c_char,
    input: *const c_char,
) -> *mut c_char {
    json_call(|| {
        let op = text(operation)?;
        let v: Value = serde_json::from_str(text(input)?).map_err(|e| PhaseError {
            kind: 2,
            message: e.to_string(),
        })?;
        let holder = (handle as *mut Mutex<PhaseActuator>)
            .as_ref()
            .ok_or_else(|| PhaseError {
                kind: 2,
                message: "null actuator handle".into(),
            })?;
        let mut a = holder.lock().map_err(|_| PhaseError {
            kind: 2,
            message: "poisoned actuator".into(),
        })?;
        let s = || decode::<String>(v["session"].clone());
        let r = || decode::<u64>(v["round"].clone());
        let result = match op {
            "snapshot" => Ok(a.state.clone()),
            "bind" => a.bind(&s()?, r()?),
            "query" => a.query(&s()?, r()?, decode(v["id"].clone())?),
            "cancel" => a.cancel(&s()?, r()?),
            "adjust" => a.adjust(
                &s()?,
                r()?,
                decode(v["id"].clone())?,
                decode(v["delta"].clone())?,
                decode(v["count"].clone())?,
                decode(v["expires"].clone())?,
                decode(v["now"].clone())?,
                decode(v["mono"].clone())?,
                decode(v["baseline"].clone())?,
                decode(v["minimum"].clone())?,
                decode(v["maximum"].clone())?,
            ),
            _ => Err("unknown actuator operation".into()),
        }
        .map_err(|message| PhaseError { kind: 2, message })?;
        Ok(serde_json::to_value(result).unwrap())
    })
}
fn actuator_fast(f: impl FnOnce() -> Result<(), ()>) -> i32 {
    match catch_unwind(AssertUnwindSafe(f)) {
        Ok(Ok(())) => 0,
        _ => -1,
    }
}
/// # Safety
/// `handle` must be null or live for this call; `output` must be null or aligned, exclusively writable storage for one `Submission`.
#[no_mangle]
pub unsafe extern "C" fn bcamsync_actuator_next(
    handle: *mut c_void,
    baseline: i64,
    mono: i64,
    output: *mut Submission,
) -> i32 {
    actuator_fast(|| {
        let a = (handle as *mut Mutex<PhaseActuator>).as_ref().ok_or(())?;
        let out = output.as_mut().ok_or(())?;
        *out = a.lock().map_err(|_| ())?.next(baseline, mono);
        Ok(())
    })
}
/// # Safety
/// `handle` must be null or a live actuator handle that cannot be freed during this call.
#[no_mangle]
pub unsafe extern "C" fn bcamsync_actuator_completed(
    handle: *mut c_void,
    submitted: Submission,
    sequence: u64,
    timestamp: i64,
) -> i32 {
    actuator_fast(|| {
        let a = (handle as *mut Mutex<PhaseActuator>).as_ref().ok_or(())?;
        a.lock()
            .map_err(|_| ())?
            .completed(submitted, sequence, timestamp);
        Ok(())
    })
}
/// # Safety
/// `handle` must be null or a live actuator handle that cannot be freed during this call.
#[no_mangle]
pub unsafe extern "C" fn bcamsync_actuator_cancel_active(handle: *mut c_void) -> i32 {
    actuator_fast(|| {
        let a = (handle as *mut Mutex<PhaseActuator>).as_ref().ok_or(())?;
        a.lock().map_err(|_| ())?.cancel_active();
        Ok(())
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    unsafe fn returned(ptr: *mut c_char) -> Value {
        let v = serde_json::from_str(CStr::from_ptr(ptr).to_str().unwrap()).unwrap();
        bcamsync_rust_string_free(ptr);
        v
    }
    #[test]
    fn ffi_owns_outputs_and_reports_invalid_handles() {
        unsafe {
            let op = CString::new("bind").unwrap();
            let input = CString::new(r#"{"session":"session","round":1}"#).unwrap();
            let result = returned(bcamsync_actuator_call(
                std::ptr::null_mut(),
                op.as_ptr(),
                input.as_ptr(),
            ));
            assert!(result["error"].is_string());
            for _ in 0..1000 {
                let handle = bcamsync_actuator_new();
                let result = returned(bcamsync_actuator_call(handle, op.as_ptr(), input.as_ptr()));
                assert_eq!(result["ok"]["round_id"], 1);
                let mut submission = Submission::default();
                assert_eq!(bcamsync_actuator_next(handle, 25000, 0, &mut submission), 0);
                assert_eq!(submission.duration_us, 25000);
                bcamsync_actuator_free(handle);
            }
            let create = CString::new("new").unwrap();
            let config = CString::new(r#"{"nominal":25000000}"#).unwrap();
            let clone = CString::new("clone").unwrap();
            for _ in 0..1000 {
                let mut handle = std::ptr::null_mut();
                let result = returned(bcamsync_phase_call(
                    &mut handle,
                    create.as_ptr(),
                    config.as_ptr(),
                ));
                assert!(result.get("ok").is_some());
                let source = CString::new(json!({"source":handle as usize}).to_string()).unwrap();
                let mut copy = std::ptr::null_mut();
                let result = returned(bcamsync_phase_call(
                    &mut copy,
                    clone.as_ptr(),
                    source.as_ptr(),
                ));
                assert!(result.get("ok").is_some());
                bcamsync_phase_free(copy);
                bcamsync_phase_free(handle);
            }
            bcamsync_rust_string_free(std::ptr::null_mut());
            bcamsync_actuator_free(std::ptr::null_mut());
        }
    }
}
