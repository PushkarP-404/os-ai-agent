// ============================================================
// telemetry.rs — /proc-based CPU, RAM, Disk stats
// Works on Linux (VM target); stubs gracefully on Windows
// ============================================================
use anyhow::Result;

#[derive(Debug, Clone, Default)]
pub struct Telemetry {
    pub cpu_usage: f32,   // 0.0 – 1.0
    pub ram_usage: f32,
    pub disk_usage: f32,
}

#[cfg(target_os = "linux")]
mod linux {
    use super::*;
    use std::fs;

    // CPU — diff two /proc/stat samples
    #[derive(Default)]
    struct CpuStat {
        user: u64, nice: u64, system: u64, idle: u64,
        iowait: u64, irq: u64, softirq: u64,
    }

    impl CpuStat {
        fn read() -> Result<Self> {
            let raw = fs::read_to_string("/proc/stat")?;
            let line = raw.lines().next().unwrap_or("");
            let nums: Vec<u64> = line
                .split_whitespace()
                .skip(1)
                .filter_map(|s| s.parse().ok())
                .collect();
            Ok(Self {
                user: nums.get(0).copied().unwrap_or(0),
                nice: nums.get(1).copied().unwrap_or(0),
                system: nums.get(2).copied().unwrap_or(0),
                idle: nums.get(3).copied().unwrap_or(0),
                iowait: nums.get(4).copied().unwrap_or(0),
                irq: nums.get(5).copied().unwrap_or(0),
                softirq: nums.get(6).copied().unwrap_or(0),
            })
        }

        fn total(&self) -> u64 {
            self.user + self.nice + self.system + self.idle
                + self.iowait + self.irq + self.softirq
        }

        fn idle_total(&self) -> u64 { self.idle + self.iowait }
    }

    pub async fn read() -> Result<Telemetry> {
        // CPU — two samples 200ms apart
        let s1 = CpuStat::read()?;
        tokio::time::sleep(std::time::Duration::from_millis(200)).await;
        let s2 = CpuStat::read()?;

        let d_total = s2.total().saturating_sub(s1.total());
        let d_idle  = s2.idle_total().saturating_sub(s1.idle_total());
        let cpu = if d_total == 0 { 0.0 }
            else { 1.0 - (d_idle as f32 / d_total as f32) };

        // RAM
        let meminfo = fs::read_to_string("/proc/meminfo")?;
        let mem_total = parse_meminfo(&meminfo, "MemTotal");
        let mem_avail = parse_meminfo(&meminfo, "MemAvailable");
        let ram = if mem_total == 0 { 0.0 }
            else { 1.0 - (mem_avail as f32 / mem_total as f32) };

        // Disk — statvfs on "/"
        let disk = disk_usage("/")?;

        Ok(Telemetry {
            cpu_usage:  cpu.clamp(0.0, 1.0),
            ram_usage:  ram.clamp(0.0, 1.0),
            disk_usage: disk.clamp(0.0, 1.0),
        })
    }

    fn parse_meminfo(raw: &str, key: &str) -> u64 {
        raw.lines()
            .find(|l| l.starts_with(key))
            .and_then(|l| l.split_whitespace().nth(1))
            .and_then(|v| v.parse().ok())
            .unwrap_or(0)
    }

    fn disk_usage(path: &str) -> Result<f32> {
        use std::ffi::CString;
        let cpath = CString::new(path)?;
        let mut st: libc::statvfs = unsafe { std::mem::zeroed() };
        let rc = unsafe { libc::statvfs(cpath.as_ptr(), &mut st) };
        if rc != 0 { return Ok(0.0); }
        let total = st.f_blocks * st.f_frsize;
        let free  = st.f_bfree  * st.f_frsize;
        if total == 0 { return Ok(0.0); }
        Ok(1.0 - (free as f32 / total as f32))
    }
}

#[cfg(not(target_os = "linux"))]
mod linux {
    use super::*;
    pub async fn read() -> Result<Telemetry> {
        // Windows host: return mock values during development
        Ok(Telemetry {
            cpu_usage:  0.25,
            ram_usage:  0.45,
            disk_usage: 0.60,
        })
    }
}

pub async fn read() -> Result<Telemetry> {
    linux::read().await
}

