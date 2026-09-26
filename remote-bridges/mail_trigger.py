#!/usr/bin/env python3
"""
Mail-to-Intent Bridge for AI-Agent OS
Polls an IMAP mailbox for emails with a specific subject trigger and dispatches the body to agent-cli.
"""

import imaplib
import email
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
    """Extracts body and sends to agent-cli, verifying the sender first."""
    sender = msg.get("From", "")
    if AUTHORIZED_SENDER.lower() not in sender.lower():
        print(f"[!] Rejected command from unauthorized sender: {sender}")
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
    
    # Dispatch to agent-cli
    cmd = ["agent-cli", f"/assist {body}"]
    print(f"[*] Executing: {' '.join(cmd)}")
    
    try:
        # Pass the body to agent-cli
        result = subprocess.run(cmd, capture_output=True, text=True)
        print(f"[*] Task completed. Agent Output:\n{result.stdout}")
        
        # Note: In a full implementation, you could use smtplib here to 
        # send result.stdout back to the original sender.
        
    except Exception as e:
        print(f"[!] Failed to execute agent-cli: {e}")

def poll_mailbox():
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
                    
        mail.close()
        mail.logout()
    except Exception as e:
        print(f"[!] IMAP polling error: {e}")

if __name__ == "__main__":
    print(f"Starting Mail-to-Intent Poller. Checking for '{SUBJECT_TRIGGER}' every {POLL_INTERVAL_SEC}s...")
    while True:
        poll_mailbox()
        time.sleep(POLL_INTERVAL_SEC)
