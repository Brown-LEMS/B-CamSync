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
}
impl Default for GroupStatus {
    fn default() -> Self {
        Self {
            state: "stopped".into(),
            ready: false,
            fault: String::new(),
            units: Vec::new(),
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
    coordinator: bool,
}
pub struct Runtime {
    host: Box<dyn Host>,
    run: Mutex<RunState>,
    published: Mutex<Published>,
    changed: Condvar,
    stopping: AtomicBool,
    common: bool,
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
                local,
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
        self.published.lock().unwrap().group = group;
        self.changed.notify_all();
    }
    pub fn status(&self) -> GroupStatus {
        self.published.lock().unwrap().group.clone()
    }
    pub fn fail(&self, reason: &str) {
        let mut p = self.published.lock().unwrap();
        p.group.state = "fault".into();
        p.group.ready = false;
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
    fn health(&self, s: &RunState) -> Result<()> {
        for unit in &s.local_units {
            let fault: String = decode(self.host.call(json!({"op": "fault", "unit": unit}))?)?;
            if !fault.is_empty() {
                return Err(format!("{unit}: {fault}"));
            }
        }
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
            if !self.stopping() {
                self.fail(&error);
            }
        }
        if !self.stopping() {
            if let Some(channel) = &s.follower {
                channel.shutdown();
            }
            self.cancel_units(&mut s);
            for peer in s.peers.values() {
                peer.shutdown();
            }
            s.peers.clear();
            while !self.stopping() {
                let _ = self.update_clock();
                self.pause(Duration::from_secs(1));
            }
        }
        if s.coordinator {
            let peers: Vec<_> = s.peers.keys().cloned().collect();
            for node in peers {
                match self.rpc(&mut s, &node, json!({"command": "stop"})) {
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
        }
        self.cancel_units(&mut s);
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
        if !self.stopping() {
            self.update_clock()?;
        }
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
        self.publish(status);
    }
    fn rpc(&self, s: &mut RunState, node: &str, mut message: Value) -> Result<Value> {
        let id = s.requests.entry(node.into()).or_default();
        *id = id
            .checked_add(1)
            .ok_or("control request identity exhausted")?;
        let request_id = *id;
        message["request_id"] = json!(request_id);
        message["session"] = json!(s.session);
        let channel = s.peers.get_mut(node).ok_or("missing control peer")?;
        channel.send(&message)?;
        let response = channel.receive()?;
        if response["request_id"].as_u64() != Some(request_id) {
            return Err("out-of-order control response".into());
        }
        if response["ok"].as_bool() != Some(true) {
            return Err(response["error"]
                .as_str()
                .unwrap_or("invalid control response")
                .into());
        }
        Ok(response["result"].clone())
    }
    fn run_follower(&self, s: &mut RunState) -> Result<()> {
        s.server = Some(listener(s.resolved.node(&s.local)?)?);
        let mut next_health = monotonic_ns();
        let client = loop {
            if self.stopping() {
                return Ok(());
            }
            if monotonic_ns() >= next_health {
                self.health(s)?;
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
            if monotonic_ns() > s.startup_deadline {
                return Err("coordinator connection startup timeout".into());
            }
        };
        s.follower = Some(Connection::new(client, s.config.request_timeout_seconds)?);
        let mut expected = 1_u64;
        let mut last_command = monotonic_ns();
        let idle_limit =
            ns(2.0 + s.config.request_timeout_seconds * (s.config.nodes.len() as f64 + 1.0));
        while !self.stopping() {
            if monotonic_ns() >= next_health {
                self.health(s)?;
                next_health = monotonic_ns() + 1_000_000_000;
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
                match command {
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
                        self.request_stop();
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
                Ok(result) => json!({"request_id": id, "ok": true, "result": result}),
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
        Ok(())
    }
    fn run_coordinator(&self, s: &mut RunState) -> Result<()> {
        for node in s.resolved.nodes.clone() {
            if node.id == s.local {
                continue;
            }
            while !self.stopping() {
                match Connection::connect(&node, s.config.request_timeout_seconds) {
                    Ok(connection) => {
                        s.peers.insert(node.id.clone(), connection);
                        break;
                    }
                    Err(_) => {
                        if monotonic_ns() > s.startup_deadline {
                            return Err(format!("node connection startup timeout: {}", node.id));
                        }
                        eprintln!("Waiting for node {}", node.id);
                        self.pause(Duration::from_millis(250));
                    }
                }
            }
            if self.stopping() {
                return Ok(());
            }
            let remaining = (s.startup_deadline - monotonic_ns()) as f64 / 1e9;
            if remaining <= 0.0 {
                return Err(format!("node initialization startup timeout: {}", node.id));
            }
            s.peers.get_mut(&node.id).unwrap().timeout =
                remaining.min(s.config.request_timeout_seconds);
            let hello = self.rpc(
                s,
                &node.id,
                json!({"command": "hello", "topology": s.config.topology()}),
            )?;
            s.peers.get_mut(&node.id).unwrap().timeout = s.config.request_timeout_seconds;
            if hello["node"] != node.id {
                return Err("peer node identity differs".into());
            }
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
                        unit_id: "reference".into(),
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
                "platform_start" => Ok(request["config"].clone()),
                "platform_sample" => Ok(json!(ClockEstimate {
                    valid: true,
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
