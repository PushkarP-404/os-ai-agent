## Package management: -- done

##### Can start by creating APK package for llama-server, creating APK for agent-daemon and kernel modules, publish to a custom APK repository.

## 

## Distro Installer:

##### Simple installer (Alpine's setup-alpine as template)

##### Ask 3-4 questions - Disk selection, Hostname/password, Enable AI agent ? - copy the kernel, rootfs and services to disk, generate a bootable ISO from the installed system.





## Configuration Management:

##### Viable version, create /etc/ai-agent/agent.conf with model info, daemon, policy. Make agent\_daemon.py read this config instead of hardcoding values.

##### Add ai-agent.config command line tool to edit safely.

##### Support --dry-run to validate config changes. This aims to let users customize without rebuilding, unblocks testing different models, timeouts, policies.



## Modular Kernel:

##### Move AI subsystem into a loadable kernel module (not baked into kernel image) for user customisation.

##### Keep syscall #548 in the kernel (since its hard requirement for IPC).Make ai\_process\_hook.ko optional, can be loaded/unloaded at runtime.Document which config are safe to toggle (DEBUG\_INFO, MODULE\_SIG, etc).



## Multi-user \& Security:

##### Run agent\_daemon as unprivileged user. Add groups: wheel(admin), aiagent-users (can query agent), aiagent-admin(can manage agent). Add policy file /etc/ai-agent/policy.conf with the permissions. Add ACL checking in daemon. Document sudo/privilege separation.



## Engineering \& CI/CD:

##### Setup github actions to build kernel on commit, run test harnesses (test\_phase6\_boot.sh), generate ISO monthly, check security vulnerabilities.

