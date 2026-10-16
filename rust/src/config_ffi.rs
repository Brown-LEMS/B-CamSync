use crate::{
    config::SyncConfig,
    ffi,
    phase::{PhaseError, PhasePolicy},
};
use serde_json::Value;
use std::ffi::CStr;
use std::os::raw::c_char;

fn input(pointer: *const c_char) -> Result<String, PhaseError> {
    if pointer.is_null() {
        return Err(PhaseError {
            kind: 1,
            message: "null configuration".into(),
        });
    }
    // The ABI caller lends a NUL-terminated UTF-8 string for this call only.
    unsafe { CStr::from_ptr(pointer) }
        .to_str()
        .map(str::to_owned)
        .map_err(|error| PhaseError {
            kind: 1,
            message: error.to_string(),
        })
}
/// # Safety
/// `pointer` must be null or readable NUL-terminated UTF-8 for this call. Release the returned string with `bcamsync_rust_string_free`.
#[no_mangle]
pub unsafe extern "C" fn bcamsync_rust_config(
    pointer: *const c_char,
    operation: i32,
) -> *mut c_char {
    ffi::json_call(|| {
        let text = input(pointer)?;
        if operation == 2 {
            let value: Value = serde_json::from_str(&text).map_err(|e| PhaseError {
                kind: 1,
                message: e.to_string(),
            })?;
            if !value.is_object() {
                return Err(PhaseError {
                    kind: 1,
                    message: "phase policy must be an object".into(),
                });
            }
            let policy: PhasePolicy = serde_json::from_value(value).map_err(|e| PhaseError {
                kind: 1,
                message: e.to_string(),
            })?;
            policy.validate()?;
            return serde_json::to_value(policy).map_err(|e| PhaseError {
                kind: 1,
                message: e.to_string(),
            });
        }
        let config: SyncConfig = serde_json::from_str(&text).map_err(|e| PhaseError {
            kind: 1,
            message: e.to_string(),
        })?;
        config.validate().map_err(|e| PhaseError {
            kind: 1,
            message: e,
        })?;
        match operation {
            0 => serde_json::to_value(config).map_err(|e| PhaseError {
                kind: 1,
                message: e.to_string(),
            }),
            1 => Ok(config.topology()),
            _ => Err(PhaseError {
                kind: 1,
                message: "unknown configuration operation".into(),
            }),
        }
    })
}
/// # Safety
/// `output` must be null or aligned writable storage for one `i64`, exclusively borrowed for this call.
#[no_mangle]
pub unsafe extern "C" fn bcamsync_rust_clock_ns(kind: i32, output: *mut i64) -> i32 {
    if output.is_null() {
        return 0;
    }
    match std::panic::catch_unwind(|| match kind {
        0 => crate::config::monotonic_ns(),
        1 => crate::config::boot_time_ns(),
        2 => crate::config::realtime_ns(),
        _ => panic!("invalid clock kind"),
    }) {
        Ok(value) => {
            unsafe {
                *output = value;
            }
            1
        }
        Err(_) => 0,
    }
}
#[no_mangle]
pub extern "C" fn bcamsync_rust_map_time(valid: i32, offset: i64, local: i64) -> *mut c_char {
    ffi::json_call(|| {
        if valid == 0 {
            return Ok(Value::from(0));
        }
        local
            .checked_add(offset)
            .map(Value::from)
            .ok_or_else(|| PhaseError {
                kind: 4,
                message: "clock mapping exceeds timestamp range".into(),
            })
    })
}
