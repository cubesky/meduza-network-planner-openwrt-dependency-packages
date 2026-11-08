// Architecture constants from meduza-network-planner-v2@7b3a4a9ecc551e537c4325ed08a4f00409bc5a4a.
pub const ARCHES: &[(&str, &[&str])] = &[
    ("x86_64-unknown-linux-musl", &["x86_64"]),
    ("aarch64-unknown-linux-musl", &["aarch64_generic", "aarch64_cortex-a53", "aarch64_cortex-a72", "aarch64_cortex-a76"]),
    ("armv7-unknown-linux-musleabihf", &["arm_cortex-a7_neon-vfpv4", "arm_cortex-a9_vfpv3-d16", "arm_cortex-a15_neon-vfpv4"]),
];
pub const TARGET_PLATFORMS: &[(&str, &str)] = &[
    ("x86_64-unknown-linux-musl", "linux/amd64"),
    ("aarch64-unknown-linux-musl", "linux/arm64"),
    ("armv7-unknown-linux-musleabihf", "linux/arm/v7"),
];
pub const FORMATS: &[&str] = &["ipk", "apk"];
