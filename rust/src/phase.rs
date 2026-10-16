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
    pub deadband_ms: f64,
    pub ready_p95_ms: f64,
    pub ready_max_ms: f64,
    pub ready_windows: i32,
    pub startup_delta_us: i32,
    pub startup_frames: i32,
    pub tracking_delta_us: i32,
    pub tracking_frames: i32,
    pub correction_interval_seconds: f64,
    pub settling_seconds: f64,
    pub application_delay_seconds: f64,
    pub startup_max_corrections: i32,
    pub excursion_ms: f64,
    pub excursion_timeout_seconds: f64,
    pub no_progress_corrections: i32,
    pub progress_ms: f64,
    pub gain: f64,
    pub max_mapping_span_ns: i64,
    pub max_samples: usize,
}
impl Default for PhasePolicy {
    fn default() -> Self {
        Self {
            window_seconds: 2.0,
            evaluation_seconds: 1.0,
            freshness_seconds: 1.0,
            deadband_ms: 0.25,
            ready_p95_ms: 0.5,
            ready_max_ms: 1.0,
            ready_windows: 2,
            startup_delta_us: 1000,
            startup_frames: 16,
            tracking_delta_us: 200,
            tracking_frames: 8,
            correction_interval_seconds: 2.0,
            settling_seconds: 0.25,
            application_delay_seconds: 0.25,
            startup_max_corrections: 5,
            excursion_ms: 2.0,
            excursion_timeout_seconds: 10.0,
            no_progress_corrections: 3,
            progress_ms: 0.05,
            gain: 0.8,
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
            p.deadband_ms,
            p.ready_p95_ms,
            p.ready_max_ms,
            p.correction_interval_seconds,
            p.settling_seconds,
            p.application_delay_seconds,
            p.excursion_ms,
            p.excursion_timeout_seconds,
            p.progress_ms,
            p.gain,
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
            p.tracking_frames,
            p.startup_max_corrections,
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
            || p.gain > 1.0
            || p.window_seconds > 6.0
        {
            return Err(invalid("phase policy exceeds controller bounds"));
        }
        if p.startup_delta_us > 1000
            || p.startup_frames > 16
            || p.tracking_delta_us > 200
            || p.tracking_frames > 8
        {
            return Err(invalid("phase pulse exceeds hard bounds"));
        }
        if !(p.deadband_ms <= p.ready_p95_ms
            && p.ready_p95_ms <= p.ready_max_ms
            && p.ready_max_ms < p.excursion_ms)
        {
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
#[derive(Clone)]
struct Latest {
    sequence: u64,
    sensor_timestamp: i64,
    received: i64,
}
#[derive(Clone)]
pub struct PhaseController {
    policy: PhasePolicy,
    nominal: i64,
    next_id: u64,
    ready: bool,
    state: String,
    fault: String,
    last_tick: Option<i64>,
    last_eval: Option<i64>,
    last_start: Option<i64>,
    excursion_start: Option<i64>,
    after: i64,
    good_end: i64,
    pending: Option<Pulse>,
    previous: Option<Measurement>,
    before_correction: Option<f64>,
    good: i32,
    attempts: i32,
    no_progress: i32,
    latest: [Option<Latest>; 2],
}
impl PhaseController {
    pub fn new(policy: PhasePolicy, nominal: i64) -> Result<Self, PhaseError> {
        policy.validate()?;
        if nominal <= 0 {
            return Err(invalid("nominal frame period must be positive"));
        }
        Ok(Self {
            policy,
            nominal,
            next_id: 1,
            ready: false,
            state: "warming".into(),
            fault: String::new(),
            last_tick: None,
            last_eval: None,
            last_start: None,
            excursion_start: None,
            after: 0,
            good_end: 0,
            pending: None,
            previous: None,
            before_correction: None,
            good: 0,
            attempts: 0,
            no_progress: 0,
            latest: [None, None],
        })
    }
    pub fn ready(&self) -> bool {
        self.ready
    }
    pub fn reset(&mut self, mono: Option<i64>, after: i64) -> Result<(), PhaseError> {
        if mono.is_some_and(|v| v < 0) || after < 0 {
            return Err(invalid("negative reset boundary"));
        }
        let next = self.next_id;
        let mut value = Self::new(self.policy.clone(), self.nominal)?;
        value.last_tick = mono;
        value.after = after;
        value.next_id = next;
        *self = value;
        Ok(())
    }
    fn result(&self, m: Option<Measurement>, action: Option<Pulse>) -> PhaseDecision {
        PhaseDecision {
            state: self.state.clone(),
            ready: self.ready,
            action,
            measurement: m,
            fault: self.fault.clone(),
        }
    }
    fn fail(&mut self, reason: &str, m: Option<Measurement>) -> PhaseDecision {
        self.state = "fault".into();
        self.ready = false;
        self.fault = reason.into();
        self.result(m, None)
    }
    pub fn tick(
        &mut self,
        master: &TimingSnapshot,
        slave: &TimingSnapshot,
        now: i64,
        mono: i64,
        clock_valid: bool,
    ) -> PhaseDecision {
        if !self.fault.is_empty() {
            return self.result(None, None);
        }
        if mono < 0 || self.last_tick.is_some_and(|v| mono < v) {
            return self.fail("monotonic clock moved backwards", None);
        }
        self.last_tick = Some(mono);
        if !clock_valid {
            return self.fail("clock health lost", None);
        }
        for s in [master, slave] {
            if let Err(e) = check_frames(s, now, &self.policy) {
                return if e.kind == 3 {
                    if self.ready {
                        self.fail("missing timing evidence", None)
                    } else {
                        self.result(None, None)
                    }
                } else {
                    self.fail(&e.message, None)
                };
            }
        }
        for (i, s) in [master, slave].iter().enumerate() {
            let frame = s.frames.last().unwrap();
            if let Some(prev) = &self.latest[i] {
                if frame.camera_sequence < prev.sequence
                    || frame.sensor_timestamp_ns < prev.sensor_timestamp
                    || (frame.camera_sequence == prev.sequence
                        && frame.sensor_timestamp_ns != prev.sensor_timestamp)
                {
                    return self.fail("camera timing restarted or changed identity", None);
                }
                if frame.camera_sequence == prev.sequence {
                    if mono - prev.received > ns(self.policy.freshness_seconds) {
                        return self.fail("timing evidence stalled on monotonic deadline", None);
                    }
                    continue;
                }
            }
            self.latest[i] = Some(Latest {
                sequence: frame.camera_sequence,
                sensor_timestamp: frame.sensor_timestamp_ns,
                received: mono,
            });
        }
        if let Some(pending) = self.pending {
            let correction = &slave.correction;
            if correction.correction_id != pending.correction_id {
                return if mono - self.last_start.unwrap() > 5000000000 {
                    self.fail("correction acknowledgement timeout", None)
                } else {
                    self.result(None, None)
                };
            }
            if correction.state != "restored" && correction.state != "cancelled" {
                return if mono - self.last_start.unwrap() > 5000000000 {
                    self.fail("correction restoration timeout", None)
                } else {
                    self.result(None, None)
                };
            }
            if correction.restored_timestamp_ns <= 0
                || correction.restored_timestamp_ns > now
                || correction.restored_timestamp_ns > i64::MAX - ns(self.policy.settling_seconds)
            {
                return self.fail("invalid restoration boundary", None);
            }
            self.after = correction.restored_timestamp_ns + ns(self.policy.settling_seconds);
            self.pending = None;
            self.state = "settling".into();
        }
        if self
            .last_eval
            .is_some_and(|v| mono - v < ns(self.policy.evaluation_seconds))
        {
            return self.result(None, None);
        }
        let mut m = match estimate_phase(
            master,
            slave,
            now,
            &self.policy,
            self.after,
            self.previous.as_ref().map(|v| v.phase_ns),
            self.nominal,
        ) {
            Ok(v) => v,
            Err(e) => {
                return if e.kind == 3 {
                    self.result(None, None)
                } else {
                    self.fail(&e.message, None)
                }
            }
        };
        if self
            .previous
            .as_ref()
            .is_some_and(|v| m.window_end_ns <= v.window_end_ns)
        {
            return self.result(None, None);
        }
        self.last_eval = Some(mono);
        let p = &self.policy;
        let drift = match &self.previous {
            Some(prev) if self.before_correction.is_none() => {
                (m.phase_ns - prev.phase_ns) / (m.window_end_ns - prev.window_end_ns) as f64
            }
            _ => 0.0,
        };
        m.drift_ns_per_second = drift * 1e9;
        m.predicted_phase_ns =
            m.phase_ns + drift * (p.window_seconds / 2.0 + p.application_delay_seconds) * 1e9;
        if let Some(before) = self.before_correction.take() {
            let progress = before - m.phase_ns.abs();
            m.correction_progress_ns = Some(progress);
            self.no_progress =
                if m.phase_ns.abs() > p.excursion_ms * 1e6 && progress < p.progress_ms * 1e6 {
                    self.no_progress + 1
                } else {
                    0
                };
        }
        self.previous = Some(m.clone());
        m.excursion = m.phase_ns.abs() > p.excursion_ms * 1e6;
        if m.excursion {
            let start = *self.excursion_start.get_or_insert(mono);
            if mono - start >= ns(p.excursion_timeout_seconds)
                || self.no_progress >= p.no_progress_corrections
            {
                return self.fail("persistent phase nonconvergence", Some(m));
            }
        } else {
            self.excursion_start = None;
        }
        let passes =
            m.absolute_p95_ns <= p.ready_p95_ms * 1e6 && m.absolute_max_ns <= p.ready_max_ms * 1e6;
        if !self.ready {
            if !passes {
                self.good = 0;
            } else if m.window_start_ns >= self.good_end {
                self.good += 1;
                self.good_end = m.window_end_ns;
                if self.good >= p.ready_windows {
                    self.ready = true;
                }
            }
        }
        self.state = if self.ready {
            "tracking"
        } else {
            "calibrating"
        }
        .into();
        m.ready_windows = self.good;
        if m.phase_ns.abs().max(m.predicted_phase_ns.abs()) <= p.deadband_ms * 1e6 {
            return self.result(Some(m), None);
        }
        if self
            .last_start
            .is_some_and(|v| mono - v < ns(p.correction_interval_seconds))
        {
            return self.result(Some(m), None);
        }
        if !self.ready && self.attempts >= p.startup_max_corrections {
            return self.fail("startup correction budget exhausted", Some(m));
        }
        let bound = if self.ready {
            p.tracking_delta_us
        } else {
            p.startup_delta_us
        };
        let count = if self.ready {
            p.tracking_frames
        } else {
            p.startup_frames
        };
        let delta = (-p.gain * m.predicted_phase_ns / (count as f64 * 1000.0))
            .round_ties_even()
            .clamp(-(bound as f64), bound as f64) as i32;
        if delta == 0 {
            return self.result(Some(m), None);
        }
        if now > i64::MAX - 5000000000 {
            return self.fail("correction timestamp overflow", Some(m));
        }
        let action = Pulse {
            correction_id: self.next_id,
            delta_us: delta,
            count,
            expires_ns: now + 5000000000,
        };
        self.next_id = self.next_id.wrapping_add(1);
        self.pending = Some(action);
        self.last_start = Some(mono);
        self.before_correction = Some(m.phase_ns.abs());
        self.attempts += 1;
        self.good = 0;
        self.good_end = 0;
        self.state = "correcting".into();
        self.result(Some(m), Some(action))
    }
}

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
                    timestamp_ns: end - (120 - i) as i64 * 25000000 + phase,
                    ..Default::default()
                })
                .collect(),
            correction: CorrectionStatus::default(),
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
    fn readiness_needs_disjoint_windows_and_stalled_input_faults() {
        let mut c = PhaseController::new(PhasePolicy::default(), 25000000).unwrap();
        assert!(
            !c.tick(
                &snapshot(EPOCH, 0, 200),
                &snapshot(EPOCH, 100000, 200),
                EPOCH + 1000000,
                1000000000,
                true
            )
            .ready
        );
        assert!(
            !c.tick(
                &snapshot(EPOCH + 1000000000, 0, 240),
                &snapshot(EPOCH + 1000000000, 100000, 240),
                EPOCH + 1001000000,
                2000000000,
                true
            )
            .ready
        );
        assert!(
            c.tick(
                &snapshot(EPOCH + 2025000000, 0, 281),
                &snapshot(EPOCH + 2025000000, 100000, 281),
                EPOCH + 2026000000,
                3025000000,
                true
            )
            .ready
        );
        assert_eq!(
            c.tick(
                &snapshot(EPOCH + 2025000000, 0, 281),
                &snapshot(EPOCH + 2025000000, 100000, 281),
                EPOCH + 2026000000,
                4126000000,
                true
            )
            .fault,
            "timing evidence stalled on monotonic deadline"
        );
    }
    #[test]
    fn correction_waits_for_observed_restoration_and_preserves_ids_on_reset() {
        let mut c = PhaseController::new(PhasePolicy::default(), 25000000).unwrap();
        let d = c.tick(
            &snapshot(EPOCH, 0, 200),
            &snapshot(EPOCH, 4000000, 200),
            EPOCH + 5000000,
            0,
            true,
        );
        let first = d.action.unwrap();
        assert_eq!(first.delta_us, -200);
        assert_eq!(first.count, 16);
        let mut slave = snapshot(EPOCH + 1000000000, 0, 240);
        slave.correction = CorrectionStatus {
            state: "restored".into(),
            correction_id: first.correction_id,
            restored_timestamp_ns: EPOCH + 500000000,
        };
        assert_eq!(
            c.tick(
                &snapshot(EPOCH + 1000000000, 0, 240),
                &slave,
                EPOCH + 1001000000,
                1000000000,
                true
            )
            .state,
            "settling"
        );
        c.reset(None, 0).unwrap();
        let second = c
            .tick(
                &snapshot(EPOCH, 0, 200),
                &snapshot(EPOCH, 4000000, 200),
                EPOCH + 5000000,
                0,
                true,
            )
            .action
            .unwrap();
        assert_eq!(second.correction_id, first.correction_id + 1);
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
        p.gain = 2.0;
        assert!(p.validate().is_err());
    }
}
