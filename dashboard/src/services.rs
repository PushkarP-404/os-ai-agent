// ============================================================
// services.rs — Query OpenRC service status (Linux)
// ============================================================
use anyhow::Result;

#[derive(Debug, Clone, PartialEq)]
#[allow(dead_code)]
pub enum ServiceStatus {
    Running,
    Stopped,
    Checking,
}

impl ServiceStatus {
    /// Convert to the Slint-side integer (matches ServiceStatus enum order)
    #[allow(dead_code)]
    pub fn to_slint_int(&self) -> i32 {
        match self {
            Self::Running  => 0,
            Self::Stopped  => 1,
            Self::Checking => 2,
        }
    }
}

#[cfg(target_os = "linux")]
pub async fn check(service: &str) -> ServiceStatus {
    use tokio::process::Command;
    let result = Command::new("rc-service")
        .args([service, "status"])
        .output()
        .await;

    match result {
        Ok(out) => {
            let stdout = String::from_utf8_lossy(&out.stdout);
            if stdout.contains("started") || stdout.contains("running") {
                ServiceStatus::Running
            } else {
                ServiceStatus::Stopped
            }
        }
        Err(_) => ServiceStatus::Stopped,
    }
}

#[cfg(not(target_os = "linux"))]
pub async fn check(_service: &str) -> ServiceStatus {
    // On Windows host, mock as running for UI development
    ServiceStatus::Running
}

pub async fn check_all() -> Result<(ServiceStatus, ServiceStatus, ServiceStatus)> {
    let (ai, llama, lp) = tokio::join!(
        check("ai-agent"),
        check("llama-server"),
        check("lightpanda"),
    );
    Ok((ai, llama, lp))
}

