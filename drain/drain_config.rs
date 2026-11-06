/*
 * Copyright (c) Huawei Technologies Co., Ltd. 2025-2025. All rights reserved.
 */
use std::collections::HashMap;
use std::fs::File;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use DrainErr::NoDrainConfigErr;
use crate::exception::drain_err::DrainErr;

#[derive(Debug, Serialize, Deserialize)]
pub struct DrainConfig {
    pub depth: usize,
    pub max_children: usize,
    pub sim_th: f64,
    pub max_capacity: usize,
    pub wildcard: String,
    pub max_wildcard_rate: f64
}

// drainConfig需要多线程并发共享读取
unsafe impl Sync for DrainConfig {}

impl DrainConfig {
    pub fn load_drain_config(path: &str) -> Result<HashMap<String, DrainConfig>, DrainErr> {
        let file = match File::open(path) {
            Ok(file) => file,
            Err(err) => return Err(NoDrainConfigErr(err.to_string())),
        };
        let mut config_map = HashMap::new();
        if let Ok(Value::Object(value)) = serde_json::from_reader(file) {
            for (key, value) in value {
                if let Ok(config) = serde_json::from_value::<DrainConfig>(value) {
                    config_map.insert(key, config);
                }
            }
        };
        Ok(config_map)
    }

    pub fn default() -> Self {
        DrainConfig {
            depth: 4,
            max_children: 200,
            sim_th: 0.8,
            max_capacity: 50000,
            wildcard: "<*>".to_string(),
            max_wildcard_rate: 0.5
        }
    }
}
