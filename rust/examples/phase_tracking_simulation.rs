//! Timestamp-only experiment. Run with cargo run --release --example phase_tracking_simulation.
//! The rig and its timing parameters are synthetic. The baseline uses the library controller.
//! Each policy sees the same per-frame disturbances. Only the scorer sees physical phase.
//! Sequence correspondence is known; both boards share the same clock. No drops,
//! timestamp resets, actuator clipping or changes of sensor mode are simulated.

use bcamsync_core::phase::{PhaseController, PhasePolicy};
use bcamsync_core::timing::{CorrectionStatus, FrameTiming, TimingSnapshot};
use serde_json::{json, Value};
use std::collections::VecDeque;

const EPOCH_NS: i64 = 1_000_000_000;
const TRIAL_SECONDS: usize = 180;
const SCORE_START_SECONDS: usize = 30;

#[derive(Clone, Copy)]
struct Rig {
    fps: usize,
    quantum_us: f64,
    quantizer_offset: f64, // fraction of a step above the baseline output
    delay: usize,          // frames from submission until first changed interval
    drift_ppm: f64,
    drift_change_ppm: f64,
    stamp_bias_us: f64,
    observation_noise_us: f64, // uniform half-width per camera
}

struct Model {
    rig: Rig,
    frame: usize,
    phase_us: f64,
    rng: u64,
    commands: VecDeque<i32>,
}

impl Model {
    fn new(rig: Rig, seed: u64) -> Self {
        Self {
            rig,
            frame: 0,
            phase_us: 1300.0,
            rng: seed,
            commands: VecDeque::from(vec![0; rig.delay]),
        }
    }

    fn noise(&mut self) -> f64 {
        self.rng = self.rng.wrapping_mul(6364136223846793005).wrapping_add(1);
        ((self.rng >> 11) as f64 / (1u64 << 53) as f64) * 2.0 - 1.0
    }

    fn schedule(&mut self, delta: i32, count: usize) {
        for i in 0..count {
            let slot = self.rig.delay - 1 + i;
            if slot >= self.commands.len() {
                self.commands.resize(slot + 1, 0);
            }
            assert_eq!(self.commands[slot], 0, "overlapping camera commands");
            self.commands[slot] = delta;
        }
    }

    fn next(&mut self) -> (i64, i64, f64) {
        self.frame += 1;
        let sec = self.frame as f64 / self.rig.fps as f64;
        let requested = self.commands.pop_front().unwrap_or(0) as f64;
        self.commands.push_back(0);
        let shift = (requested / self.rig.quantum_us + self.rig.quantizer_offset).round()
            * self.rig.quantum_us;
        let drift = self.rig.drift_ppm + self.rig.drift_change_ppm * (sec / 45.0).sin();
        self.phase_us += drift / self.rig.fps as f64 + shift;
        let physical_phase = self.phase_us + 1.5 * self.noise();
        let master = sec * 1e6 + self.rig.observation_noise_us * self.noise();
        let follower = sec * 1e6
            + physical_phase
            + self.rig.stamp_bias_us
            + self.rig.observation_noise_us * self.noise();
        (
            EPOCH_NS + (master * 1000.0).round() as i64,
            EPOCH_NS + (follower * 1000.0).round() as i64,
            physical_phase,
        )
    }
}

fn mean(xs: &[f64]) -> f64 {
    xs.iter().sum::<f64>() / xs.len() as f64
}

// Identify repeatable single-frame commands using only synthetic camera observations.
// The delay search covers 1–12 frames. The 10 unperturbed frames on either side
// estimate the local natural drift. Calibrate separately from the tracking trial.
fn calibrate(rig: Rig) -> (usize, Vec<(i32, f64)>, usize) {
    let mut model = Model::new(rig, 913);
    let mut actions = vec![(0, 0.0)];
    let mut delay_score = [0.0; 12];
    // Interleave signs at each level to limit the total calibration phase excursion.
    for magnitude in [1i32, 2, 4, 8, 16, 32, 64, 128] {
        let mut responses = [Vec::new(), Vec::new()];
        for _ in 0..6 {
            for (idx, sign) in [-1, 1].into_iter().enumerate() {
                let mut values = Vec::new();
                for frame in 0..32 {
                    if frame == 10 {
                        model.schedule(sign * magnitude, 1);
                    }
                    let (m, s, _) = model.next();
                    values.push((s - m) as f64 / 1000.0);
                }
                if magnitude == 128 {
                    for (lag, score) in delay_score.iter_mut().enumerate() {
                        *score += sign as f64 * (values[10 + lag] - values[9 + lag]);
                    }
                }
                let slope = ((mean(&values[5..10]) - mean(&values[0..5]))
                    + (mean(&values[27..32]) - mean(&values[22..27])))
                    / 10.0;
                responses[idx].push(mean(&values[22..32]) - mean(&values[..10]) - slope * 22.0);
            }
        }
        for (sign, xs) in [-1, 1].into_iter().zip(responses) {
            let average = mean(&xs);
            let sem = (xs.iter().map(|x| (x - average).powi(2)).sum::<f64>()
                / ((xs.len() - 1) * xs.len()) as f64)
                .sqrt();
            if sign as f64 * average > (3.0 * sem).max(1.0) {
                actions.push((sign * magnitude, average));
            }
        }
    }
    let lag = delay_score
        .iter()
        .enumerate()
        .max_by(|a, b| a.1.total_cmp(b.1))
        .unwrap()
        .0
        + 1;
    (lag, actions, model.frame)
}

struct Predictive {
    history: VecDeque<f64>,
    scheduled_total_us: f64,
    applied_total_us: f64,
    pending: VecDeque<(usize, f64)>,
    last_request: usize,
}

impl Predictive {
    fn new() -> Self {
        Self {
            history: VecDeque::new(),
            scheduled_total_us: 0.0,
            applied_total_us: 0.0,
            pending: VecDeque::new(),
            last_request: 0,
        }
    }

    fn command(
        &mut self,
        phase_us: f64,
        frame: usize,
        fps: usize,
        lag: usize,
        actions: &[(i32, f64)],
    ) -> i32 {
        while self.pending.front().is_some_and(|(due, _)| *due <= frame) {
            self.applied_total_us += self.pending.pop_front().unwrap().1;
        }
        self.history.push_back(phase_us - self.applied_total_us);
        if self.history.len() > 4 * fps {
            self.history.pop_front();
        }
        let interval = (fps / 4).max(lag + 1);
        if frame < 2 * fps || frame - self.last_request < interval {
            return 0;
        }
        let n = self.history.len() as f64;
        let centre = (n - 1.0) / 2.0;
        let avg = self.history.iter().sum::<f64>() / n;
        let slope = self
            .history
            .iter()
            .enumerate()
            .map(|(i, y)| (i as f64 - centre) * (y - avg))
            .sum::<f64>()
            / (n * (n * n - 1.0) / 12.0);
        // Predict to the middle of the next control interval. Remove the effects
        // already attributed to past commands before fitting the relative rate.
        let predicted =
            avg + slope * (centre + lag as f64 + interval as f64 / 2.0) + self.scheduled_total_us;
        let (request, change) = *actions
            .iter()
            .min_by(|a, b| (predicted + a.1).abs().total_cmp(&(predicted + b.1).abs()))
            .unwrap();
        if request != 0 {
            self.pending.push_back((frame + lag, change));
            self.scheduled_total_us += change;
            self.last_request = frame;
        }
        request
    }
}

fn append_frame(s: &mut TimingSnapshot, seq: usize, timestamp: i64, now: i64, keep: usize) {
    s.snapshot_timestamp_ns = now;
    if s.frames.len() >= keep {
        s.frames.remove(0);
    }
    s.frames.push(FrameTiming {
        camera_sequence: seq as u64,
        sensor_timestamp_ns: timestamp,
        timestamp_ns: timestamp,
        ..Default::default()
    });
}

fn p99(values: &mut [f64]) -> f64 {
    values.sort_by(f64::total_cmp);
    values[(values.len() * 99).div_ceil(100) - 1]
}

fn trial(
    rig: Rig,
    initial_phase_us: f64,
    deadband_us: Option<f64>,
    calibration: &(usize, Vec<(i32, f64)>, usize),
) -> Value {
    let mut model = Model::new(rig, 739);
    model.phase_us = initial_phase_us;
    let mut baseline = PhaseController::new(
        PhasePolicy {
            deadband_ms: deadband_us.unwrap_or(250.0) / 1000.0,
            ..Default::default()
        },
        1_000_000_000 / rig.fps as i64,
    )
    .unwrap();
    let mut predictive = Predictive::new();
    let (mut master, mut follower) = (TimingSnapshot::default(), TimingSnapshot::default());
    let mut restoration = None;
    let (mut commands, mut changed_frames) = (0, 0);
    let (mut physical, mut observed) = (Vec::new(), Vec::new());
    for k in 1..=rig.fps * TRIAL_SECONDS {
        let (m, s, phase) = model.next();
        let now = m.max(s) + 10_000;
        let measured = (s - m) as f64 / 1000.0;
        if k > rig.fps * SCORE_START_SECONDS {
            physical.push(phase.abs());
            observed.push(measured.abs());
        }
        let (delta, frames) = if deadband_us.is_none() {
            (
                predictive.command(measured, k, rig.fps, calibration.0, &calibration.1),
                1,
            )
        } else {
            append_frame(&mut master, k, m, now, 3 * rig.fps);
            append_frame(&mut follower, k, s, now, 3 * rig.fps);
            if let Some((due, id)) = restoration {
                if k == due {
                    follower.correction = CorrectionStatus {
                        state: "restored".into(),
                        correction_id: id,
                        restored_timestamp_ns: s,
                    };
                    restoration = None;
                }
            }
            let d = baseline.tick(&master, &follower, now, now, true);
            if !d.fault.is_empty() {
                return json!({"policy":"production","deadband_us":deadband_us,"fault":d.fault,"fault_frame":k});
            }
            if let Some(p) = d.action {
                follower.correction = CorrectionStatus {
                    state: "submitting".into(),
                    correction_id: p.correction_id,
                    restored_timestamp_ns: 0,
                };
                restoration = Some((k + rig.delay + p.count as usize, p.correction_id));
                (p.delta_us, p.count as usize)
            } else {
                (0, 0)
            }
        };
        if delta != 0 {
            model.schedule(delta, frames);
            if k > rig.fps * SCORE_START_SECONDS {
                commands += 1;
                changed_frames += frames;
            }
        }
    }
    let physical_mean = mean(&physical);
    json!({"policy":if deadband_us.is_none() {"predictive"} else {"production"},
        "deadband_us":deadband_us,
        "scored_frames":physical.len(),"physical_mae_us":physical_mean,
        "physical_p99_us":p99(&mut physical),"physical_max_us":physical.last(),
        "timestamp_p99_us":p99(&mut observed),"requests":commands,
        "requested_nonbaseline_frames":changed_frames})
}

fn main() {
    for fps in [15, 24, 30, 40, 60, 90] {
        let mut scenarios = vec![
            ("steady", 10., 0.2, 3, 3.83, 0., 0., 4.),
            ("reversing", 22., 0.2, 7, -3., 9., 0., 8.),
            ("timestamp_bias", 10., 0.2, 3, 3.83, 0., 60., 4.),
        ];
        // November Pi 5 timestamp characterization motivates the quantum, approximate
        // drift and observed 24/40 Hz response asymmetry. The queue is modelled as
        // a fixed ten-frame delay; observation noise, linearity and zero bias are assumptions.
        if fps == 24 {
            scenarios.push(("pi24_approximation", 24.5, 0.16, 10, -3.15, 0., 0., 4.));
        }
        if fps == 40 {
            scenarios.push(("pi40_approximation", 24.5, 0., 10, -3.35, 0., 0., 4.));
        }
        for (name, quantum, offset, delay, rate, warm, bias, noise) in scenarios {
            let rig = Rig {
                fps,
                quantum_us: quantum,
                quantizer_offset: offset,
                delay,
                drift_ppm: rate,
                drift_change_ppm: warm,
                stamp_bias_us: bias,
                observation_noise_us: noise,
            };
            let calibration = calibrate(rig);
            eprintln!("simulating {fps} Hz {name}");
            // Near-zero initial phase isolates maintenance from cold acquisition
            // when tightening only the deadband prevents the production startup.
            for initial_phase in [0., 1300.] {
                println!(
                    "{}",
                    json!({"scenario":name,"fps":fps,"simulated_quantum_us":quantum,
                "quantizer_offset_steps":offset,"initial_phase_us":initial_phase,
                "simulated_delay_frames":delay,"timestamp_bias_us":bias,"natural_rate_ppm":rate,
                "rate_variation_ppm":warm,"timestamp_noise_halfwidth_us":noise,
                "calibration_frames":calibration.2,"identified_delay_frames":calibration.0,
                "identified_actions":calibration.1,"scored_seconds":TRIAL_SECONDS-SCORE_START_SECONDS,
                "results":[trial(rig,initial_phase,Some(250.0),&calibration),trial(rig,initial_phase,Some(25.0),&calibration),
                           trial(rig,initial_phase,None,&calibration)]})
                );
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn commands_change_the_scheduled_interval_at_the_configured_latency() {
        let rig = Rig {
            fps: 24,
            quantum_us: 24.5,
            quantizer_offset: 0.16,
            delay: 10,
            drift_ppm: -3.15,
            drift_change_ppm: 0.,
            stamp_bias_us: 0.,
            observation_noise_us: 4.,
        };
        for (command, effective) in [
            (-32, -24.5),
            (-16, 0.),
            (-8, 0.),
            (8, 0.),
            (16, 24.5),
            (32, 24.5),
        ] {
            let mut treated = Model::new(rig, 8);
            let mut reference = Model::new(rig, 8);
            treated.schedule(command, 2);
            for k in 1..=15 {
                let (_, _, a) = treated.next();
                let (_, _, b) = reference.next();
                let affected = (0..2).filter(|i| k >= rig.delay + i).count();
                assert!((a - b - effective * affected as f64).abs() < 1e-7);
            }
        }
        let (delay, actions, _) = calibrate(rig);
        assert_eq!(delay, rig.delay);
        assert!(actions
            .iter()
            .any(|(cmd, impact)| *cmd == 16 && *impact > 15.0));
        assert!(actions
            .iter()
            .any(|(cmd, impact)| *cmd == -32 && *impact < -15.0));
    }
}
