use serde::{Deserialize, Serialize};
use crate::drain::default_drain::LabelSource;

#[derive(Debug, Serialize, Deserialize)]
pub struct DrainTemplate {
    id: u64,
    content: Vec<String>,
    pub normal: bool,
    pub label_source: LabelSource
}

impl DrainTemplate {
    pub fn new(id: u64, content: Vec<String>, normal: bool, label_source: LabelSource) -> DrainTemplate {
        DrainTemplate {
            id,
            content,
            normal,
            label_source
        }
    }

    pub fn id(&self) -> u64 {
        self.id
    }

    pub fn content(&self) -> &Vec<String> {
        &self.content
    }

    pub fn len(&self) -> u64 {
        self.content.len() as u64
    }

    pub fn set_content(&mut self, content: Vec<String>) {
        self.content = content;
    }

    pub fn set_id(&mut self, id: u64) {
        self.id = id;
    }
}

impl Clone for DrainTemplate {
    fn clone(&self) -> DrainTemplate {
        DrainTemplate::new(self.id, self.content.clone(), self.normal, self.label_source.clone())
    }
}