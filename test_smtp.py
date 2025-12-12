import smtplib
from email.message import EmailMessage
import json
import os

def load_settings() -> dict:
    """Load settings from settings.json."""
    settings_path = os.path.join(os.path.dirname(__file__), 'settings.json')
    try:
        with open(settings_path, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        print("Error: settings.json not found. Please ensure it exists in the same directory as this script.")
        return {}

def test_smtp_connection():
    """Test SMTP connection using settings from settings.json."""
    settings = load_settings()
    
    smtp_host = settings.get("smtp_host")
    smtp_port = settings.get("smtp_port", 587)
    smtp_user = settings.get("smtp_user")
    smtp_pass = settings.get("smtp_pass")
    use_tls = settings.get("smtp_use_tls", True)
    
    if not all([smtp_host, smtp_user, smtp_pass]):
        print("Error: Missing SMTP configuration in settings.json.")
        return False
    
    print(f"Testing SMTP connection to {smtp_host}:{smtp_port}...")
    print(f"Username: {smtp_user}")
    print(f"Using TLS: {use_tls}")
    
    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
            print("Connected to SMTP server.")
            
            if use_tls:
                print("Starting TLS...")
                server.starttls()
                print("TLS started successfully.")
            
            print("Attempting to log in...")
            server.login(smtp_user, smtp_pass)
            print("Login successful!")
            
            # Test sending an email
            msg = EmailMessage()
            msg["Subject"] = "SMTP Test Email"
            msg["From"] = settings.get("from_email", smtp_user)
            msg["To"] = smtp_user  # Send to self for testing
            msg.set_content("This is a test email to verify SMTP settings.")
            
            server.send_message(msg)
            print(f"Test email sent successfully to {smtp_user}")
            return True
            
    except Exception as e:
        print(f"SMTP connection failed: {e}")
        return False

if __name__ == "__main__":
    test_smtp_connection()
