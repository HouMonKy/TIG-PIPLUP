//! Shared input planning utilities.
//!
//! This module contains pure functions for directory scanning and leaf-group
//! construction that are used by both the CLI and the async API. It has no
//! dependency on `clap`, `anyhow`, or any async runtime.

use std::{
    collections::BTreeMap,
    ffi::OsStr,
    fs, io,
    path::{Path, PathBuf},
};

/// Recursively collect all `.log` files under `root`.
///
/// Symlinks are skipped. Files are filtered by `.log` extension
/// (case-insensitive). The returned files are **not** sorted; callers must
/// sort if deterministic ordering is required.
///
/// # Errors
///
/// Returns an I/O error if directory traversal fails.
pub fn collect_log_files(root: &Path) -> io::Result<Vec<PathBuf>> {
    let mut files = Vec::new();
    collect_log_files_recursive(root, &mut files)?;
    Ok(files)
}

/// Recursive helper for [`collect_log_files`].
fn collect_log_files_recursive(dir: &Path, files: &mut Vec<PathBuf>) -> io::Result<()> {
    for entry in fs::read_dir(dir)? {
        let entry = entry?;
        let file_type = entry.file_type()?;
        let path = entry.path();

        if file_type.is_symlink() {
            continue;
        }

        if file_type.is_dir() {
            collect_log_files_recursive(&path, files)?;
        } else if file_type.is_file() && has_log_extension(&path) {
            files.push(path);
        }
    }

    Ok(())
}

/// Check whether `path` has a `.log` extension (case-insensitive).
pub fn has_log_extension(path: &Path) -> bool {
    path.extension()
        .and_then(OsStr::to_str)
        .map(|ext| ext.eq_ignore_ascii_case("log"))
        .unwrap_or(false)
}

/// Group sorted files by their parent (leaf) directory.
///
/// Returns a `BTreeMap` keyed by leaf directory path, so iteration order is
/// deterministic (sorted by leaf path). Each value is the list of files in
/// that leaf directory, preserving the input order.
pub fn group_files_by_leaf(files: &[PathBuf]) -> BTreeMap<PathBuf, Vec<PathBuf>> {
    let mut grouped = BTreeMap::<PathBuf, Vec<PathBuf>>::new();
    for file in files {
        let leaf_dir = file
            .parent()
            .map(Path::to_path_buf)
            .unwrap_or_else(|| PathBuf::from("."));
        grouped.entry(leaf_dir).or_default().push(file.clone());
    }
    grouped
}
