use crate::phase::PhasePolicy;
use serde::{Deserialize, Deserializer, Serialize, Serializer};
use serde_json::{json, Value};
use std::collections::BTreeSet;
use std::net::Ipv4Addr;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct NodeConfig {
    pub id: String,
    pub address: String,
    pub interface: String,
    pub port: u16,
}
impl Default for NodeConfig {
    fn default() -> Self {
        Self {
            id: String::new(),
            address: String::new(),
            interface: String::new(),
            port: 58080,
        }
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct SyncUnitConfig {
    pub id: String,
    pub node_id: String,
}

#[derive(Clone, Debug)]
pub struct SyncConfig {
    pub group_id: String,
    pub reference_unit: String,
    pub fps: u32,
    pub network_mode: String,
    pub clock_mode: String,
    pub clock_port: u16,
    pub nodes: Vec<NodeConfig>,
    pub units: Vec<SyncUnitConfig>,
    pub phase_policy: PhasePolicy,
    pub clock_max_offset_ms: f64,
    pub clock_max_age_seconds: f64,
    pub request_timeout_seconds: f64,
    pub startup_timeout_seconds: f64,
}
impl Default for SyncConfig {
    fn default() -> Self {
        Self {
            group_id: String::new(),
            reference_unit: String::new(),
            fps: 40,
            network_mode: "auto".into(),
            clock_mode: "network".into(),
            clock_port: 58081,
            nodes: Vec::new(),
            units: Vec::new(),
            phase_policy: PhasePolicy::default(),
            clock_max_offset_ms: 0.1,
            clock_max_age_seconds: 10.0,
            request_timeout_seconds: 2.0,
            startup_timeout_seconds: 60.0,
        }
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
struct NetworkConfig {
    mode: String,
    clock_port: u16,
}
impl Default for NetworkConfig {
    fn default() -> Self {
        Self {
            mode: "auto".into(),
            clock_port: 58081,
        }
    }
}
#[derive(Serialize, Deserialize)]
#[serde(default)]
struct ConfigDocument {
    group_id: String,
    reference_unit: String,
    fps: u32,
    clock_mode: String,
    network: NetworkConfig,
    nodes: Vec<NodeConfig>,
    units: Vec<SyncUnitConfig>,
    phase_policy: PhasePolicy,
    clock_max_offset_ms: f64,
    clock_max_age_seconds: f64,
    request_timeout_seconds: f64,
    startup_timeout_seconds: f64,
}
impl Default for ConfigDocument {
    fn default() -> Self {
        SyncConfig::default().document()
    }
}
impl SyncConfig {
    fn document(&self) -> ConfigDocument {
        ConfigDocument {
            group_id: self.group_id.clone(),
            reference_unit: self.reference_unit.clone(),
            fps: self.fps,
            clock_mode: self.clock_mode.clone(),
            network: NetworkConfig {
                mode: self.network_mode.clone(),
                clock_port: self.clock_port,
            },
            nodes: self.nodes.clone(),
            units: self.units.clone(),
            phase_policy: self.phase_policy.clone(),
            clock_max_offset_ms: self.clock_max_offset_ms,
            clock_max_age_seconds: self.clock_max_age_seconds,
            request_timeout_seconds: self.request_timeout_seconds,
            startup_timeout_seconds: self.startup_timeout_seconds,
        }
    }
    pub fn node(&self, id: &str) -> Result<&NodeConfig, String> {
        self.nodes
            .iter()
            .find(|n| n.id == id)
            .ok_or_else(|| format!("unknown node: {id}"))
    }
    pub fn unit(&self, id: &str) -> Result<&SyncUnitConfig, String> {
        self.units
            .iter()
            .find(|u| u.id == id)
            .ok_or_else(|| format!("unknown unit: {id}"))
    }
    pub fn reference_node(&self) -> Result<&str, String> {
        Ok(&self.unit(&self.reference_unit)?.node_id)
    }
    pub fn validate(&self) -> Result<(), String> {
        self.phase_policy
            .validate()
            .map_err(|error| error.to_string())?;
        let valid_id = |id: &str| !id.is_empty() && !id.contains('\0');
        if !valid_id(&self.group_id) || self.nodes.is_empty() || self.units.is_empty() {
            return Err("invalid group identity or empty topology".into());
        }
        if !matches!(self.network_mode.as_str(), "auto" | "existing") {
            return Err("network mode must be auto or existing".into());
        }
        if !matches!(self.clock_mode.as_str(), "network" | "host") {
            return Err("clock mode must be network or host".into());
        }
        if self.clock_port == 0 || self.fps == 0 || self.fps > 1_000_000 {
            return Err("invalid clock port or unrepresentable frame period".into());
        }
        let addressed =
            self.nodes.len() > 1 && (self.network_mode == "existing" || self.clock_mode == "host");
        let mut ids = BTreeSet::new();
        let mut addresses = BTreeSet::new();
        for node in &self.nodes {
            if !valid_id(&node.id)
                || !ids.insert(&node.id)
                || node.port == 0
                || node.interface.contains('\0')
                || (self.clock_mode == "network"
                    && self.nodes.len() > 1
                    && self.network_mode == "auto"
                    && node.interface.is_empty())
                || (addressed
                    && (node.address.parse::<Ipv4Addr>().is_err()
                        || !addresses.insert((&node.address, node.port))))
            {
                return Err("invalid or duplicate node endpoint".into());
            }
        }
        self.unit(&self.reference_unit)?;
        let mut units = BTreeSet::new();
        for unit in &self.units {
            self.node(&unit.node_id)?;
            if !valid_id(&unit.id) || !units.insert(&unit.id) {
                return Err("invalid or duplicate synchronization unit".into());
            }
        }
        for node in &self.nodes {
            if !self.units.iter().any(|u| u.node_id == node.id) {
                return Err(format!("node has no units: {}", node.id));
            }
        }
        if !self.clock_max_offset_ms.is_finite()
            || self.clock_max_offset_ms <= 0.0
            || self.clock_max_offset_ms * 1e6 >= i64::MAX as f64
        {
            return Err("invalid clock offset threshold".into());
        }
        let available = (i64::MAX - monotonic_ns()) as f64;
        for value in [
            self.clock_max_age_seconds,
            self.request_timeout_seconds,
            self.startup_timeout_seconds,
        ] {
            if !value.is_finite() || value <= 0.0 || value * 1e9 >= available {
                return Err("timeout is invalid or exceeds timestamp range".into());
            }
        }
        if (2.0 + self.request_timeout_seconds * (self.nodes.len() + 1) as f64) * 1e9 >= available {
            return Err("group request budget exceeds timestamp range".into());
        }
        Ok(())
    }
    pub fn topology(&self) -> Value {
        let mut nodes: Vec<&str> = self.nodes.iter().map(|n| n.id.as_str()).collect();
        nodes.sort_unstable();
        let mut units: Vec<&SyncUnitConfig> = self.units.iter().collect();
        units.sort_by(|a, b| a.id.cmp(&b.id));
        json!({"group_id":self.group_id,"reference_unit":self.reference_unit,"fps":self.fps,
            "clock_mode":self.clock_mode,"clock_domain":if self.nodes.len() == 1 {"local"} else {"common"},
            "nodes":nodes,"units":units,"phase_policy":self.phase_policy})
    }
    pub fn sync_json(&self) -> String {
        self.topology().to_string()
    }
}
impl Serialize for SyncConfig {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        self.document().serialize(serializer)
    }
}
impl<'de> Deserialize<'de> for SyncConfig {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let value = Value::deserialize(deserializer)?;
        let malformed = !value.is_object()
            || value.get("phase_policy").is_some_and(|p| !p.is_object())
            || value.get("network").is_some_and(|p| !p.is_object())
            || ["nodes", "units"].iter().any(|field| {
                value.get(field).is_some_and(|items| {
                    items
                        .as_array()
                        .is_none_or(|items| items.iter().any(|item| !item.is_object()))
                })
            });
        if malformed {
            return Err(serde::de::Error::custom(
                "invalid synchronization configuration shape",
            ));
        }
        let d: ConfigDocument = serde_json::from_value(value).map_err(serde::de::Error::custom)?;
        Ok(Self {
            group_id: d.group_id,
            reference_unit: d.reference_unit,
            fps: d.fps,
            network_mode: d.network.mode,
            clock_port: d.network.clock_port,
            clock_mode: d.clock_mode,
            nodes: d.nodes,
            units: d.units,
            phase_policy: d.phase_policy,
            clock_max_offset_ms: d.clock_max_offset_ms,
            clock_max_age_seconds: d.clock_max_age_seconds,
            request_timeout_seconds: d.request_timeout_seconds,
            startup_timeout_seconds: d.startup_timeout_seconds,
        })
    }
}
// libc's time_t and c_long widths differ across supported Unix targets.
#[allow(clippy::unnecessary_cast)]
fn clock_ns(clock: libc::clockid_t) -> i64 {
    let mut value = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    // The pointer is valid and exclusively owned for this synchronous libc call.
    assert_eq!(
        unsafe { libc::clock_gettime(clock, &mut value) },
        0,
        "clock_gettime failed"
    );
    (value.tv_sec as i64)
        .checked_mul(1_000_000_000)
        .and_then(|v| v.checked_add(value.tv_nsec as i64))
        .expect("clock exceeds timestamp range")
}
pub fn monotonic_ns() -> i64 {
    clock_ns(libc::CLOCK_MONOTONIC)
}
pub fn realtime_ns() -> i64 {
    clock_ns(libc::CLOCK_REALTIME)
}
pub fn boot_time_ns() -> i64 {
    #[cfg(target_os = "linux")]
    {
        clock_ns(libc::CLOCK_BOOTTIME)
    }
    #[cfg(not(target_os = "linux"))]
    {
        monotonic_ns()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn local() -> SyncConfig {
        serde_json::from_value(
            json!({"group_id":"local", "reference_unit":"reference", "nodes":[{"id":"local"}],
            "units":[{"id":"reference","node_id":"local"}]}),
        )
        .unwrap()
    }
    #[test]
    fn configuration_roundtrip_and_application_fields() {
        let config = local();
        config.validate().unwrap();
        let mut document = serde_json::to_value(&config).unwrap();
        document["units"][0]["camera_id"] = json!("application-owned");
        let restored: SyncConfig = serde_json::from_value(document).unwrap();
        assert_eq!(restored.sync_json(), config.sync_json());
        assert_eq!(restored.reference_node().unwrap(), "local");
    }
    #[test]
    fn malformed_topology_and_time_budget_are_rejected() {
        let mut config = local();
        config.fps = 0;
        assert!(config.validate().is_err());
        config = local();
        config.request_timeout_seconds = 6e9;
        assert!(config.validate().is_err());
        config = local();
        config.units[0].node_id = "missing".into();
        assert!(config.validate().is_err());
    }
    #[test]
    fn configuration_fields_require_objects_and_arrays() {
        let value = serde_json::to_value(local()).unwrap();
        for (field, invalid) in [
            ("phase_policy", json!([])),
            ("network", json!([])),
            ("nodes", json!({})),
            ("units", json!([["reference", "local"]])),
        ] {
            let mut malformed = value.clone();
            malformed[field] = invalid;
            assert!(serde_json::from_value::<SyncConfig>(malformed).is_err());
        }
    }
}
