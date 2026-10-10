#[cfg(feature = "cli")]
fn main() -> anyhow::Result<()> {
    log_parser::cli::run()
}

#[cfg(not(feature = "cli"))]
fn main() {
    eprintln!("The CLI binary requires the `cli` feature.");
    std::process::exit(1);
}
