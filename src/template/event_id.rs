use md5::{Digest, Md5};

pub fn event_id_for_template(template: &str) -> String {
    let mut hasher = Md5::new();
    hasher.update(template.as_bytes());
    let digest = hasher.finalize();
    format!("{digest:x}")[0..8].to_string()
}

#[cfg(test)]
mod tests {
    use super::event_id_for_template;

    #[test]
    fn event_id_matches_md5_prefix_contract() {
        assert_eq!(event_id_for_template("INFO worker <*>"), "33be87e4");
        assert_eq!(event_id_for_template("user=<*> status=<*>"), "a9febcc1");
    }
}
