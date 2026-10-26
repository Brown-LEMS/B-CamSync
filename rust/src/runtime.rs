use crate::config::{boot_time_ns, monotonic_ns, realtime_ns, NodeConfig, SyncConfig};
use crate::phase::{validate_timing, PhaseController};
use crate::timing::{CorrectionStatus, Pulse, TimingSnapshot};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::io::{Read, Write};
use std::net::{Ipv4Addr, Shutdown, SocketAddr, SocketAddrV4, TcpListener, TcpStream};
use std::os::fd::{AsRawFd, FromRawFd};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Condvar, Mutex};
use std::time::Duration;

type Result<T> = std::result::Result<T, String>;
const MESSAGE_LIMIT: usize = 1024 * 1024;

/// The host supplies capture adapters and optional operating-system clock setup.
/// No image storage or camera SDK object crosses this boundary.
pub trait Host: Send + Sync {
    fn call(&self, request: Value) -> Result<Value>;
}

#[derive(Clone, Default, Serialize, Deserialize)]
pub struct ClockEstimate {
    pub valid: bool,
    pub offset_ns: i64,
    pub uncertainty_ns: i64,
    #[serde(default)]
    pub fault: String,
}
#[derive(Clone, Serialize, Deserialize)]
pub struct UnitStatus {
    #[serde(rename = "id")]
    pub unit_id: String,
    pub state: String,
    pub ready: bool,
    pub fault: String,
    #[serde(rename = "phase")]
    pub phase_ms: f64,
    #[serde(rename = "p95")]
    pub p95_ms: f64,
    #[serde(rename = "max")]
    pub max_ms: f64,
    #[serde(rename = "measured_at")]
    pub measurement_end_ns: i64,
}
impl Default for UnitStatus {
    fn default() -> Self {
        Self {
            unit_id: String::new(),
            state: "warming".into(),
            ready: false,
            fault: String::new(),
            phase_ms: 0.0,
            p95_ms: 0.0,
            max_ms: 0.0,
            measurement_end_ns: 0,
        }
    }
}
#[derive(Clone, Serialize, Deserialize)]
pub struct GroupStatus {
    pub state: String,
    pub ready: bool,
    pub fault: String,
    pub units: Vec<UnitStatus>,
    pub waiting_nodes: Vec<String>,
    pub stop_node: String,
    pub stop_reason: String,
}
impl Default for GroupStatus {
    fn default() -> Self {
        Self {
            state: "stopped".into(),
            ready: false,
            fault: String::new(),
            units: Vec::new(),
            waiting_nodes: Vec::new(),
            stop_node: String::new(),
            stop_reason: String::new(),
        }
    }
}
struct Published {
    group: GroupStatus,
    clock: ClockEstimate,
    clock_sampled_at: i64,
}
struct RunState {
    config: SyncConfig,
    resolved: SyncConfig,
    local: String,
    control_session: String,
    session: String,
    round: u64,
    local_units: Vec<String>,
    bound: BTreeSet<String>,
    corrections: BTreeMap<String, u64>,
    peers: BTreeMap<String, Connection>,
    requests: BTreeMap<String, u64>,
    follower: Option<Connection>,
    server: Option<TcpListener>,
    stop_request_id: u64,
    units_cleaned: bool,
    startup_deadline: i64,
    join_deadline: Option<i64>,
    clock_prepared: bool,
    membership_complete: bool,
    active: bool,
    coordinator: bool,
}
pub struct Runtime {
    host: Box<dyn Host>,
    run: Mutex<RunState>,
    published: Mutex<Published>,
    changed: Condvar,
    stopping: AtomicBool,
    common: bool,
    local: String,
    clock_max_age_ns: i64,
}
fn shifted(value: i64, offset: i64) -> Result<i64> {
    value
        .checked_add(offset)
        .ok_or_else(|| "clock mapping exceeds timestamp range".into())
}
fn ns(seconds: f64) -> i64 {
    (seconds * 1e9) as i64
}
fn decode<T: serde::de::DeserializeOwned>(value: Value) -> Result<T> {
    serde_json::from_value(value).map_err(|e| e.to_string())
}
impl Runtime {
    pub fn new(
        config: SyncConfig,
        local: String,
        identity: String,
        mut local_units: Vec<String>,
        host: Box<dyn Host>,
    ) -> Result<Self> {
        config.validate()?;
        config.node(&local)?;
        local_units.sort();
        let mut expected: Vec<_> = config
            .units
            .iter()
            .filter(|u| u.node_id == local)
            .map(|u| u.id.clone())
            .collect();
        expected.sort();
        if expected != local_units {
            return Err("sync interfaces do not match local topology".into());
        }
        let common = config.nodes.len() > 1;
        let coordinator = config.reference_node()? == local;
        let clock_max_age_ns = ns(config.clock_max_age_seconds);
        Ok(Self {
            host,
            run: Mutex::new(RunState {
                resolved: config.clone(),
                config,
                local: local.clone(),
                control_session: identity,
                session: String::new(),
                round: 0,
                local_units,
                bound: BTreeSet::new(),
                corrections: BTreeMap::new(),
                peers: BTreeMap::new(),
                requests: BTreeMap::new(),
                follower: None,
                server: None,
                stop_request_id: 0,
                units_cleaned: false,
                startup_deadline: 0,
                join_deadline: None,
                clock_prepared: false,
                membership_complete: false,
                active: false,
                coordinator,
            }),
            published: Mutex::new(Published {
                group: GroupStatus::default(),
                clock: ClockEstimate::default(),
                clock_sampled_at: 0,
            }),
            changed: Condvar::new(),
            stopping: AtomicBool::new(true),
            common,
            local,
            clock_max_age_ns,
        })
    }
    pub fn begin(&self) -> Result<Value> {
        let mut s = self.run.lock().unwrap();
        s.round = s
            .round
            .checked_add(1)
            .ok_or("synchronization round exhausted")?;
        self.stopping.store(false, Ordering::Release);
        s.units_cleaned = false;
        s.clock_prepared = false;
        s.membership_complete = false;
        s.active = false;
        s.resolved = s.config.clone();
        s.stop_request_id = 0;
        s.corrections.clear();
        s.bound.clear();
        {
            let mut p = self.published.lock().unwrap();
            p.clock = ClockEstimate {
                valid: !self.common,
                ..ClockEstimate::default()
            };
            p.clock_sampled_at = 0;
        }
        s.startup_deadline = shifted(monotonic_ns(), ns(s.config.startup_timeout_seconds))?;
        self.publish(GroupStatus {
            state: "starting".into(),
            waiting_nodes: s
                .config
                .nodes
                .iter()
                .filter(|n| n.id != s.local)
                .map(|n| n.id.clone())
                .collect(),
            ..GroupStatus::default()
        });
        s.session = if s.coordinator {
            format!("{}-{}", s.control_session, s.round)
        } else {
            String::new()
        };
        Ok(json!({"identity": s.control_session, "round": s.round}))
    }
    pub fn set_node_addresses(&self, addresses: BTreeMap<String, String>) -> Result<()> {
        let mut s = self.run.lock().unwrap();
        let mut c = s.config.clone();
        if addresses.len() != c.nodes.len() {
            return Err("addresses must identify every configured node".into());
        }
        for node in &mut c.nodes {
            let address = addresses
                .get(&node.id)
                .ok_or_else(|| format!("missing node address: {}", node.id))?;
            let ip = address
                .parse::<Ipv4Addr>()
                .map_err(|_| "node address must be a unicast IPv4 address")?;
            if u32::from(ip) == 0 || ip.octets()[0] >= 224 {
                return Err("node address must be a unicast IPv4 address".into());
            }
            node.address = address.clone();
        }
        c.network_mode = "existing".into();
        c.validate()?;
        s.config = c;
        Ok(())
    }
    fn publish(&self, group: GroupStatus) {
        let mut p = self.published.lock().unwrap();
        if self.stopping()
            && !p.group.stop_reason.is_empty()
            && group.state != "stopped"
            && group.state != "fault"
        {
            return;
        }
        p.group = group;
        self.changed.notify_all();
    }
    pub fn status(&self) -> GroupStatus {
        self.published.lock().unwrap().group.clone()
    }
    pub fn fail(&self, reason: &str) {
        let mut p = self.published.lock().unwrap();
        p.group.state = "fault".into();
        p.group.ready = false;
        if p.group.stop_reason.is_empty() {
            p.group.stop_node = self.local.clone();
            p.group.stop_reason = "fault".into();
        }
        self.stopping.store(true, Ordering::Release);
        if p.group.fault.is_empty() {
            p.group.fault = reason.into();
        } else {
            p.group.fault.push_str("; ");
            p.group.fault.push_str(reason);
        }
        for unit in &mut p.group.units {
            unit.ready = false;
        }
        self.changed.notify_all();
    }
    pub fn stopping(&self) -> bool {
        self.stopping.load(Ordering::Acquire)
    }
    pub fn request_stop(&self) {
        self.stop_from(&self.local, "requested");
    }
    fn stop_from(&self, node: &str, reason: &str) {
        let mut p = self.published.lock().unwrap();
        if p.group.state == "stopped" && self.stopping() {
            return;
        }
        if p.group.stop_reason.is_empty() {
            p.group.stop_node = node.into();
            p.group.stop_reason = reason.into();
        }
        p.group.ready = false;
        if p.group.fault.is_empty() {
            p.group.state = "stopping".into();
        }
        for unit in &mut p.group.units {
            unit.ready = false;
        }
        self.stopping.store(true, Ordering::Release);
        self.changed.notify_all();
    }
    pub fn wait_for_stop(&self) {
        let guard = self.published.lock().unwrap();
        drop(
            self.changed
                .wait_while(guard, |_| !self.stopping())
                .unwrap(),
        );
    }
    pub fn wait_ready(&self, timeout: Duration) -> Result<bool> {
        let guard = self.published.lock().unwrap();
        let (p, _) = self
            .changed
            .wait_timeout_while(guard, timeout, |p| {
                !p.group.ready && p.group.fault.is_empty() && !self.stopping()
            })
            .unwrap();
        if !p.group.fault.is_empty() {
            return Err(p.group.fault.clone());
        }
        Ok(p.group.ready)
    }
    fn pause(&self, duration: Duration) {
        let p = self.published.lock().unwrap();
        drop(
            self.changed
                .wait_timeout_while(p, duration, |_| !self.stopping())
                .unwrap(),
        );
    }
    pub fn clock(&self) -> ClockEstimate {
        let p = self.published.lock().unwrap();
        let mut clock = p.clock.clone();
        if self.common
            && (p.clock_sampled_at == 0
                || monotonic_ns() - p.clock_sampled_at > self.clock_max_age_ns)
        {
            clock.valid = false;
        }
        clock
    }
    fn update_clock(&self) -> Result<()> {
        let estimate = self
            .host
            .call(json!({"op": "platform_sample"}))
            .and_then(decode::<ClockEstimate>)
            .unwrap_or_else(|fault| ClockEstimate {
                fault,
                ..ClockEstimate::default()
            });
        {
            let mut p = self.published.lock().unwrap();
            p.clock = estimate.clone();
            p.clock_sampled_at = monotonic_ns();
        }
        if !estimate.valid {
            return Err(if estimate.fault.is_empty() {
                "common clock is unavailable".into()
            } else {
                estimate.fault
            });
        }
        Ok(())
    }
    fn capture_health(&self, s: &RunState) -> Result<()> {
        for unit in &s.local_units {
            let fault: String = decode(self.host.call(json!({"op": "fault", "unit": unit}))?)?;
            if !fault.is_empty() {
                return Err(format!("{unit}: {fault}"));
            }
        }
        Ok(())
    }
    fn health(&self, s: &RunState) -> Result<()> {
        self.capture_health(s)?;
        self.update_clock()
    }
    fn common_now(&self) -> Result<i64> {
        let clock = self.clock();
        if !clock.valid {
            return Err("common clock is unavailable".into());
        }
        shifted(boot_time_ns(), clock.offset_ns)
    }
    fn snapshot(&self, unit: &str) -> Result<TimingSnapshot> {
        let snapshot: TimingSnapshot =
            decode(self.host.call(json!({"op": "snapshot", "unit": unit}))?)?;
        if snapshot.unit_id != unit {
            return Err("snapshot unit identity differs".into());
        }
        Ok(snapshot)
    }
    fn mapped_snapshot(&self, unit: &str) -> Result<TimingSnapshot> {
        let mut result = self.snapshot(unit)?;
        let clock = self.clock();
        if !clock.valid {
            return Err("common clock is unavailable".into());
        }
        result.snapshot_timestamp_ns = shifted(result.snapshot_timestamp_ns, clock.offset_ns)?;
        for frame in &mut result.frames {
            frame.timestamp_ns = shifted(frame.timestamp_ns, clock.offset_ns)?;
            frame.mapping_span_ns = shifted(frame.mapping_span_ns, clock.uncertainty_ns)?;
        }
        if result.correction.restored_timestamp_ns != 0 {
            result.correction.restored_timestamp_ns =
                shifted(result.correction.restored_timestamp_ns, clock.offset_ns)?;
        }
        Ok(result)
    }
    fn adjust_local(
        &self,
        s: &mut RunState,
        unit: &str,
        mut pulse: Pulse,
    ) -> Result<CorrectionStatus> {
        let clock = self.clock();
        if !clock.valid {
            return Err("common clock is unavailable".into());
        }
        pulse.expires_ns = pulse
            .expires_ns
            .checked_sub(clock.offset_ns)
            .ok_or("clock mapping exceeds timestamp range")?;
        let ack: CorrectionStatus = decode(
            self.host
                .call(json!({"op": "adjust", "unit": unit, "pulse": pulse}))?,
        )?;
        if ack.correction_id != pulse.correction_id
            || !matches!(ack.state.as_str(), "submitting" | "settling" | "restored")
        {
            return Err("correction acknowledgement differs".into());
        }
        s.corrections.insert(unit.into(), pulse.correction_id);
        Ok(ack)
    }
    fn cancel_units(&self, s: &mut RunState) {
        if s.units_cleaned {
            return;
        }
        s.units_cleaned = true;
        let mut pending = Vec::new();
        for unit in &s.bound {
            match self.host.call(json!({"op": "cancel", "unit": unit})) {
                Ok(_) => pending.push(unit.clone()),
                Err(error) => self.fail(&format!("{unit}: {error}")),
            }
        }
        let deadline = monotonic_ns().saturating_add(ns(s.config.request_timeout_seconds));
        while !pending.is_empty() {
            pending.retain(|unit| {
                let result = self.snapshot(unit).and_then(|observed| {
                    let correction = observed.correction;
                    if s.corrections
                        .get(unit)
                        .is_some_and(|id| *id != correction.correction_id)
                    {
                        return Err("restoration correction identity differs".into());
                    }
                    if matches!(correction.state.as_str(), "submitting" | "settling") {
                        if monotonic_ns() >= deadline {
                            return Err("baseline restoration deadline exceeded".into());
                        }
                        return Ok(true);
                    }
                    if (correction.state == "idle" && !s.corrections.contains_key(unit))
                        || (matches!(correction.state.as_str(), "restored" | "cancelled")
                            && correction.restored_timestamp_ns > 0
                            && correction.restored_timestamp_ns <= observed.snapshot_timestamp_ns)
                    {
                        return Ok(false);
                    }
                    Err("invalid baseline restoration feedback".into())
                });
                match result {
                    Ok(wait) => wait,
                    Err(error) => {
                        self.fail(&format!("{unit}: {error}"));
                        false
                    }
                }
            });
            if !pending.is_empty() {
                std::thread::sleep(Duration::from_millis(10));
            }
        }
    }
    pub fn run(&self) {
        let mut s = self.run.lock().unwrap();
        let result =
            std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| self.run_inner(&mut s)))
                .unwrap_or_else(|_| Err("unknown synchronization failure".into()));
        if let Err(error) = result {
            let cancelled = self.stopping()
                && matches!(
                    error.as_str(),
                    "peer requested group shutdown"
                        | "group synchronization is stopping"
                        | "node platform startup cancelled"
                        | "network session operation cancelled"
                );
            if !cancelled && self.status().fault.is_empty() {
                self.fail(&error);
            }
        }
        self.cancel_units(&mut s);
        if s.coordinator {
            let peers: Vec<_> = s.peers.keys().cloned().collect();
            for node in peers {
                let status = self.status();
                // Each remote adapter may need a complete request budget to restore its baseline.
                s.peers.get_mut(&node).unwrap().timeout =
                    2.0 * s.config.request_timeout_seconds + 1.0;
                match self.rpc(
                    &mut s,
                    &node,
                    json!({"command": "stop", "stop_node": status.stop_node,
                    "stop_reason": status.stop_reason, "fault": status.fault}),
                ) {
                    Ok(reply) if reply["stopped"] == true => {}
                    Ok(reply) => self.fail(&format!(
                        "{node}: {}",
                        reply["fault"]
                            .as_str()
                            .unwrap_or("invalid stop acknowledgement")
                    )),
                    Err(error) => {
                        self.fail(&format!("{node}: shutdown acknowledgement failed: {error}"))
                    }
                }
            }
        } else if let Some(channel) = &s.follower {
            // Faults close the channel; a requested stop is acknowledged by run_follower first.
            if s.stop_request_id == 0 {
                channel.shutdown();
            }
        }
    }
    fn run_inner(&self, s: &mut RunState) -> Result<()> {
        for unit in &s.local_units {
            if self.stopping() {
                break;
            }
            self.host.call(json!({"op": "bind", "unit": unit, "identity": s.control_session, "round": s.round}))?;
            s.bound.insert(unit.clone());
        }
        if !self.stopping() {
            s.resolved = decode(self.host.call(json!({"op": "platform_start", "config": s.resolved, "deadline": s.startup_deadline}))?)?;
        }
        let _ = self.update_clock();
        s.join_deadline = s
            .config
            .join_timeout_seconds
            .map(|t| monotonic_ns().saturating_add(ns(t)));
        if !self.stopping() {
            if s.coordinator {
                self.run_coordinator(s)?;
            } else {
                self.run_follower(s)?;
            }
        }
        Ok(())
    }
    pub fn finish(&self) {
        let mut s = self.run.lock().unwrap();
        // The owning access layer has completed capture cleanup before group acknowledgement.
        if s.stop_request_id != 0 {
            let request_id = s.stop_request_id;
            if let Some(channel) = &mut s.follower {
                let status = self.status();
                if let Err(error) = channel.send(&json!({"request_id": request_id, "ok": true, "result": {"stopped": status.fault.is_empty(), "fault": status.fault}})) { self.fail(&error); }
            }
            s.stop_request_id = 0;
        }
        s.follower = None;
        for peer in s.peers.values() {
            peer.shutdown();
        }
        s.peers.clear();
        s.requests.clear();
        s.server = None;
        if let Err(error) = self.host.call(json!({"op": "platform_stop"})) {
            self.fail(&error);
        }
        self.published.lock().unwrap().clock.valid = false;
        let mut status = self.status();
        status.ready = false;
        for unit in &mut status.units {
            unit.ready = false;
        }
        if status.fault.is_empty() {
            status.state = "stopped".into();
        }
        status.waiting_nodes.clear();
        let warning = format!("WARNING: Synchronization stopped by {} ({}). Acquisition continues; subsequent frames are not guaranteed to be synchronized.{}",
            status.stop_node, status.stop_reason,
            if status.fault.is_empty() { String::new() } else { format!(" Fault: {}", status.fault) });
        self.publish(status);
        eprintln!("{warning}");
    }
    fn peer_fault(&self, node: &str, error: String) -> String {
        self.stop_from(node, "fault");
        self.fail(&format!("{node}: {error}"));
        error
    }
    fn rpc(&self, s: &mut RunState, node: &str, mut message: Value) -> Result<Value> {
        let stopping_command = message["command"] == "stop";
        let hello_command = message["command"] == "hello";
        if self.stopping() && !stopping_command && !hello_command {
            return Err("group synchronization is stopping".into());
        }
        let id = s.requests.entry(node.into()).or_default();
        *id = id
            .checked_add(1)
            .ok_or("control request identity exhausted")?;
        let request_id = *id;
        message["request_id"] = json!(request_id);
        message["session"] = json!(s.session);
        let channel = s.peers.get_mut(node).ok_or("missing control peer")?;
        let transport_error = |error| {
            if hello_command && self.stopping() && self.status().fault.is_empty() {
                "group synchronization is stopping".into()
            } else {
                self.peer_fault(node, error)
            }
        };
        channel.send(&message).map_err(transport_error)?;
        let response = channel.receive().map_err(transport_error)?;
        if response["request_id"].as_u64() != Some(request_id) {
            return Err(self.peer_fault(node, "out-of-order control response".into()));
        }
        if response["stop_requested"] == true && !stopping_command {
            let reason = response["stop_reason"]
                .as_str()
                .ok_or("missing peer stop reason")?;
            self.stop_from(node, reason);
            let fault = response["fault"].as_str().ok_or("missing peer fault")?;
            if !fault.is_empty() {
                self.fail(&format!("{node}: {fault}"));
            }
            if !hello_command {
                return Err("peer requested group shutdown".into());
            }
        }
        if response["ok"].as_bool() != Some(true) {
            return Err(self.peer_fault(
                node,
                response["error"]
                    .as_str()
                    .unwrap_or("invalid control response")
                    .into(),
            ));
        }
        Ok(response["result"].clone())
    }
    fn run_follower(&self, s: &mut RunState) -> Result<()> {
        s.server = Some(listener(s.resolved.node(&s.local)?)?);
        let mut waiting = self.status();
        waiting.state = "waiting".into();
        self.publish(waiting);
        let mut next_health = monotonic_ns();
        let client = loop {
            if self.stopping() {
                return Ok(());
            }
            if monotonic_ns() >= next_health {
                self.capture_health(s)?;
                next_health = monotonic_ns() + 1_000_000_000;
            }
            let listener = s.server.as_ref().unwrap();
            if poll_fd(listener.as_raw_fd(), libc::POLLIN, 100)? {
                match listener.accept() {
                    Ok((stream, _)) => break stream,
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {}
                    Err(error) => return Err(error.to_string()),
                }
            }
            self.check_join_deadline(s)?;
        };
        s.follower = Some(Connection::new(client, s.config.request_timeout_seconds)?);
        let mut expected = 1_u64;
        let mut last_command = monotonic_ns();
        let idle_limit =
            ns(2.0 + s.config.request_timeout_seconds * (s.config.nodes.len() as f64 + 1.0));
        let mut stop_deadline = None;
        loop {
            if self.stopping() {
                let deadline = stop_deadline.get_or_insert_with(|| {
                    monotonic_ns()
                        + ns(2.0
                            + (s.config.nodes.len() as f64 + 3.0)
                                * s.config.request_timeout_seconds)
                });
                if monotonic_ns() > *deadline {
                    return Err("group stop confirmation deadline exceeded".into());
                }
            } else if monotonic_ns() >= next_health {
                let health = self.capture_health(s).and_then(|_| {
                    if s.active {
                        self.update_clock()
                    } else {
                        Ok(())
                    }
                });
                if let Err(error) = health {
                    self.fail(&error);
                }
                next_health = monotonic_ns() + 1_000_000_000;
            }
            if !self.stopping() {
                if !s.membership_complete {
                    self.check_join_deadline(s)?;
                } else if !self.status().ready && monotonic_ns() > s.startup_deadline {
                    return Err("group startup calibration deadline exceeded".into());
                }
            }
            if !s.follower.as_ref().unwrap().readable(100)? {
                if monotonic_ns() - last_command > idle_limit {
                    return Err("coordinator heartbeat deadline exceeded".into());
                }
                continue;
            }
            let incoming = s.follower.as_mut().unwrap().receive()?;
            last_command = monotonic_ns();
            let id = incoming["request_id"]
                .as_u64()
                .ok_or("invalid control request identity")?;
            let result = (|| {
                if id != expected {
                    return Err("out-of-order command".into());
                }
                expected = expected
                    .checked_add(1)
                    .ok_or("control request identity exhausted")?;
                let command = incoming["command"]
                    .as_str()
                    .ok_or("invalid control command")?;
                if s.session.is_empty() {
                    if command != "hello" || incoming["topology"] != s.config.topology() {
                        return Err("group synchronization configuration differs".into());
                    }
                    s.session = incoming["session"]
                        .as_str()
                        .ok_or("invalid session identity")?
                        .into();
                    if s.session.is_empty() || s.session.len() > 128 {
                        return Err("invalid session identity".into());
                    }
                    return Ok(json!({"node": s.local}));
                }
                if incoming["session"].as_str() != Some(s.session.as_str()) {
                    return Err("session identity differs".into());
                }
                if self.stopping() && command != "stop" {
                    return Ok(Value::Null);
                }
                match command {
                    "prepare" => {
                        let status: GroupStatus = decode(incoming["status"].clone())?;
                        if status.waiting_nodes.is_empty() && !s.membership_complete {
                            s.membership_complete = true;
                            s.join_deadline = None;
                            s.startup_deadline =
                                monotonic_ns().saturating_add(ns(s.config.startup_timeout_seconds));
                        }
                        self.publish(status);
                        let clock = if incoming["clock"] == true {
                            self.prepare_clock(s)?;
                            self.update_clock().is_ok()
                        } else {
                            false
                        };
                        Ok(json!({"clock_valid": clock}))
                    }
                    "activate" => {
                        s.active = true;
                        Ok(json!({"active": true}))
                    }
                    "snapshots" => {
                        self.health(s)?;
                        let snapshots: Vec<_> = s
                            .local_units
                            .iter()
                            .map(|unit| self.mapped_snapshot(unit))
                            .collect::<Result<_>>()?;
                        Ok(
                            json!({"node": s.local, "snapshots": snapshots, "checked_at": realtime_ns()}),
                        )
                    }
                    "adjust" => {
                        let unit = incoming["unit"].as_str().ok_or("invalid control unit")?;
                        if unit == s.config.reference_unit {
                            return Err("reference unit cannot be adjusted".into());
                        }
                        let pulse = Pulse {
                            correction_id: incoming["id"]
                                .as_u64()
                                .ok_or("invalid correction identity")?,
                            delta_us: incoming["delta"]
                                .as_i64()
                                .ok_or("invalid correction delta")?
                                as _,
                            count: incoming["count"]
                                .as_u64()
                                .ok_or("invalid correction count")?
                                as _,
                            expires_ns: incoming["expires"]
                                .as_i64()
                                .ok_or("invalid correction expiry")?,
                        };
                        let ack = self.adjust_local(s, unit, pulse)?;
                        Ok(json!({"id": ack.correction_id, "state": ack.state}))
                    }
                    "status" => {
                        self.publish(decode(incoming["status"].clone())?);
                        Ok(json!({"stored": true}))
                    }
                    "stop" => {
                        self.stop_from(
                            incoming["stop_node"]
                                .as_str()
                                .ok_or("missing stop origin")?,
                            incoming["stop_reason"]
                                .as_str()
                                .ok_or("missing stop reason")?,
                        );
                        let fault = incoming["fault"].as_str().ok_or("missing stop fault")?;
                        if !fault.is_empty() {
                            self.fail(fault);
                        }
                        s.stop_request_id = id;
                        Ok(Value::Null)
                    }
                    _ => Err("unknown control command".into()),
                }
            })();
            if s.stop_request_id != 0 {
                return Ok(());
            }
            let response = match result {
                Ok(result) => {
                    let status = self.status();
                    json!({"request_id": id, "ok": true, "result": result,
                        "stop_requested": self.stopping(), "stop_reason": status.stop_reason, "fault": status.fault})
                }
                Err(error) => {
                    s.follower
                        .as_mut()
                        .unwrap()
                        .send(&json!({"request_id": id, "ok": false, "error": error}))?;
                    return Err(error);
                }
            };
            s.follower.as_mut().unwrap().send(&response)?;
        }
    }
    fn check_join_deadline(&self, s: &RunState) -> Result<()> {
        if s.join_deadline
            .is_some_and(|deadline| monotonic_ns() > deadline)
        {
            return Err("group node join timeout".into());
        }
        Ok(())
    }
    fn resolve(&self, s: &mut RunState) -> Result<()> {
        s.resolved = decode(self.host.call(json!({"op": "platform_resolve"}))?)?;
        Ok(())
    }
    fn prepare_clock(&self, s: &mut RunState) -> Result<()> {
        if !s.clock_prepared {
            self.resolve(s)?;
            if s.resolved.nodes.iter().any(|n| n.address.is_empty()) && self.common {
                return Ok(());
            }
            self.host
                .call(json!({"op": "platform_clock", "config": s.resolved}))?;
            s.clock_prepared = true;
        }
        Ok(())
    }
    fn run_coordinator(&self, s: &mut RunState) -> Result<()> {
        loop {
            if self.stopping() {
                return Ok(());
            }
            self.capture_health(s)?;
            self.resolve(s)?;
            for node in s.resolved.nodes.clone() {
                if self.stopping() {
                    break;
                }
                if node.id == s.local || s.peers.contains_key(&node.id) || node.address.is_empty() {
                    continue;
                }
                // Short connection attempts keep already joined peers responsive while others start.
                if let Ok(connection) =
                    Connection::connect(&node, s.config.request_timeout_seconds.min(0.1))
                {
                    s.peers.insert(node.id.clone(), connection);
                    s.peers.get_mut(&node.id).unwrap().timeout = s.config.request_timeout_seconds;
                    let hello = match self.rpc(
                        s,
                        &node.id,
                        json!({"command": "hello", "topology": s.config.topology()}),
                    ) {
                        Ok(hello) => hello,
                        Err(error) => {
                            // An unacknowledged connection is not a member of this round.
                            s.peers.remove(&node.id);
                            return Err(error);
                        }
                    };
                    if hello["node"] != node.id {
                        return Err("peer node identity differs".into());
                    }
                }
            }
            let waiting_nodes: Vec<_> = s
                .config
                .nodes
                .iter()
                .filter(|n| n.id != s.local && !s.peers.contains_key(&n.id))
                .map(|n| n.id.clone())
                .collect();
            let joined = waiting_nodes.is_empty();
            let status = GroupStatus {
                state: if joined { "clock" } else { "waiting" }.into(),
                waiting_nodes,
                ..GroupStatus::default()
            };
            self.publish(status.clone());
            if joined {
                s.membership_complete = true;
                s.join_deadline = None;
                s.startup_deadline =
                    monotonic_ns().saturating_add(ns(s.config.startup_timeout_seconds));
            }
            let nodes: Vec<_> = s.peers.keys().cloned().collect();
            for node in nodes {
                self.rpc(
                    s,
                    &node,
                    json!({"command": "prepare", "clock": false, "status": status}),
                )?;
            }
            if joined {
                break;
            }
            self.check_join_deadline(s)?;
            self.pause(Duration::from_millis(100));
        }
        // Control is already connected while each node acquires its clock mapping.
        let status = self.status();
        loop {
            if self.stopping() {
                return Ok(());
            }
            self.capture_health(s)?;
            self.prepare_clock(s)?;
            let mut valid = self.update_clock().is_ok();
            let nodes: Vec<_> = s.peers.keys().cloned().collect();
            for node in nodes {
                valid &= self.rpc(
                    s,
                    &node,
                    json!({"command": "prepare", "clock": true, "status": status}),
                )?["clock_valid"]
                    == true;
            }
            if valid {
                break;
            }
            if monotonic_ns() > s.startup_deadline {
                return Err("group clock startup deadline exceeded".into());
            }
            self.pause(Duration::from_millis(100));
        }
        s.active = true;
        let nodes: Vec<_> = s.peers.keys().cloned().collect();
        for node in nodes {
            self.rpc(s, &node, json!({"command": "activate"}))?;
        }
        let mut policy = s.config.phase_policy.clone();
        policy.max_mapping_span_ns = policy
            .max_mapping_span_ns
            .min((s.config.clock_max_offset_ms * 1e6).ceil() as i64);
        let mut controllers = BTreeMap::new();
        for unit in &s.config.units {
            if unit.id != s.config.reference_unit {
                let mut controller =
                    PhaseController::new(policy.clone(), 1_000_000_000 / s.config.fps as i64)
                        .map_err(|e| e.to_string())?;
                controller
                    .reset(Some(monotonic_ns()), self.common_now()?)
                    .map_err(|e| e.to_string())?;
                controllers.insert(unit.id.clone(), controller);
            }
        }
        let mut next = 0;
        let mut last_metrics: BTreeMap<String, UnitStatus> = BTreeMap::new();
        while !self.stopping() {
            if monotonic_ns() < next {
                self.pause(Duration::from_millis(20));
                continue;
            }
            self.health(s)?;
            let mut snapshots = BTreeMap::new();
            for unit in &s.local_units {
                snapshots.insert(unit.clone(), self.mapped_snapshot(unit)?);
            }
            for node in s.config.nodes.clone() {
                if node.id == s.local {
                    continue;
                }
                let result = self.rpc(s, &node.id, json!({"command": "snapshots"}))?;
                if result["node"] != node.id {
                    return Err("snapshot node identity differs".into());
                }
                let rows = result["snapshots"]
                    .as_array()
                    .ok_or("invalid node timing snapshots")?;
                for row in rows {
                    let snapshot: TimingSnapshot = decode(row.clone())?;
                    if snapshot.frames.len() > 512 {
                        return Err("invalid timing window size".into());
                    }
                    if s.config.unit(&snapshot.unit_id)?.node_id != node.id
                        || snapshots.contains_key(&snapshot.unit_id)
                    {
                        return Err("snapshot unit identity differs".into());
                    }
                    snapshots.insert(snapshot.unit_id.clone(), snapshot);
                }
            }
            if snapshots.len() != s.config.units.len() {
                return Err("incomplete group timing snapshots".into());
            }
            let mut status = GroupStatus {
                state: "calibrating".into(),
                ready: true,
                ..GroupStatus::default()
            };
            for unit in s.config.units.clone() {
                let mut us = last_metrics.get(&unit.id).cloned().unwrap_or_default();
                us.unit_id = unit.id.clone();
                if unit.id == s.config.reference_unit {
                    us.state = "reference".into();
                    us.ready = false;
                    match validate_timing(&snapshots[&unit.id], self.common_now()?, &policy) {
                        Ok(_) => us.ready = true,
                        Err(error) if error.kind == 3 => {
                            if self.status().ready {
                                return Err("reference timing missing".into());
                            }
                        }
                        Err(error) => return Err(error.to_string()),
                    }
                    status.ready &= us.ready;
                } else {
                    let decision = controllers.get_mut(&unit.id).unwrap().tick(
                        &snapshots[&s.config.reference_unit],
                        &snapshots[&unit.id],
                        self.common_now()?,
                        monotonic_ns(),
                        true,
                    );
                    us.state = decision.state;
                    us.ready = decision.ready;
                    us.fault = decision.fault.clone();
                    if let Some(measurement) = decision.measurement {
                        us.phase_ms = measurement.phase_ns / 1e6;
                        us.p95_ms = measurement.absolute_p95_ns / 1e6;
                        us.max_ms = measurement.absolute_max_ns / 1e6;
                        us.measurement_end_ns = measurement.window_end_ns;
                    }
                    if !decision.fault.is_empty() {
                        return Err(format!("{}: {}", unit.id, decision.fault));
                    }
                    status.ready &= decision.ready;
                    if let Some(mut pulse) = decision.action {
                        pulse.expires_ns = shifted(self.common_now()?, 1_000_000_000)?;
                        if unit.node_id == s.local {
                            self.adjust_local(s, &unit.id, pulse)?;
                        } else {
                            let ack = self.rpc(s, &unit.node_id, json!({"command": "adjust", "unit": unit.id, "id": pulse.correction_id, "delta": pulse.delta_us, "count": pulse.count, "expires": pulse.expires_ns}))?;
                            if ack["id"].as_u64() != Some(pulse.correction_id) {
                                return Err("correction acknowledgement differs".into());
                            }
                        }
                    }
                }
                last_metrics.insert(unit.id, us.clone());
                status.units.push(us);
            }
            if status.ready {
                status.state = "tracking".into();
            } else if monotonic_ns() > s.startup_deadline {
                return Err("group startup calibration deadline exceeded".into());
            }
            self.publish(status.clone());
            for node in s.config.nodes.clone() {
                if node.id != s.local {
                    self.rpc(s, &node.id, json!({"command": "status", "status": status}))?;
                }
            }
            next = monotonic_ns() + ns(policy.evaluation_seconds.min(1.0));
        }
        Ok(())
    }
}

fn poll_fd(fd: i32, events: i16, timeout_ms: i32) -> Result<bool> {
    let mut descriptor = libc::pollfd {
        fd,
        events,
        revents: 0,
    };
    // The descriptor remains owned by its Connection/Listener for the entire poll.
    let result = unsafe { libc::poll(&mut descriptor, 1, timeout_ms) };
    if result < 0 {
        if std::io::Error::last_os_error().kind() == std::io::ErrorKind::Interrupted {
            return Ok(false);
        }
        return Err("control poll failed".into());
    }
    if result == 0 {
        return Ok(false);
    }
    if descriptor.revents & events != 0 {
        return Ok(true);
    }
    Err("control channel disconnected".into())
}
fn listener(node: &NodeConfig) -> Result<TcpListener> {
    let ip = node
        .address
        .parse::<Ipv4Addr>()
        .map_err(|_| "numeric IPv4 address required")?;
    // std does not expose pre-bind SO_REUSEADDR. Ownership transfers immediately to TcpListener.
    let fd = unsafe { libc::socket(libc::AF_INET, libc::SOCK_STREAM, 0) };
    if fd < 0 {
        return Err("cannot create listener".into());
    }
    let listener = unsafe { TcpListener::from_raw_fd(fd) };
    if unsafe { libc::fcntl(fd, libc::F_SETFD, libc::FD_CLOEXEC) } < 0 {
        return Err("cannot set socket close-on-exec".into());
    }
    let yes: libc::c_int = 1;
    let mut address: libc::sockaddr_in = unsafe { std::mem::zeroed() };
    #[cfg(target_os = "macos")]
    {
        address.sin_len = std::mem::size_of_val(&address) as _;
    }
    address.sin_family = libc::AF_INET as _;
    address.sin_port = node.port.to_be();
    address.sin_addr.s_addr = u32::from_ne_bytes(ip.octets());
    let result = unsafe {
        libc::setsockopt(
            fd,
            libc::SOL_SOCKET,
            libc::SO_REUSEADDR,
            &yes as *const _ as *const _,
            std::mem::size_of_val(&yes) as _,
        );
        libc::bind(
            fd,
            &address as *const _ as *const _,
            std::mem::size_of_val(&address) as _,
        )
    };
    if result != 0 || unsafe { libc::listen(fd, 1) } != 0 {
        return Err(format!(
            "cannot bind control endpoint {}:{}: {}",
            node.address,
            node.port,
            std::io::Error::last_os_error()
        ));
    }
    listener.set_nonblocking(true).map_err(|e| e.to_string())?;
    Ok(listener)
}
struct Connection {
    stream: TcpStream,
    timeout: f64,
    pending: Vec<u8>,
}
impl Connection {
    fn new(stream: TcpStream, timeout: f64) -> Result<Self> {
        stream.set_nonblocking(true).map_err(|e| e.to_string())?;
        Ok(Self {
            stream,
            timeout,
            pending: Vec::new(),
        })
    }
    fn connect(node: &NodeConfig, timeout: f64) -> Result<Self> {
        let ip = node
            .address
            .parse::<Ipv4Addr>()
            .map_err(|_| "numeric IPv4 address required")?;
        let stream = TcpStream::connect_timeout(
            &SocketAddr::V4(SocketAddrV4::new(ip, node.port)),
            Duration::from_secs_f64(timeout),
        )
        .map_err(|e| format!("cannot connect to {}: {e}", node.id))?;
        Self::new(stream, timeout)
    }
    fn shutdown(&self) {
        let _ = self.stream.shutdown(Shutdown::Both);
    }
    fn readable(&self, timeout: i32) -> Result<bool> {
        if self.pending.contains(&b'\n') {
            return Ok(true);
        }
        poll_fd(self.stream.as_raw_fd(), libc::POLLIN, timeout)
    }
    fn wait(&self, events: i16, deadline: i64) -> Result<()> {
        loop {
            let remaining = deadline - monotonic_ns();
            if remaining <= 0 {
                return Err("control channel deadline exceeded".into());
            }
            if poll_fd(
                self.stream.as_raw_fd(),
                events,
                ((remaining + 999_999) / 1_000_000).min(i32::MAX as i64) as i32,
            )? {
                return Ok(());
            }
        }
    }
    fn send(&mut self, value: &Value) -> Result<()> {
        let mut data = serde_json::to_vec(value).map_err(|e| e.to_string())?;
        data.push(b'\n');
        if data.len() > MESSAGE_LIMIT {
            return Err("control message exceeds bound".into());
        }
        let deadline = monotonic_ns() + ns(self.timeout);
        let mut done = 0;
        while done < data.len() {
            self.wait(libc::POLLOUT, deadline)?;
            match self.stream.write(&data[done..]) {
                Ok(0) => return Err("control send failed".into()),
                Ok(count) => done += count,
                Err(error)
                    if matches!(
                        error.kind(),
                        std::io::ErrorKind::WouldBlock | std::io::ErrorKind::Interrupted
                    ) => {}
                Err(_) => return Err("control send failed".into()),
            }
        }
        Ok(())
    }
    fn receive(&mut self) -> Result<Value> {
        let deadline = monotonic_ns() + ns(self.timeout);
        loop {
            if let Some(pos) = self.pending.iter().position(|byte| *byte == b'\n') {
                let value =
                    serde_json::from_slice(&self.pending[..pos]).map_err(|e| e.to_string())?;
                self.pending.drain(..=pos);
                return Ok(value);
            }
            if self.pending.len() >= MESSAGE_LIMIT {
                return Err("control message exceeds bound".into());
            }
            self.wait(libc::POLLIN, deadline)?;
            let mut buffer = [0_u8; 4096];
            let limit = buffer.len().min(MESSAGE_LIMIT - self.pending.len());
            match self.stream.read(&mut buffer[..limit]) {
                Ok(0) => return Err("control channel disconnected".into()),
                Ok(count) => self.pending.extend_from_slice(&buffer[..count]),
                Err(error)
                    if matches!(
                        error.kind(),
                        std::io::ErrorKind::WouldBlock | std::io::ErrorKind::Interrupted
                    ) => {}
                Err(_) => return Err("control channel disconnected".into()),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::SyncUnitConfig;
    use crate::timing::FrameTiming;
    use std::sync::atomic::AtomicUsize;
    use std::sync::Arc;

    #[derive(Default)]
    struct Counters {
        binds: AtomicUsize,
        cancels: AtomicUsize,
        stops: AtomicUsize,
    }
    struct CaptureHost {
        counters: Arc<Counters>,
        fail_bind: bool,
        config: Mutex<Value>,
        clock_valid: bool,
    }
    impl Host for CaptureHost {
        fn call(&self, request: Value) -> Result<Value> {
            match request["op"].as_str().unwrap() {
                "bind" => {
                    if self.fail_bind {
                        return Err("injected binding failure".into());
                    }
                    self.counters.binds.fetch_add(1, Ordering::Relaxed);
                    Ok(Value::Null)
                }
                "cancel" => {
                    self.counters.cancels.fetch_add(1, Ordering::Relaxed);
                    Ok(Value::Null)
                }
                "fault" => Ok(json!("")),
                "snapshot" => {
                    let now = boot_time_ns();
                    Ok(json!(TimingSnapshot {
                        unit_id: request["unit"].as_str().unwrap().into(),
                        snapshot_timestamp_ns: now,
                        frames: vec![FrameTiming {
                            camera_sequence: 1,
                            sensor_timestamp_ns: now - 1,
                            timestamp_ns: now - 1,
                            ..FrameTiming::default()
                        }],
                        correction: CorrectionStatus::default()
                    }))
                }
                "platform_start" => {
                    *self.config.lock().unwrap() = request["config"].clone();
                    Ok(request["config"].clone())
                }
                "platform_resolve" => Ok(self.config.lock().unwrap().clone()),
                "platform_clock" => Ok(Value::Null),
                "platform_sample" => Ok(json!(ClockEstimate {
                    valid: self.clock_valid,
                    ..ClockEstimate::default()
                })),
                "platform_stop" => {
                    self.counters.stops.fetch_add(1, Ordering::Relaxed);
                    Ok(Value::Null)
                }
                _ => Err("unexpected test operation".into()),
            }
        }
    }
    fn runtime(counters: Arc<Counters>, fail_bind: bool) -> Arc<Runtime> {
        let config = SyncConfig {
            group_id: "rust-runtime".into(),
            reference_unit: "reference".into(),
            nodes: vec![NodeConfig {
                id: "local".into(),
                ..NodeConfig::default()
            }],
            units: vec![SyncUnitConfig {
                id: "reference".into(),
                node_id: "local".into(),
            }],
            ..SyncConfig::default()
        };
        Arc::new(
            Runtime::new(
                config,
                "local".into(),
                "test-identity".into(),
                vec!["reference".into()],
                Box::new(CaptureHost {
                    counters,
                    fail_bind,
                    config: Mutex::new(Value::Null),
                    clock_valid: true,
                }),
            )
            .unwrap(),
        )
    }
    #[test]
    fn restarts_release_bound_controls_and_invalidate_clock() {
        let counters = Arc::new(Counters::default());
        let runtime = runtime(counters.clone(), false);
        for round in 1..=8 {
            let binding = runtime.begin().unwrap();
            assert_eq!(binding["identity"], "test-identity");
            assert_eq!(binding["round"], round);
            let worker_runtime = runtime.clone();
            let worker = std::thread::spawn(move || {
                worker_runtime.run();
                worker_runtime.finish();
            });
            assert!(runtime.wait_ready(Duration::from_secs(1)).unwrap());
            assert!(runtime.clock().valid);
            runtime.request_stop();
            worker.join().unwrap();
            assert_eq!(runtime.status().state, "stopped");
            assert!(runtime.status().fault.is_empty());
            assert!(!runtime.clock().valid);
            assert_eq!(counters.binds.load(Ordering::Relaxed), round as usize);
            assert_eq!(counters.cancels.load(Ordering::Relaxed), round as usize);
            assert_eq!(counters.stops.load(Ordering::Relaxed), round as usize);
        }
    }
    #[test]
    fn failed_binding_does_not_cancel_unbound_capture() {
        let counters = Arc::new(Counters::default());
        let runtime = runtime(counters.clone(), true);
        runtime.begin().unwrap();
        let worker_runtime = runtime.clone();
        let worker = std::thread::spawn(move || {
            worker_runtime.run();
            worker_runtime.finish();
        });
        assert!(runtime
            .wait_ready(Duration::from_secs(1))
            .unwrap_err()
            .contains("injected binding failure"));
        runtime.request_stop();
        worker.join().unwrap();
        assert_eq!(counters.binds.load(Ordering::Relaxed), 0);
        assert_eq!(counters.cancels.load(Ordering::Relaxed), 0);
        assert_eq!(counters.stops.load(Ordering::Relaxed), 1);
    }
    fn pending_clock_group(
        follower_startup: f64,
    ) -> (Vec<Arc<Runtime>>, Vec<std::thread::JoinHandle<()>>) {
        let endpoints: Vec<_> = (0..2)
            .map(|_| TcpListener::bind("127.0.0.1:0").unwrap())
            .collect();
        let config = SyncConfig {
            group_id: "pending-clock".into(),
            reference_unit: "reference".into(),
            network_mode: "existing".into(),
            clock_mode: "host".into(),
            nodes: endpoints
                .iter()
                .enumerate()
                .map(|(i, endpoint)| NodeConfig {
                    id: format!("node-{i}"),
                    address: "127.0.0.1".into(),
                    port: endpoint.local_addr().unwrap().port(),
                    ..NodeConfig::default()
                })
                .collect(),
            units: vec![
                SyncUnitConfig {
                    id: "reference".into(),
                    node_id: "node-0".into(),
                },
                SyncUnitConfig {
                    id: "follower".into(),
                    node_id: "node-1".into(),
                },
            ],
            startup_timeout_seconds: 10.0,
            join_timeout_seconds: Some(0.5),
            request_timeout_seconds: 0.5,
            ..SyncConfig::default()
        };
        drop(endpoints);
        let runtimes: Vec<_> = config
            .units
            .iter()
            .map(|unit| {
                let mut local_config = config.clone();
                if unit.node_id == "node-1" {
                    local_config.startup_timeout_seconds = follower_startup;
                }
                Arc::new(
                    Runtime::new(
                        local_config,
                        unit.node_id.clone(),
                        unit.node_id.clone(),
                        vec![unit.id.clone()],
                        Box::new(CaptureHost {
                            counters: Arc::new(Counters::default()),
                            fail_bind: false,
                            config: Mutex::new(Value::Null),
                            clock_valid: false,
                        }),
                    )
                    .unwrap(),
                )
            })
            .collect();
        for runtime in &runtimes {
            runtime.begin().unwrap();
        }
        let workers: Vec<_> = runtimes
            .iter()
            .map(|runtime| {
                let runtime = runtime.clone();
                std::thread::spawn(move || {
                    runtime.run();
                    runtime.finish();
                })
            })
            .collect();
        let deadline = monotonic_ns() + ns(3.0);
        while runtimes.iter().any(|r| r.status().state != "clock") && monotonic_ns() < deadline {
            std::thread::sleep(Duration::from_millis(10));
        }
        (runtimes, workers)
    }
    #[test]
    fn follower_stop_during_clock_preparation_ends_group() {
        let (runtimes, workers) = pending_clock_group(10.0);
        // Joining has completed; its shorter budget must not limit clock convergence.
        std::thread::sleep(Duration::from_millis(600));
        let clock_pending = runtimes.iter().all(|r| {
            r.status().state == "clock" && !r.clock().valid && r.status().fault.is_empty()
        });
        runtimes[1].request_stop();
        for worker in workers {
            worker.join().unwrap();
        }
        assert!(
            clock_pending,
            "both nodes must expose pending clock preparation"
        );
        for runtime in &runtimes {
            let status = runtime.status();
            assert_eq!(status.state, "stopped");
            assert!(status.fault.is_empty(), "{}", status.fault);
            assert_eq!(status.stop_node, "node-1");
            assert_eq!(status.stop_reason, "requested");
        }
    }
    #[test]
    fn follower_enforces_its_clock_startup_budget() {
        let (runtimes, workers) = pending_clock_group(0.25);
        assert!(runtimes[1]
            .wait_ready(Duration::from_secs(2))
            .unwrap_err()
            .contains("startup"));
        for worker in workers {
            worker.join().unwrap();
        }
        assert!(!runtimes[0].status().fault.is_empty());
        assert_eq!(runtimes[1].status().stop_reason, "fault");
        assert_eq!(runtimes[1].status().stop_node, "node-1");
    }
    #[test]
    fn stop_does_not_skip_hello_on_an_open_connection() {
        let endpoint = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = endpoint.local_addr().unwrap();
        let peer = std::thread::spawn(move || {
            let (stream, _) = endpoint.accept().unwrap();
            let mut connection = Connection::new(stream, 1.0).unwrap();
            let hello = connection.receive().unwrap();
            assert_eq!(hello["command"], "hello");
            connection
                .send(&json!({"request_id": hello["request_id"], "ok": true,
                "result": {"node": "peer"}}))
                .unwrap();
        });
        let runtime = runtime(Arc::new(Counters::default()), false);
        runtime.begin().unwrap();
        let stream = TcpStream::connect(address).unwrap();
        let mut state = runtime.run.lock().unwrap();
        state
            .peers
            .insert("peer".into(), Connection::new(stream, 1.0).unwrap());
        runtime.request_stop();
        let result = runtime
            .rpc(&mut state, "peer", json!({"command": "hello"}))
            .unwrap();
        assert_eq!(result["node"], "peer");
        peer.join().unwrap();
    }
    #[test]
    fn control_channel_rejects_unbounded_input() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let sender = std::thread::spawn(move || {
            let mut stream = TcpStream::connect(address).unwrap();
            stream.write_all(&vec![b'x'; MESSAGE_LIMIT]).unwrap();
        });
        let (stream, _) = listener.accept().unwrap();
        let mut channel = Connection::new(stream, 2.0).unwrap();
        assert_eq!(
            channel.receive().unwrap_err(),
            "control message exceeds bound"
        );
        sender.join().unwrap();
    }
}
