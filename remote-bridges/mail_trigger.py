#!/usr/bin/env python3
"""
Mail-to-Intent Bridge for AI-Agent OS
Polls an IMAP mailbox for emails with a specific subject trigger and dispatches the body to agent-cli.
"""

import imaplib
import email
import email.utils
import time
import subprocess
import os

# Configuration
IMAP_SERVER = os.environ.get("IMAP_SERVER", "imap.gmail.com")
EMAIL_ACCOUNT = os.environ.get("EMAIL_ACCOUNT", "your_agent_email@gmail.com")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "your_app_password")
AUTHORIZED_SENDER = os.environ.get("AUTHORIZED_SENDER", "owner@gmail.com")  # The 1 parent email ID
SUBJECT_TRIGGER = "[AGENT-CMD]"
POLL_INTERVAL_SEC = 60

def process_email(msg):
    """Extracts body and sends to agent-cli, verifying the sender first.
    
    Security fix (2026-09-27):
    - Use email.utils.parseaddr() to extract only the email address, not
      the display name. This prevents 'Evil <authorized@gmail.com>' bypass.
    - Pass body as a separate argument, not string-formatted into the command.
    """
    raw_from = msg.get("From", "")
    # Parse only the email address portion (ignores display name)
    _, sender_addr = email.utils.parseaddr(raw_from)
    if AUTHORIZED_SENDER.lower() != sender_addr.lower():
        print(f"[!] Rejected command from unauthorized sender: {raw_from} (addr: {sender_addr})")
        return
        
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                body = part.get_payload(decode=True).decode('utf-8', 'ignore')
                break
    else:
        body = msg.get_payload(decode=True).decode('utf-8', 'ignore')
    
    body = body.strip()
    if not body:
        return
        
    print(f"[*] Received command via email: {body[:50]}...")
    
    # Security fix (2026-09-27): Pass body as separate argument (not string-formatted)
    # to prevent any argument-level injection from newlines or special characters.
    cmd = ["agent-cli", body]
    print(f"[*] Executing: agent-cli '<email body>'")
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        print(f"[*] Task completed. Agent Output:\n{result.stdout}")
        if result.returncode != 0:
            print(f"[!] agent-cli returned error: {result.stderr}")
        
    except Exception as e:
        print(f"[!] Failed to execute agent-cli: {e}")

def poll_mailbox():
    mail = None
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER)
        mail.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
        mail.select("inbox")
        
        # Search for UNSEEN emails with the specific subject trigger
        status, messages = mail.search(None, f'(UNSEEN SUBJECT "{SUBJECT_TRIGGER}")')
        if status != "OK":
            return
            
        mail_ids = messages[0].split()
        for mail_id in mail_ids:
            status, msg_data = mail.fetch(mail_id, "(RFC822)")
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    process_email(msg)
    except Exception as e:
        print(f"[!] IMAP polling error: {e}")
    finally:
        # Bug fix (2026-09-27): Always close IMAP connection to prevent leaks
        if mail is not None:
            try:
                mail.close()
                mail.logout()
            except Exception:
                pass

if __name__ == "__main__":
    print(f"Starting Mail-to-Intent Poller. Checking for '{SUBJECT_TRIGGER}' every {POLL_INTERVAL_SEC}s...")
    while True:
        poll_mailbox()
        time.sleep(POLL_INTERVAL_SEC)
