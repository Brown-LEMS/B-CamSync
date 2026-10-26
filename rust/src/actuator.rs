use serde::{Deserialize, Serialize};
#[repr(C)]
#[derive(Clone, Copy, Default, Serialize, Deserialize)]
pub struct Submission {
    pub duration_us: i64,
    pub correction_id: u64,
    pub restoration: bool,
    pub round_id: u64,
}
#[derive(Clone, Serialize)]
pub struct State {
    pub session_id: String,
    pub state: String,
    pub round_id: u64,
    pub correction_id: u64,
    pub delta_us: i32,
    pub count: i32,
    pub submitted_frames: i32,
    pub expires_ns: i64,
    pub deadline_mono_ns: i64,
    pub restored_timestamp_ns: i64,
    pub restore_sequence: u64,
    pub cancelled: bool,
}
impl Default for State {
    fn default() -> Self {
        Self {
            session_id: String::new(),
            state: "idle".into(),
            round_id: 0,
            correction_id: 0,
            delta_us: 0,
            count: 0,
            submitted_frames: 0,
            expires_ns: 0,
            deadline_mono_ns: 0,
            restored_timestamp_ns: 0,
            restore_sequence: 0,
            cancelled: false,
        }
    }
}
#[derive(Default)]
pub struct PhaseActuator {
    pub state: State,
    restoration_submitted: bool,
}
impl PhaseActuator {
    fn identity(&self, session: &str, round: u64) -> Result<(), String> {
        if session != self.state.session_id || round != self.state.round_id || round == 0 {
            Err("wrong phase session or round".into())
        } else {
            Ok(())
        }
    }
    fn active(&self) -> bool {
        self.state.state == "submitting" || self.state.state == "settling"
    }
    pub fn bind(&mut self, session: &str, round: u64) -> Result<State, String> {
        if round == 0 || session.is_empty() {
            return Err("invalid phase binding".into());
        }
        let v = &self.state;
        if v.session_id == session && v.round_id == round {
            return Ok(v.clone());
        }
        if !v.session_id.is_empty() && (v.session_id != session || round <= v.round_id) {
            return Err("stale or different phase binding".into());
        }
        if self.active() {
            return Err("phase correction still active".into());
        }
        self.state = State {
            session_id: session.into(),
            round_id: round,
            ..Default::default()
        };
        self.restoration_submitted = false;
        Ok(self.state.clone())
    }
    pub fn query(&self, session: &str, round: u64, id: u64) -> Result<State, String> {
        self.identity(session, round)?;
        if id != 0 && id != self.state.correction_id {
            return Err("unknown phase correction".into());
        }
        Ok(self.state.clone())
    }
    #[allow(clippy::too_many_arguments)]
    pub fn adjust(
        &mut self,
        session: &str,
        round: u64,
        id: u64,
        delta: i32,
        count: i32,
        expires: i64,
        now: i64,
        mono: i64,
        baseline: i64,
        minimum: i64,
        maximum: i64,
    ) -> Result<State, String> {
        self.identity(session, round)?;
        let v = &self.state;
        if id != 0 && id == v.correction_id {
            if delta == v.delta_us && count == v.count && expires == v.expires_ns {
                return Ok(v.clone());
            }
            return Err("conflicting phase correction duplicate".into());
        }
        let duration = expires as i128 - now as i128;
        if id == 0
            || id <= v.correction_id
            || !(-1000..=1000).contains(&delta)
            || delta == 0
            || !(1..=16).contains(&count)
            || minimum <= 0
            || maximum < minimum
            || baseline < minimum
            || baseline > maximum
            || (delta < 0 && (baseline as i128 - minimum as i128) < -(delta as i128))
            || (delta > 0 && (maximum as i128 - baseline as i128) < delta as i128)
            || now <= 0
            || duration <= 0
            || duration > 5000000000
            || mono < 0
            || mono as i128 + duration > i64::MAX as i128
        {
            return Err("invalid, expired or out-of-range phase correction".into());
        }
        if self.active() {
            return Err("overlapping phase correction".into());
        }
        let v = &mut self.state;
        v.correction_id = id;
        v.delta_us = delta;
        v.count = count;
        v.submitted_frames = 0;
        v.expires_ns = expires;
        v.deadline_mono_ns = mono + duration as i64;
        v.restored_timestamp_ns = 0;
        v.restore_sequence = 0;
        v.cancelled = false;
        v.state = "submitting".into();
        self.restoration_submitted = false;
        Ok(v.clone())
    }
    pub fn cancel(&mut self, session: &str, round: u64) -> Result<State, String> {
        self.identity(session, round)?;
        self.cancel_active();
        Ok(self.state.clone())
    }
    pub fn cancel_active(&mut self) {
        if self.active() {
            self.state.cancelled = true;
        }
    }
    pub fn next(&mut self, baseline: i64, mono: i64) -> Submission {
        let v = &mut self.state;
        if v.state == "submitting" {
            if mono >= v.deadline_mono_ns {
                v.cancelled = true;
            }
            if !v.cancelled && v.submitted_frames < v.count {
                v.submitted_frames += 1;
                return Submission {
                    duration_us: baseline
                        .checked_add(v.delta_us as i64)
                        .expect("phase duration overflow"),
                    correction_id: v.correction_id,
                    restoration: false,
                    round_id: v.round_id,
                };
            }
            v.state = "settling".into();
        }
        if v.state == "settling" && !self.restoration_submitted {
            self.restoration_submitted = true;
            return Submission {
                duration_us: baseline,
                correction_id: v.correction_id,
                restoration: true,
                round_id: v.round_id,
            };
        }
        Submission {
            duration_us: baseline,
            correction_id: 0,
            restoration: false,
            round_id: v.round_id,
        }
    }
    pub fn completed(&mut self, s: Submission, sequence: u64, timestamp: i64) {
        let v = &mut self.state;
        if s.restoration
            && s.correction_id == v.correction_id
            && s.round_id == v.round_id
            && v.state == "settling"
        {
            v.restore_sequence = sequence;
            v.restored_timestamp_ns = timestamp;
            v.state = "restored".into();
        }
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn restoration_requires_observation_and_matching_round() {
        let mut a = PhaseActuator::default();
        a.bind("session", 1).unwrap();
        a.adjust(
            "session", 1, 1, -200, 2, 1000000001, 1, 0, 25000, 24000, 26000,
        )
        .unwrap();
        assert_eq!(a.next(25000, 0).duration_us, 24800);
        assert_eq!(a.next(25000, 1).duration_us, 24800);
        let restore = a.next(25000, 2);
        assert!(restore.restoration);
        assert_eq!(a.state.state, "settling");
        a.completed(
            Submission {
                round_id: 2,
                ..restore
            },
            10,
            100,
        );
        assert_eq!(a.state.state, "settling");
        a.completed(restore, 10, 100);
        assert_eq!(a.state.state, "restored");
        assert_eq!(a.state.restored_timestamp_ns, 100);
    }
    #[test]
    fn duplicate_expiry_and_overflow() {
        let mut a = PhaseActuator::default();
        a.bind("session", 1).unwrap();
        a.adjust("session", 1, 1, 100, 2, 1001, 1, 0, 25000, 24000, 26000)
            .unwrap();
        assert!(a
            .adjust("session", 1, 1, 101, 2, 1001, 1, 0, 25000, 24000, 26000)
            .is_err());
        assert!(a.next(25000, 1000).restoration);
        let mut a = PhaseActuator::default();
        a.bind("session", 1).unwrap();
        assert!(a
            .adjust("session", 1, 1, 100, 2, i64::MAX, 1, 0, 25000, 24000, 26000)
            .is_err());
    }
}
