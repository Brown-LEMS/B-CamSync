use crate::timing::{Pulse, TimingSnapshot};
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug)]
pub struct PhaseError {
    pub kind: i32,
    pub message: String,
}
impl std::fmt::Display for PhaseError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}
impl std::error::Error for PhaseError {}
fn error(kind: i32, message: impl Into<String>) -> PhaseError {
    PhaseError {
        kind,
        message: message.into(),
    }
}
fn invalid(message: &str) -> PhaseError {
    error(1, message)
}
fn timing(message: &str) -> PhaseError {
    error(2, message)
}
fn pending(message: &str) -> PhaseError {
    error(3, message)
}
fn ns(seconds: f64) -> i64 {
    (seconds * 1e9).round() as i64
}
fn median(mut v: Vec<f64>) -> f64 {
    v.sort_by(f64::total_cmp);
    let n = v.len();
    if n % 2 == 1 {
        v[n / 2]
    } else {
        (v[n / 2 - 1] + v[n / 2]) / 2.0
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct PhasePolicy {
    pub window_seconds: f64,
    pub evaluation_seconds: f64,
    pub freshness_seconds: f64,
    pub ready_p95_ms: f64,
    pub ready_max_ms: f64,
    pub ready_windows: i32,
    pub startup_delta_us: i32,
    pub startup_frames: i32,
    pub tracking_delta_us: i32,
    pub excursion_ms: f64,
    pub excursion_timeout_seconds: f64,
    pub no_progress_corrections: i32,
    pub max_mapping_span_ns: i64,
    pub max_samples: usize,
}
impl Default for PhasePolicy {
    fn default() -> Self {
        Self {
            window_seconds: 2.0,
            evaluation_seconds: 1.0,
            freshness_seconds: 1.0,
            ready_p95_ms: 0.5,
            ready_max_ms: 1.0,
            ready_windows: 2,
            startup_delta_us: 1000,
            startup_frames: 16,
            tracking_delta_us: 200,
            excursion_ms: 2.0,
            excursion_timeout_seconds: 10.0,
            no_progress_corrections: 3,
            max_mapping_span_ns: 100000,
            max_samples: 512,
        }
    }
}
impl PhasePolicy {
    pub fn validate(&self) -> Result<(), PhaseError> {
        let p = self;
        for v in [
            p.window_seconds,
            p.evaluation_seconds,
            p.freshness_seconds,
            p.ready_p95_ms,
            p.ready_max_ms,
            p.excursion_ms,
            p.excursion_timeout_seconds,
        ] {
            if !v.is_finite() || v <= 0.0 || v > 1e9 {
                return Err(invalid(
                    "phase policy values must be finite and positive within timestamp range",
                ));
            }
        }
        if [
            p.ready_windows,
            p.startup_delta_us,
            p.startup_frames,
            p.tracking_delta_us,
            p.no_progress_corrections,
        ]
        .iter()
        .any(|&v| v <= 0)
        {
            return Err(invalid("phase policy counts must be positive"));
        }
        if p.max_mapping_span_ns <= 0
            || p.max_samples == 0
            || p.max_samples > 512
            || p.window_seconds > 6.0
        {
            return Err(invalid("phase policy exceeds controller bounds"));
        }
        if p.startup_delta_us > 1000 || p.startup_frames > 16 || p.tracking_delta_us > 200 {
            return Err(invalid("phase pulse exceeds hard bounds"));
        }
        if !(p.ready_p95_ms <= p.ready_max_ms && p.ready_max_ms < p.excursion_ms) {
            return Err(invalid("phase thresholds must be ordered"));
        }
        Ok(())
    }
}
fn check_frames(s: &TimingSnapshot, now: i64, p: &PhasePolicy) -> Result<(), PhaseError> {
    let f = &s.frames;
    if f.is_empty() {
        return Err(pending("empty timing window"));
    }
    if f.len() > p.max_samples {
        return Err(timing("timing window exceeds sample bound"));
    }
    if now <= 0 || s.snapshot_timestamp_ns <= 0 {
        return Err(timing("invalid common timestamp"));
    }
    let latest = f.last().unwrap().timestamp_ns;
    if latest <= 0
        || latest > s.snapshot_timestamp_ns
        || (now as i128 - latest as i128) > ns(p.freshness_seconds) as i128
        || (latest as i128 - now as i128) > p.max_mapping_span_ns as i128
    {
        return Err(timing("stale or future timing evidence"));
    }
    for (i, v) in f.iter().enumerate() {
        if v.timestamp_ns <= 0 || v.sensor_timestamp_ns <= 0 {
            return Err(timing("invalid frame timestamp"));
        }
        if v.mapping_span_ns < 0 || v.mapping_span_ns > p.max_mapping_span_ns {
            return Err(timing("clock mapping uncertainty exceeds bound"));
        }
        if i == 0 {
            continue;
        }
        let a = &f[i - 1];
        if a.camera_sequence == u64::MAX || v.camera_sequence != a.camera_sequence + 1 {
            return Err(timing("camera sequence discontinuity"));
        }
        if v.timestamp_ns <= a.timestamp_ns || v.sensor_timestamp_ns <= a.sensor_timestamp_ns {
            return Err(timing("nonmonotone camera timing"));
        }
    }
    Ok(())
}
pub fn validate_timing(s: &TimingSnapshot, now: i64, p: &PhasePolicy) -> Result<(), PhaseError> {
    p.validate()?;
    check_frames(s, now, p)
}
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Measurement {
    pub window_start_ns: i64,
    pub window_end_ns: i64,
    pub pair_count: usize,
    pub master_frames: usize,
    pub slave_frames: usize,
    pub unmatched_master: usize,
    pub unmatched_slave: usize,
    pub phase_ns: f64,
    pub absolute_p95_ns: f64,
    pub absolute_max_ns: f64,
    pub master_period_ns: f64,
    pub slave_period_ns: f64,
    pub drift_ns_per_second: f64,
    pub predicted_phase_ns: f64,
    pub correction_progress_ns: Option<f64>,
    pub ready_windows: i32,
    pub excursion: bool,
}
pub fn estimate_phase(
    master: &TimingSnapshot,
    slave: &TimingSnapshot,
    now: i64,
    p: &PhasePolicy,
    after: i64,
    previous: Option<f64>,
    nominal: i64,
) -> Result<Measurement, PhaseError> {
    p.validate()?;
    if nominal <= 0 || after < 0 || previous.is_some_and(|v| !v.is_finite()) {
        return Err(invalid("invalid phase estimation arguments"));
    }
    check_frames(master, now, p)?;
    check_frames(slave, now, p)?;
    let end = master
        .frames
        .last()
        .unwrap()
        .timestamp_ns
        .min(slave.frames.last().unwrap().timestamp_ns);
    let mut m = Measurement {
        window_end_ns: end,
        window_start_ns: end - ns(p.window_seconds),
        ..Default::default()
    };
    if m.window_start_ns < after
        || master.frames[0]
            .timestamp_ns
            .max(slave.frames[0].timestamp_ns)
            > m.window_start_ns
    {
        return Err(pending("insufficient settled overlapping coverage"));
    }
    let selected: [Vec<_>; 2] = [master, slave].map(|s| {
        s.frames
            .iter()
            .filter(|f| f.timestamp_ns >= m.window_start_ns && f.timestamp_ns <= end)
            .collect()
    });
    let mut periods = [0.0; 2];
    for (role, s) in [master, slave].iter().enumerate() {
        let frames = &selected[role];
        if (frames.len() as f64)
            < 3.0f64.max((p.window_seconds * 1e9 / nominal as f64 * 0.9).floor())
        {
            return Err(timing("insufficient frame coverage"));
        }
        let intervals: Vec<_> = frames
            .windows(2)
            .map(|w| (w[1].sensor_timestamp_ns - w[0].sensor_timestamp_ns) as f64)
            .collect();
        periods[role] = median(intervals.clone());
        if periods[role] < 0.9 * nominal as f64
            || periods[role] > 1.1 * nominal as f64
            || intervals
                .iter()
                .any(|&v| v < 0.8 * periods[role] || v > 1.2 * periods[role])
        {
            return Err(timing(&format!("camera cadence anomaly in {}", s.unit_id)));
        }
        if (frames[0].timestamp_ns - m.window_start_ns) as f64 > 1.5 * periods[role]
            || (end - frames.last().unwrap().timestamp_ns) as f64 > 1.5 * periods[role]
        {
            return Err(timing("inadequate overlapping boundary coverage"));
        }
    }
    let left = &selected[0];
    let right = &selected[1];
    let nearest = |timestamp: i64, offset: f64| {
        let pos = right.partition_point(|f| ((f.timestamp_ns - timestamp) as f64) < offset);
        let mut best = if pos == right.len() {
            right.len() - 1
        } else {
            pos
        };
        if best > 0
            && (((right[best - 1].timestamp_ns - timestamp) as f64) - offset).abs()
                <= (((right[best].timestamp_ns - timestamp) as f64) - offset).abs()
        {
            best -= 1;
        }
        best
    };
    let anchor = previous.unwrap_or_else(|| {
        let timestamp = left[left.len() / 2].timestamp_ns;
        (right[nearest(timestamp, 0.0)].timestamp_ns - timestamp) as f64
    });
    let mut used = vec![false; right.len()];
    let mut deltas = Vec::new();
    let mut interior = 0;
    for (i, f) in left.iter().enumerate() {
        let j = nearest(f.timestamp_ns, anchor.round_ties_even());
        let delta = (right[j].timestamp_ns - f.timestamp_ns) as f64;
        if used[j] || (delta - anchor.round_ties_even()).abs() > periods[0].max(periods[1]) / 2.0 {
            m.unmatched_master += 1;
            if i > 0 && i + 1 < left.len() {
                interior += 1;
            }
        } else {
            used[j] = true;
            deltas.push(delta);
        }
    }
    for (i, &u) in used.iter().enumerate() {
        if !u {
            m.unmatched_slave += 1;
            if i > 0 && i + 1 < used.len() {
                interior += 1;
            }
        }
    }
    if interior > 0 || deltas.is_empty() {
        return Err(timing("unmatched interior frames"));
    }
    let mut phase = median(deltas.clone());
    let period = (periods[0] + periods[1]) / 2.0;
    if let Some(prev) = previous {
        phase += ((prev - phase) / period).round_ties_even() * period;
    }
    let mut absolute = Vec::with_capacity(deltas.len());
    for d in &mut deltas {
        *d += ((phase - *d) / period).round_ties_even() * period;
        absolute.push(d.abs());
    }
    absolute.sort_by(f64::total_cmp);
    m.phase_ns = median(deltas.clone());
    m.absolute_p95_ns = absolute[(0.95 * absolute.len() as f64).ceil() as usize - 1];
    m.absolute_max_ns = *absolute.last().unwrap();
    m.master_period_ns = periods[0];
    m.slave_period_ns = periods[1];
    m.pair_count = deltas.len();
    m.master_frames = left.len();
    m.slave_frames = right.len();
    Ok(m)
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct PhaseDecision {
    pub state: String,
    pub ready: bool,
    pub action: Option<Pulse>,
    pub measurement: Option<Measurement>,
    pub fault: String,
}
#[path = "phase_control.rs"]
mod control;
pub use control::PhaseController;

#[cfg(test)]
mod tests {
    use super::*;
    use crate::timing::{CorrectionStatus, FrameTiming};
    const EPOCH: i64 = 1780000000000000000;
    fn snapshot(end: i64, phase: i64, last: u64) -> TimingSnapshot {
        TimingSnapshot {
            unit_id: "camera".into(),
            snapshot_timestamp_ns: end + phase + 1000000,
            frames: (0..121)
                .map(|i| FrameTiming {
                    camera_sequence: last - 120 + i,
                    sensor_timestamp_ns: 100000000000 + (last - 120 + i) as i64 * 25000000 + phase,
                    requested_duration_us: 25000,
                    reported_duration_us: 25000,
                    timestamp_ns: end - (120 - i) as i64 * 25000000 + phase,
                    ..Default::default()
                })
                .collect(),
            correction: CorrectionStatus::default(),
            minimum_duration_us: 20000,
            maximum_duration_us: 30000,
        }
    }
    #[test]
    fn epoch_precision_and_phase_unwrap() {
        let p = PhasePolicy::default();
        let m = estimate_phase(
            &snapshot(EPOCH, 0, 200),
            &snapshot(EPOCH, 123457, 200),
            EPOCH + 1000000,
            &p,
            0,
            None,
            25000000,
        )
        .unwrap();
        assert_eq!(m.phase_ns, 123457.0);
        assert_eq!(m.absolute_p95_ns, 123457.0);
        let m = estimate_phase(
            &snapshot(EPOCH, 0, 200),
            &snapshot(EPOCH, -12400000, 200),
            EPOCH + 1000000,
            &p,
            0,
            Some(12600000.0),
            25000000,
        )
        .unwrap();
        assert_eq!(m.phase_ns, 12600000.0);
    }
    #[test]
    fn cold_start_requires_observations_and_preserves_ids_on_reset() {
        let mut controller = PhaseController::new(PhasePolicy::default(), 25_000_000).unwrap();
        let master = snapshot(EPOCH, 0, 200);
        let follower = snapshot(EPOCH, 100_000, 200);
        let first = controller.tick(&master, &follower, EPOCH + 1_000_000, 0, true);
        assert_eq!(first.state, "identifying_negative");
        assert!(!first.ready);
        let pulse = first.action.unwrap();
        let d = controller.tick(&master, &follower, EPOCH + 1_000_000, 200_000_000, true);
        assert!(d.action.is_none() && !d.ready && d.fault.is_empty());
        let d = controller.tick(&master, &follower, EPOCH + 1_000_000, 1_200_000_000, true);
        assert_eq!(d.fault, "timing evidence stalled on monotonic deadline");
        controller.reset(None, 0).unwrap();
        let again = controller
            .tick(&master, &follower, EPOCH + 1_000_000, 0, true)
            .action
            .unwrap();
        assert_eq!(again.correction_id, pulse.correction_id + 1);
        let mut follower = snapshot(EPOCH + 1_000_000_000, 0, 240);
        follower.correction = CorrectionStatus {
            state: "restored".into(),
            correction_id: again.correction_id,
            restored_timestamp_ns: EPOCH + 900_000_000,
        };
        let d = controller.tick(
            &snapshot(EPOCH + 1_000_000_000, 0, 240),
            &follower,
            EPOCH + 1_001_000_000,
            1_000_000_000,
            true,
        );
        assert!(d.fault.contains("no tagged camera request"));
    }

    #[test]
    fn restart_uses_the_current_epoch_even_if_history_contains_the_same_pulse_id() {
        let mut controller = PhaseController::new(PhasePolicy::default(), 25_000_000).unwrap();
        let mut first = snapshot(EPOCH, 0, 200);
        first.minimum_duration_us = 24998;
        let pulse = controller
            .tick(&snapshot(EPOCH, 0, 200), &first, EPOCH + 1_000_000, 0, true)
            .action
            .unwrap();
        assert_eq!(
            pulse.delta_us, -2,
            "use the available tested camera headroom"
        );
        let end = EPOCH + 800_000_000;
        let mut observed = snapshot(end, 0, 232);
        observed.minimum_duration_us = first.minimum_duration_us;
        for f in &mut observed.frames {
            let seq = f.camera_sequence;
            if (185..191).contains(&seq) || (205..211).contains(&seq) {
                f.correction_id = pulse.correction_id;
                f.requested_duration_us += i64::from(pulse.delta_us);
            }
            if (208..214).contains(&seq) {
                f.reported_duration_us += i64::from(pulse.delta_us);
            }
            let shift = (seq.saturating_sub(207).min(6) as i64) * 2000;
            f.timestamp_ns -= shift;
            f.sensor_timestamp_ns -= shift;
        }
        observed.correction = CorrectionStatus {
            state: "restored".into(),
            correction_id: pulse.correction_id,
            restored_timestamp_ns: end,
        };
        let next = controller.tick(
            &snapshot(end, 0, 232),
            &observed,
            end + 1_000_000,
            800_000_000,
            true,
        );
        assert!(next.fault.is_empty(), "{}", next.fault);
        assert!(next.action.unwrap().delta_us > 0);
    }

    #[test]
    fn malformed_timestamps_and_policy_fail_without_overflow() {
        let p = PhasePolicy::default();
        let s = snapshot(EPOCH, 0, 200);
        assert!(validate_timing(&s, i64::MIN, &p).is_err());
        let mut bad = s.clone();
        bad.frames.last_mut().unwrap().timestamp_ns = i64::MAX;
        bad.snapshot_timestamp_ns = i64::MAX;
        assert!(validate_timing(&bad, 1, &p).is_err());
        bad = s.clone();
        bad.frames[30].camera_sequence += 1;
        assert!(validate_timing(&bad, EPOCH + 1000000, &p).is_err());
        let mut p = p;
        p.startup_frames = 17;
        assert!(p.validate().is_err());
    }
}
