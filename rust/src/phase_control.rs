//! Camera-response identification, coarse phase acquisition and queue-aware tracking.
use super::{
    check_frames, estimate_phase, invalid, median, ns, Measurement, PhaseDecision, PhaseError,
    PhasePolicy,
};
use crate::actuator::INITIAL_RESPONSE_SPAN;
use crate::timing::{FrameTiming, Pulse, TimingSnapshot};

const PROBE_FRAMES: i32 = 6;
const GUARD_FRAMES: u64 = 3;

#[derive(Clone, Copy, Debug)]
struct Step {
    command_us: i32,
    shift_us: f64,
    lag: u64,
}

#[derive(Clone, Copy)]
struct Pending {
    pulse: Pulse,
    issued_seq: u64,
    started: i64,
    purpose: Purpose,
}

#[derive(Clone, Copy, PartialEq)]
enum Purpose {
    Identify,
    Acquire,
    Track,
}

#[derive(Clone)]
struct Model {
    reference_us: f64,
    follower_us: f64,
    baseline_request: i64,
    baseline_report: i64,
    noise_us: f64,
    drift_us: f64,
    steps: [Option<Step>; 2],
    probe_us: i32,
}

/// Phase state is relative to a real reference frame in the common clock domain.
#[derive(Clone)]
struct Observer {
    last_seq: u64,
    phase_us: f64,
    rate_us: f64,
    alpha: f64,
    crossing_pulse: bool,
}

impl Observer {
    fn update(&mut self, seq: u64, measured: f64, period: f64) {
        let frames = seq - self.last_seq;
        self.phase_us += frames as f64 * self.rate_us;
        let error = wrap(measured - self.phase_us, period);
        self.phase_us += self.alpha * error;
        self.rate_us += self.alpha.powi(2) * 0.25 * error;
        self.last_seq = seq;
    }
}

fn wrap(x: f64, period: f64) -> f64 {
    x - (x / period).round_ties_even() * period
}

fn relative_phase(frame: &FrameTiming, reference: &TimingSnapshot, period_us: f64) -> f64 {
    let right = reference
        .frames
        .partition_point(|r| r.timestamp_ns < frame.timestamp_ns);
    let near = reference.frames[right.saturating_sub(1)..(right + 1).min(reference.frames.len())]
        .iter()
        .min_by_key(|r| r.timestamp_ns.abs_diff(frame.timestamp_ns))
        .unwrap();
    wrap(
        (frame.timestamp_ns - near.timestamp_ns) as f64 / 1000.0,
        period_us,
    )
}

fn interval_us(a: &FrameTiming, b: &FrameTiming) -> f64 {
    (b.sensor_timestamp_ns - a.sensor_timestamp_ns) as f64 / 1000.0
}

/// Estimate camera response only from uninterrupted sensor intervals. Sequence
/// numbers alone cannot identify simultaneous cadence stalls on both ports.
fn cadence_gap(frames: &[FrameTiming], period_us: f64, pulse_us: f64) -> bool {
    frames.windows(2).any(|pair| {
        (interval_us(&pair[0], &pair[1]) - period_us).abs() > 0.2 * period_us + pulse_us.abs()
    })
}

impl Model {
    fn baseline(
        master: &TimingSnapshot,
        slave: &TimingSnapshot,
        after: i64,
        nominal: i64,
    ) -> Option<Self> {
        // Average unperturbed intervals over the same elapsed time on both cameras.
        let length = (500_000_000 / nominal).clamp(8, 64) as usize;
        let recent = |s: &TimingSnapshot| -> Option<(f64, f64)> {
            if s.frames.len() <= length {
                return None;
            }
            let f = &s.frames[s.frames.len() - length - 1..];
            if f[0].timestamp_ns < after || cadence_gap(f, nominal as f64 / 1000., 0.) {
                return None;
            }
            let period = interval_us(&f[0], f.last()?) / length as f64;
            let noise = median(
                f.windows(2)
                    .map(|p| (interval_us(&p[0], &p[1]) - period).abs())
                    .collect(),
            );
            Some((period, noise))
        };
        let (reference_us, _) = recent(master)?;
        let (follower_us, noise_us) = recent(slave)?;
        let frames = &slave.frames[slave.frames.len() - length..];
        Some(Self {
            reference_us,
            follower_us,
            noise_us,
            drift_us: follower_us - reference_us,
            baseline_request: frames.last()?.requested_duration_us,
            baseline_report: median(
                frames
                    .iter()
                    .map(|f| f.reported_duration_us as f64)
                    .collect(),
            ) as i64,
            steps: [None, None],
            probe_us: 4,
        })
    }
    fn fine(&self, phase: f64) -> Step {
        self.steps[usize::from(phase <= 0.)].unwrap()
    }
    fn lag(&self) -> u64 {
        self.steps
            .iter()
            .flatten()
            .map(|s| s.lag)
            .max()
            .unwrap_or(0)
    }

    /// Locate the contiguous burst in sensor intervals. Reported duration corroborates
    /// the response; a request acknowledgement alone is never an actuator measurement.
    fn response(
        &self,
        frames: &[FrameTiming],
        tag: usize,
        pulse: Pulse,
        search: usize,
    ) -> Option<Step> {
        if tag == 0 {
            return None;
        }
        let n = pulse.count as usize;
        let sign = f64::from(pulse.delta_us.signum());
        let mut best = None;
        for offset in 0..=search {
            let first = tag + offset;
            let last = first + n - 1;
            if last >= frames.len() {
                break;
            }
            let reported = frames[first..=last]
                .iter()
                .filter(|f| {
                    (pulse.delta_us > 0 && f.reported_duration_us > self.baseline_report)
                        || (pulse.delta_us < 0 && f.reported_duration_us < self.baseline_report)
                })
                .count();
            if self.baseline_report > 0 && reported * 2 < n {
                continue;
            }
            let shift =
                interval_us(&frames[first - 1], &frames[last]) / n as f64 - self.follower_us;
            if shift * sign < (self.noise_us * 2. / n as f64).max(1.0) {
                continue;
            }
            // The full burst averages the timestamp quantization of its two endpoints.
            if best.is_none_or(|(_, value): (usize, f64)| shift * sign > value * sign) {
                best = Some((first, shift));
            }
        }
        best.map(|(i, shift_us)| Step {
            command_us: pulse.delta_us,
            shift_us,
            lag: frames[i].camera_sequence,
        })
    }
}

#[derive(Clone)]
pub struct PhaseController {
    policy: PhasePolicy,
    nominal: i64,
    after: i64,
    next_id: u64,
    ready: bool,
    state: String,
    fault: String,
    latest: [Option<(u64, i64, i64)>; 2],
    last_tick: Option<i64>,
    model: Option<Model>,
    pending: Option<Pending>,
    observer: Option<Observer>,
    health_after: i64,
    last_health: i64,
    good_end: i64,
    good: i32,
    excursion_since: Option<i64>,
    last_progress: Option<(f64, i32)>,
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
            after: 0,
            next_id: 1,
            ready: false,
            state: "warming".into(),
            fault: String::new(),
            latest: [None, None],
            last_tick: None,
            model: None,
            pending: None,
            observer: None,
            health_after: 0,
            last_health: 0,
            good_end: 0,
            good: 0,
            excursion_since: None,
            last_progress: None,
        })
    }

    pub fn ready(&self) -> bool {
        self.ready
    }

    pub fn reset(&mut self, mono: Option<i64>, after: i64) -> Result<(), PhaseError> {
        if mono.is_some_and(|m| m < 0) || after < 0 {
            return Err(invalid("negative reset boundary"));
        }
        let seq = self.next_id;
        *self = Self::new(self.policy.clone(), self.nominal)?;
        self.last_tick = mono;
        self.next_id = seq;
        self.after = after;
        Ok(())
    }
    fn result(&self, measurement: Option<Measurement>, action: Option<Pulse>) -> PhaseDecision {
        PhaseDecision {
            state: self.state.clone(),
            ready: self.ready,
            action,
            measurement,
            fault: self.fault.clone(),
        }
    }
    fn fail(&mut self, reason: &str) -> PhaseDecision {
        self.ready = false;
        self.state = "fault".into();
        self.fault = reason.to_owned();
        self.result(None, None)
    }
    fn bound(&self, slave: &TimingSnapshot, sign: i32, limit: i32) -> i32 {
        let baseline = self.model.as_ref().unwrap().baseline_request;
        let headroom = if sign < 0 {
            baseline - slave.minimum_duration_us
        } else {
            slave.maximum_duration_us - baseline
        };
        headroom.clamp(0, i64::from(limit)) as i32
    }
    fn burst_limit(&self) -> i32 {
        // The actuator accepts commands for at most five seconds. Reserve a second
        // for dispatch and request scheduling even on slow acquisition modes.
        (4_000_000_000 / self.nominal).clamp(1, i64::from(self.policy.startup_frames)) as i32
    }

    fn issue(
        &mut self,
        purpose: Purpose,
        slave: &TimingSnapshot,
        now: i64,
        mono: i64,
        cmd: i32,
        count: i32,
    ) -> Result<Pulse, String> {
        let ttl = ((count as i128 + 4) * self.nominal as i128 + 500_000_000)
            .clamp(1_000_000_000, 5_000_000_000) as i64;
        let pulse = Pulse {
            correction_id: self.next_id,
            delta_us: cmd,
            count,
            expires_ns: now
                .checked_add(ttl)
                .ok_or("correction timestamp overflow")?,
        };
        self.next_id = self
            .next_id
            .checked_add(1)
            .ok_or("correction ID overflow")?;
        self.pending = Some(Pending {
            pulse,
            issued_seq: slave.frames.last().unwrap().camera_sequence,
            started: mono,
            purpose,
        });
        Ok(pulse)
    }

    fn complete(&mut self, slave: &TimingSnapshot, now: i64, mono: i64) -> Result<bool, String> {
        let Some(p) = self.pending else {
            return Ok(true);
        };
        let c = &slave.correction;
        if c.correction_id != p.pulse.correction_id {
            if mono - p.started > 5_000_000_000 {
                return Err("correction acknowledgement timeout".into());
            }
            return Ok(false);
        }
        if c.state == "cancelled" {
            return Err("phase correction cancelled during acquisition".into());
        }
        if c.state != "restored" {
            if !matches!(c.state.as_str(), "submitting" | "settling") {
                return Err(format!(
                    "invalid correction acknowledgement state: {}",
                    c.state
                ));
            }
            // The submission lifetime does not bound when those exposures can be
            // observed through a device queue, particularly at low frame rates.
            let lag = self
                .model
                .as_ref()
                .unwrap()
                .lag()
                .max(INITIAL_RESPONSE_SPAN as u64);
            let frames = i128::from(lag) + i128::from(p.pulse.count) + i128::from(GUARD_FRAMES);
            let limit = (frames * i128::from(self.nominal)).max(5_000_000_000);
            if i128::from(mono - p.started) > limit {
                return Err("correction restoration timeout".into());
            }
            return Ok(false);
        }
        if c.restored_timestamp_ns <= 0 || c.restored_timestamp_ns > now {
            return Err("invalid restoration boundary".into());
        }
        let search_limit = self.bound(
            slave,
            p.pulse.delta_us.signum(),
            self.policy.tracking_delta_us,
        );
        let model = self.model.as_mut().unwrap();
        let tag = slave.frames.iter().position(|f| {
            f.camera_sequence > p.issued_seq
                && f.correction_id == p.pulse.correction_id
                && f.requested_duration_us != model.baseline_request
        });
        let Some(tag) = tag else {
            return Err("restored correction has no tagged camera request".into());
        };
        let tagged = slave.frames[tag].camera_sequence;
        if tag == 0 {
            return Err("invalid tagged correction sequence".into());
        }
        let n = p.pulse.count as usize;
        if slave.frames.len() < tag + n
            || slave.frames[tag..tag + n].iter().any(|f| {
                f.correction_id != p.pulse.correction_id
                    || f.requested_duration_us != model.baseline_request + p.pulse.delta_us as i64
            })
        {
            return Err("incomplete camera pulse submission".into());
        }
        let seq = slave.frames.last().unwrap().camera_sequence;
        if p.purpose == Purpose::Identify {
            let lead = tagged - p.issued_seq;
            let known = model.lag().saturating_sub(lead);
            let search = if model.lag() > 0 {
                known as usize + 3
            } else {
                INITIAL_RESPONSE_SPAN
            };
            let available = slave.frames.len() - tag - n;
            let end = (tag + n + search).min(slave.frames.len());
            if cadence_gap(
                &slave.frames[tag - 1..end],
                model.follower_us,
                f64::from(p.pulse.delta_us),
            ) {
                if available < search {
                    return Ok(false);
                }
                // This trial cannot distinguish a missed exposure from commanded
                // movement. Repeat this exact signed probe on a fresh sequence.
                model.probe_us = p.pulse.delta_us.abs();
                self.pending = None;
                return Ok(true);
            }
            let response = model.response(&slave.frames, tag, p.pulse, available.min(search));
            // An effective burst can finish early once its trailing edge is observed.
            if available < search
                && response.is_none_or(|s| seq < s.lag.saturating_add(n as u64 + GUARD_FRAMES))
            {
                return Ok(false);
            }
            if let Some(mut step) = response {
                step.lag -= p.issued_seq;
                model.steps[usize::from(p.pulse.delta_us > 0)] = Some(step);
                model.probe_us = 4;
            } else {
                if p.pulse.delta_us.abs() >= search_limit {
                    return Err(format!(
                        "no measurable {} frame-period response within {search_limit} us",
                        if p.pulse.delta_us < 0 {
                            "negative"
                        } else {
                            "positive"
                        }
                    ));
                }
                model.probe_us = p.pulse.delta_us.abs() * 2;
            }
        } else {
            let due = p
                .issued_seq
                .saturating_add(model.fine(-(p.pulse.delta_us as f64)).lag)
                .saturating_add(n as u64 - 1 + GUARD_FRAMES);
            if seq < due {
                return Ok(false);
            }
            if p.purpose == Purpose::Track {
                // Update the actual decision-to-effect horizon from an isolated pulse
                // when both reported duration and the measured sensor interval agree.
                let direction = usize::from(p.pulse.delta_us > 0);
                let step = model.steps[direction].as_mut().unwrap();
                let mut observations = slave.frames[tag..]
                    .iter()
                    .take_while(|f| f.camera_sequence <= due)
                    .filter(|f| {
                        (p.pulse.delta_us > 0 && f.reported_duration_us > model.baseline_report)
                            || (p.pulse.delta_us < 0
                                && f.reported_duration_us < model.baseline_report)
                    });
                if let Some(effect) = observations.next() {
                    if observations.next().is_none() {
                        let i = tag + (effect.camera_sequence - tagged) as usize;
                        let shift = interval_us(&slave.frames[i - 1], effect) - model.follower_us;
                        if shift / step.shift_us > 0.5 && shift / step.shift_us < 1.8 {
                            step.lag = effect.camera_sequence - p.issued_seq;
                        }
                    }
                }
            }
        }
        self.pending = None;
        Ok(true)
    }

    fn control(
        &mut self,
        master: &TimingSnapshot,
        slave: &TimingSnapshot,
        now: i64,
        mono: i64,
    ) -> Result<Option<Pulse>, String> {
        if self.model.is_none() {
            self.model = Model::baseline(master, slave, self.after, self.nominal);
        }
        let Some(model) = self.model.as_ref() else {
            return Ok(None);
        };
        if slave.minimum_duration_us <= 0
            || model.baseline_request <= slave.minimum_duration_us
            || model.baseline_request >= slave.maximum_duration_us
            || model.baseline_request
                > i64::MAX
                    - i64::from(
                        self.policy
                            .startup_delta_us
                            .max(self.policy.tracking_delta_us),
                    )
        {
            return Err("camera timing needs actual bidirectional frame-duration headroom".into());
        }
        if (model.reference_us - self.nominal as f64 / 1000.).abs()
            > 0.1 * self.nominal as f64 / 1000.
            || (model.follower_us - self.nominal as f64 / 1000.).abs()
                > 0.1 * self.nominal as f64 / 1000.
        {
            return Err("measured camera period differs from nominal rate".into());
        }
        if !self.complete(slave, now, mono)? {
            return Ok(None);
        }
        let model = self.model.as_ref().unwrap();
        if let Some(index) = model.steps.iter().position(Option::is_none) {
            let sign = if index == 0 { -1 } else { 1 };
            let limit = self.bound(slave, sign, self.policy.tracking_delta_us);
            self.state = if sign < 0 {
                "identifying_negative"
            } else {
                "identifying_positive"
            }
            .into();
            let cmd = sign * model.probe_us.min(limit);
            let cnt = PROBE_FRAMES.min(self.burst_limit());
            return self
                .issue(Purpose::Identify, slave, now, mono, cmd, cnt)
                .map(Some);
        }
        let frame = slave.frames.last().unwrap();
        let seq = frame.camera_sequence;
        let period = model.reference_us;
        let phase = relative_phase(frame, master, period);
        if self.observer.is_none() {
            self.state = "acquiring".into();
            let step = model.fine(phase);
            if phase.abs() > 1.1 * step.shift_us.abs() {
                let target = phase.abs();
                let (previous, stalls) = self.last_progress.unwrap_or((f64::INFINITY, 0));
                let stalls = if target >= previous - step.shift_us.abs() * 0.25 {
                    stalls + 1
                } else {
                    0
                };
                if stalls >= self.policy.no_progress_corrections {
                    return Err("coarse phase acquisition made no progress".into());
                }
                self.last_progress = Some((target, stalls));
                let sign = step.command_us.signum();
                let limit = self.bound(slave, sign, self.policy.startup_delta_us);
                // Large changes use the requested period in physical microseconds.
                // Near lock, use the calibrated pulse count and measured physical step.
                let (cmd, count) = if target <= step.shift_us.abs() * 4. {
                    (
                        step.command_us,
                        (target / step.shift_us.abs())
                            .round()
                            .clamp(1., self.burst_limit() as f64) as i32,
                    )
                } else {
                    if limit == 0 {
                        return Err("no coarse adjustment headroom".into());
                    }
                    let count = (target / limit as f64)
                        .ceil()
                        .clamp(1., self.burst_limit() as f64)
                        as i32;
                    let amount = (target / count as f64).round().clamp(1., limit as f64) as i32;
                    (sign * amount, count)
                };
                return self
                    .issue(Purpose::Acquire, slave, now, mono, cmd, count)
                    .map(Some);
            }
            self.observer = Some(Observer {
                last_seq: seq,
                crossing_pulse: false,
                phase_us: phase,
                rate_us: model.drift_us,
                alpha: 1. - (-2. * period / 1e6).exp(),
            });
            self.health_after = frame.timestamp_ns;
            self.state = "qualifying".into();
            return Ok(None);
        }
        let obs = self.observer.as_mut().unwrap();
        if seq == obs.last_seq {
            return Ok(None);
        }
        // The estimated movement of a pending pulse has already been applied to
        // phase_us. Across that blind interval use a single measurement update.
        if obs.crossing_pulse {
            obs.update(seq, phase, period);
        } else {
            for f in &slave.frames[slave
                .frames
                .partition_point(|f| f.camera_sequence <= obs.last_seq)..]
            {
                obs.update(f.camera_sequence, relative_phase(f, master, period), period);
            }
        }
        obs.crossing_pulse = false;
        let predicted = obs.phase_us + model.lag() as f64 * obs.rate_us;
        let step = model.fine(predicted);
        let moving_away = obs.rate_us.abs() > 0.4 * period / 1e6 && obs.rate_us * predicted > 0.;
        let trigger = if moving_away { 0.5 } else { 0.9 };
        if predicted.abs() >= trigger * step.shift_us.abs() {
            obs.phase_us += step.shift_us;
            obs.crossing_pulse = true;
            return self
                .issue(Purpose::Track, slave, now, mono, step.command_us, 1)
                .map(Some);
        }
        Ok(None)
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
            return self.fail("monotonic clock moved backwards");
        }
        self.last_tick = Some(mono);
        if !clock_valid {
            return self.fail("clock health lost");
        }
        for (i, s) in [master, slave].iter().enumerate() {
            if let Err(e) = check_frames(s, now, &self.policy) {
                if e.kind == 3 && !self.ready {
                    return self.result(None, None);
                }
                return self.fail(&e.message);
            }
            let f = s.frames.last().unwrap();
            if let Some((seq, sensor, received)) = self.latest[i] {
                if f.camera_sequence < seq
                    || f.sensor_timestamp_ns < sensor
                    || (f.camera_sequence == seq && f.sensor_timestamp_ns != sensor)
                {
                    return self.fail("camera timing restarted or changed identity");
                }
                if f.camera_sequence == seq {
                    if mono - received > ns(self.policy.freshness_seconds) {
                        return self.fail("timing evidence stalled on monotonic deadline");
                    }
                    continue;
                }
            }
            self.latest[i] = Some((f.camera_sequence, f.sensor_timestamp_ns, mono));
        }
        let action = match self.control(master, slave, now, mono) {
            Ok(a) => a,
            Err(e) => return self.fail(&e),
        };
        let mut measurement = None;
        if self.observer.is_some()
            && (self.last_health == 0
                || mono - self.last_health >= ns(self.policy.evaluation_seconds))
        {
            match estimate_phase(
                master,
                slave,
                now,
                &self.policy,
                self.health_after,
                None,
                self.nominal,
            ) {
                Ok(mut m) => {
                    let o = self.observer.as_ref().unwrap();
                    m.drift_ns_per_second =
                        o.rate_us * 1e9 / self.model.as_ref().unwrap().reference_us;
                    m.predicted_phase_ns = (o.phase_us
                        + o.rate_us * self.model.as_ref().unwrap().lag() as f64)
                        * 1000.;
                    m.excursion = m.phase_ns.abs() > self.policy.excursion_ms * 1e6;
                    if m.excursion {
                        let since = *self.excursion_since.get_or_insert(mono);
                        if mono - since >= ns(self.policy.excursion_timeout_seconds) {
                            return self.fail("persistent phase nonconvergence");
                        }
                    } else {
                        self.excursion_since = None;
                    }
                    if !self.ready
                        && m.absolute_p95_ns <= self.policy.ready_p95_ms * 1e6
                        && m.absolute_max_ns <= self.policy.ready_max_ms * 1e6
                    {
                        if m.window_start_ns >= self.good_end {
                            self.good += 1;
                            self.good_end = m.window_end_ns;
                        }
                        self.ready = self.good >= self.policy.ready_windows;
                    } else if !self.ready {
                        self.good = 0;
                    }
                    m.ready_windows = self.good;
                    self.last_health = mono;
                    self.state = if self.ready { "tracking" } else { "qualifying" }.into();
                    measurement = Some(m);
                }
                Err(e) if e.kind == 3 => (),
                Err(e) => return self.fail(&e.message),
            }
        }
        self.result(measurement, action)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn requested_and_reported_change_without_a_sensor_response_is_not_identification() {
        let mut frames: Vec<_> = (0..20)
            .map(|i| FrameTiming {
                camera_sequence: i,
                sensor_timestamp_ns: 1_000_000_000 + 25_000_000 * i as i64,
                reported_duration_us: if (5..11).contains(&i) { 25016 } else { 25000 },
                ..Default::default()
            })
            .collect();
        let model = Model {
            reference_us: 25000.,
            follower_us: 25000.,
            baseline_request: 25000,
            baseline_report: 25000,
            noise_us: 0.,
            drift_us: 0.,
            steps: [None, None],
            probe_us: 16,
        };
        let pulse = Pulse {
            delta_us: 16,
            count: 6,
            ..Default::default()
        };
        assert!(model.response(&frames, 2, pulse, 8).is_none());
        for f in &mut frames {
            let affected = (f.camera_sequence as i64 - 4).clamp(0, 6);
            f.sensor_timestamp_ns += affected * 24_500;
        }
        let step = model.response(&frames, 2, pulse, 8).unwrap();
        assert_eq!(step.lag, 5);
        assert_eq!(step.shift_us, 24.5);
    }
}
