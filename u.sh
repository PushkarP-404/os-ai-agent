#!/bin/sh
set -e

echo "Downloading agent daemon files..."
cd /usr/local/lib/ai-agent/
wget -q -O agent_daemon.py http://10.0.2.2:8000/agent-daemon/agent_daemon.py
wget -q -O logger.py http://10.0.2.2:8000/agent-daemon/logger.py

echo "Downloading cdp_controller..."
wget -q -O /home/aiuser/cdp_controller.py http://10.0.2.2:8000/agent-daemon/cdp_controller.py

echo "Downloading dashboard..."
mkdir -p /root/os-ai-agent/dashboard
cd /root/os-ai-agent/dashboard
wget -q -O dashboard.py http://10.0.2.2:8000/dashboard/dashboard.py
wget -q -O ai-agent-dashboard.desktop http://10.0.2.2:8000/dashboard/ai-agent-dashboard.desktop

echo "Downloading and compiling agent-cli..."
mkdir -p /root/os-ai-agent/agent-cli
cd /root/os-ai-agent/agent-cli
wget -q -O agent-cli.c http://10.0.2.2:8000/agent-cli/agent-cli.c
wget -q -O Makefile http://10.0.2.2:8000/agent-cli/Makefile
make -s

echo "Fixing permissions for aiuser..."
addgroup aiuser wheel || true

echo "Enabling SSH root password logins..."
sed -i 's/^#PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config
sed -i 's/^PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config
/etc/init.d/sshd restart || true

echo "Restarting agent service..."
/etc/init.d/ai-agent restart

echo "SUCCESS! All updates applied."
