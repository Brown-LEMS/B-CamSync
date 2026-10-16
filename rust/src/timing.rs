use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct FrameTiming {
    #[serde(rename = "sequence", alias = "camera_sequence")]
    pub camera_sequence: u64,
    #[serde(rename = "sensor", alias = "sensor_timestamp_ns")]
    pub sensor_timestamp_ns: i64,
    #[serde(rename = "time", alias = "timestamp_ns")]
    pub timestamp_ns: i64,
    #[serde(rename = "span", alias = "mapping_span_ns")]
    pub mapping_span_ns: i64,
    #[serde(rename = "requested", alias = "requested_duration_us")]
    pub requested_duration_us: i64,
    #[serde(rename = "reported", alias = "reported_duration_us")]
    pub reported_duration_us: i64,
    pub correction_id: u64,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct CorrectionStatus {
    pub state: String,
    #[serde(rename = "id", alias = "correction_id")]
    pub correction_id: u64,
    #[serde(rename = "restored", alias = "restored_timestamp_ns")]
    pub restored_timestamp_ns: i64,
}
impl Default for CorrectionStatus {
    fn default() -> Self {
        Self {
            state: "idle".into(),
            correction_id: 0,
            restored_timestamp_ns: 0,
        }
    }
}
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct TimingSnapshot {
    #[serde(rename = "unit", alias = "unit_id")]
    pub unit_id: String,
    #[serde(rename = "boundary", alias = "snapshot_timestamp_ns")]
    pub snapshot_timestamp_ns: i64,
    pub frames: Vec<FrameTiming>,
    pub correction: CorrectionStatus,
}
#[derive(Clone, Copy, Debug, Default, Serialize, Deserialize)]
pub struct Pulse {
    pub correction_id: u64,
    pub delta_us: i32,
    pub count: i32,
    pub expires_ns: i64,
}
