//! Validation-only fixture: prove Rust accesses and standard-library synchronization are visible to TSan.
use std::sync::{Arc, Barrier, Mutex};

static mut UNGUARDED: usize = 0;

fn main() {
    let mode = std::env::args().nth(1).expect("provide race or mutex");
    let barrier = Arc::new(Barrier::new(2));
    if mode == "race" {
        std::thread::scope(|scope| {
            let start = barrier.clone();
            scope.spawn(move || {
                start.wait();
                for value in 0..100_000 {
                    // Deliberate test-only race; a successful sanitizer must reject this process.
                    unsafe { std::ptr::write_volatile(std::ptr::addr_of_mut!(UNGUARDED), value) };
                }
            });
            barrier.wait();
            for value in 0..100_000 {
                unsafe { std::ptr::write_volatile(std::ptr::addr_of_mut!(UNGUARDED), value) };
            }
        });
    } else {
        assert_eq!(mode, "mutex");
        let counter = Arc::new(Mutex::new(0_usize));
        std::thread::scope(|scope| {
            let start = barrier.clone();
            let worker_counter = counter.clone();
            scope.spawn(move || {
                start.wait();
                for _ in 0..10_000 {
                    *worker_counter.lock().unwrap() += 1;
                }
            });
            barrier.wait();
            for _ in 0..10_000 {
                *counter.lock().unwrap() += 1;
            }
        });
        assert_eq!(*counter.lock().unwrap(), 20_000);
    }
}
